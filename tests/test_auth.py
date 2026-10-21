from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import bili_audio as app
import bili_auth as auth

# A real nav response always carries wbi_img; the signing path depends on it.
WBI_IMG = {
    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
}


def nav_payload(**data) -> dict:
    return {"code": 0, "data": {"wbi_img": WBI_IMG, **data}}


def signing_session() -> mock.Mock:
    """A Mock session that still runs the real WBI signing code, with a fixed key."""
    session = mock.Mock()
    session.ensure_buvid.return_value = None
    session.wbi_mixin_key.return_value = "0" * 32
    session.wbi_query.side_effect = lambda params, timeout: auth.Session.wbi_query(session, params, timeout)
    return session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = mock.patch.dict(os.environ, {"BILI_AUDIO_CONFIG_DIR": self.directory.name}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_save_reload_permissions_precedence_and_cleanup(self):
        with auth.Session("test") as session:
            temporary = session.path
            session.load_header("SESSDATA=secret; bili_jct=csrf")
            session.save()
            self.assertEqual(stat.S_IMODE(Path(session.path).stat().st_mode), 0o600)
        self.assertFalse(Path(temporary).exists())
        self.assertEqual(stat.S_IMODE(auth.session_path().stat().st_mode), 0o600)
        with auth.Session("test") as session:
            self.assertTrue(session.has_login)
            self.assertEqual(session.source, "saved")
        with mock.patch.dict(os.environ, {"BILIBILI_COOKIE": "SESSDATA=other"}):
            with auth.Session("test") as session:
                self.assertEqual(session.source, "environment")
                self.assertEqual(next(iter(session.jar)).value, "other")
            with auth.Session("test", str(auth.session_path())) as session:
                self.assertEqual(session.source, "file")
                self.assertEqual(next(iter(session.jar)).value, "secret")

    def test_verified_account_survives_restart_without_network_check(self):
        response = {"code": 0, "data": {"isLogin": True, "mid": 42, "uname": "demo"}}
        with auth.Session("test") as session:
            cookie = auth.cookie_from_pair("SESSDATA", "secret")
            cookie.expires = int(auth.time.time()) + 3600
            session.jar.set_cookie(cookie)
            with mock.patch.object(session, "request", return_value=response):
                session.status(1)
            session.save()
        with auth.Session("test") as restored:
            with mock.patch.object(restored, "request", return_value=response) as request:
                self.assertEqual(restored.status(1, cached=True)["uid"], 42)
                request.assert_not_called()
                restored.status(1)  # Explicit status always checks the server.
                request.assert_called_once()

    def test_cache_expiry_changed_cookie_and_revocation(self):
        response = {"code": 0, "data": {"isLogin": True, "mid": 42, "uname": "demo"}}
        with auth.Session("test") as session:
            session.load_header("SESSDATA=secret")
            with mock.patch.object(session, "request", return_value=response) as request:
                session.status(1)
                session._verified["until"] = 0
                session.status(1, cached=True)
                self.assertEqual(request.call_count, 2)
                session.load_header("SESSDATA=changed")
                session.status(1, cached=True)
                self.assertEqual(request.call_count, 3)
                session.save()
        with auth.Session("test") as session:
            with mock.patch.object(session, "request", return_value={"code": -101}):
                self.assertFalse(session.status(1)["logged_in"])
        self.assertNotIn(auth.ACCOUNT_COMMENT, auth.session_path().read_text())

    def test_one_command_login_then_next_launch_skips_verification(self):
        calls = []

        def qr(session, *_):
            session.load_header("SESSDATA=new-login")

        def request(session, url, timeout):
            calls.append(url)
            if url == auth.NAV_URL:
                return nav_payload(isLogin=True, mid=42, uname="demo")
            if url == auth.SPI_URL:
                return {"code": 0, "data": {"b_3": "buvid3-value", "b_4": "buvid4-value"}}
            return {"code": 0, "data": {"item": [{"goto": "av", "bvid": "BV1234567890"}]}}

        with (mock.patch("bili_audio.terminal_is_interactive", return_value=True),
              mock.patch("bili_audio.require_command", return_value="fzf"),
              mock.patch("bili_audio.login_qr", side_effect=qr) as login,
              mock.patch.object(auth.Session, "request", autospec=True, side_effect=request),
              mock.patch("bili_audio.interactive_playback", return_value=0) as playback,
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(app.main([]), 0)
            self.assertTrue(auth.session_path().exists())
            self.assertEqual(app.main([]), 0)
            self.assertEqual(login.call_count, 1)
            self.assertEqual(playback.call_count, 2)
            # One nav verifies the login; the second launch skips that and only
            # fetches the WBI signing key, which is not cached across processes.
            self.assertEqual(calls.count(auth.NAV_URL), 2)
            self.assertEqual(calls.count(auth.SPI_URL), 1)  # buvid3 persists after the first run.

    def test_revoked_feed_reauthenticates_once_and_restarts_list(self):
        status = nav_payload(isLogin=True, mid=42, uname="demo")
        with auth.Session("test") as session:
            session.load_header("SESSDATA=old")
            with mock.patch.object(session, "request", return_value=status):
                session.status(1)
            session.save()
        calls = []

        feeds = []

        def request(session, url, timeout):
            calls.append(url)
            if url == auth.NAV_URL:
                return status
            if url == auth.SPI_URL:
                return {"code": 0, "data": {"b_3": "buvid3-value", "b_4": "buvid4-value"}}
            feeds.append(url)
            if len(feeds) == 1:  # The saved account was revoked server-side.
                return {"code": -101}
            return {"code": 0, "data": {"item": []}}

        with (mock.patch("bili_audio.terminal_is_interactive", return_value=True),
              mock.patch("bili_audio.require_command", return_value="fzf"),
              mock.patch("bili_audio.login_qr", side_effect=lambda s, *_: s.load_header("SESSDATA=new")) as login,
              mock.patch.object(auth.Session, "request", autospec=True, side_effect=request),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(app.main([]), 1)  # Empty authenticated feed, not an auth failure.
            login.assert_called_once()
            # buvid, WBI key, revoked feed, nav for the new login, feed again.
            self.assertEqual(len(calls), 5)
            self.assertEqual(calls.count(auth.SPI_URL), 1)  # One device id, kept across re-login.

    def test_network_failure_does_not_trigger_qr_or_replace_account(self):
        with auth.Session("test") as session:
            session.load_header("SESSDATA=old")
            session.save()
        before = auth.session_path().read_bytes()
        with (mock.patch.object(auth.Session, "request", side_effect=SystemExit("網路逾時")),
              mock.patch("bili_audio.login_qr") as login):
            with self.assertRaisesRegex(SystemExit, "網路逾時"):
                app.main([])
            login.assert_not_called()
        self.assertEqual(auth.session_path().read_bytes(), before)

    def test_expired_cookie_never_uses_cached_account(self):
        response = {"code": 0, "data": {"isLogin": True, "mid": 42, "uname": "demo"}}
        with auth.Session("test") as session:
            cookie = auth.cookie_from_pair("SESSDATA", "secret")
            cookie.expires = int(auth.time.time()) + 3600
            session.jar.set_cookie(cookie)
            with mock.patch.object(session, "request", return_value=response):
                session.status(1)
            cookie.expires = 1
            with mock.patch.object(session, "request") as request:
                self.assertFalse(session.status(1, cached=True)["logged_in"])
                request.assert_not_called()

    def test_import_discards_foreign_and_expired_cookies(self):
        cookie_file = Path(self.directory.name) / "export.txt"
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tvalid\n"
            ".notbilibili.com\tTRUE\t/\tTRUE\t0\tforeign\tsecret\n"
            ".bilibili.com\tTRUE\t/\tTRUE\t1\told\texpired\n"
        )
        with auth.Session("test", str(cookie_file)) as session:
            self.assertEqual([c.name for c in session.jar], ["SESSDATA"])

    def test_header_injection_and_wrong_domain_rejected(self):
        with auth.Session("test") as session:
            for raw in ("SESSDATA=x\nX-Evil: yes", "bad name=value", "bad", "SESSDATA=with space"):
                with self.subTest(raw=raw), self.assertRaises(SystemExit):
                    session.load_header(raw)
            with self.assertRaises(SystemExit):
                session.request("https://evil.example/?token=secret", 1)

    def test_curl_keeps_secret_out_of_argv_and_captures_set_cookie(self):
        with auth.Session("test") as session:
            session.load_header("SESSDATA=secret")

            def run(command, **kwargs):
                self.assertNotIn("secret", " ".join(command))
                self.assertNotIn("qr-secret", " ".join(command))
                self.assertIn("qrcode_key=qr-secret", kwargs["input"])
                self.assertNotIn("-L", command)
                jar_path = command[command.index("--cookie-jar") + 1]
                with open(jar_path, "a") as output:
                    output.write(".bilibili.com\tTRUE\t/\tTRUE\t0\tnew_cookie\tvalue\n")
                return subprocess.CompletedProcess(command, 0, '{"code":0}', "")

            with mock.patch("bili_auth.shutil.which", return_value="curl"), mock.patch("bili_auth.subprocess.run", side_effect=run):
                self.assertEqual(session.request(auth.QR_BASE + "poll?qrcode_key=qr-secret", 5), {"code": 0})
            self.assertIn("new_cookie", [c.name for c in session.jar])

    def test_status_expired_and_transport_errors_are_distinct(self):
        with auth.Session("test") as session:
            with mock.patch.object(session, "request") as request:
                self.assertFalse(session.status(1)["logged_in"])
                request.assert_not_called()
                session.load_header("SESSDATA=secret")
                request.return_value = {"code": -101}
                self.assertFalse(session.status(1)["logged_in"])
                request.return_value = {"code": -412}
                with self.assertRaises(SystemExit):
                    session.status(1)

    def test_failed_login_preserves_existing_session(self):
        with auth.Session("test") as session:
            session.load_header("SESSDATA=original")
            session.save()
        before = auth.session_path().read_bytes()
        with mock.patch("sys.stdin", io.StringIO("SESSDATA=invalid\n")), mock.patch.object(auth.Session, "request", return_value={"code": -101}):
            with self.assertRaisesRegex(SystemExit, "原本儲存的帳號未變更"):
                app.main(["login", "--cookie-stdin"])
        self.assertEqual(auth.session_path().read_bytes(), before)

    def test_import_login_status_and_logout_flow(self):
        response = {"code": 0, "data": {"isLogin": True, "mid": 42, "uname": "測試帳號"}}
        with mock.patch.object(auth.Session, "request", return_value=response), mock.patch("sys.stdin", io.StringIO("SESSDATA=secret\n")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.main(["login", "--cookie-stdin"]), 0)
            self.assertNotIn("secret", output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(app.main(["status", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["uid"], 42)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(["logout"]), 0)
            self.assertFalse(auth.session_path().exists())

    def test_api_and_player_share_scoped_credentials(self):
        with auth.Session("test") as session:
            session.load_header("SESSDATA=secret")
            session.save()
        paths = []

        def dispatch(args):
            self.assertEqual(args.backend, "direct")
            self.assertIsNotNone(app._AUTH_SESSION)
            self.assertEqual(args.cookies, app._AUTH_SESSION.path)
            self.assertTrue(Path(args.cookies).exists())
            self.assertIn("--cookies", app.yt_dlp_cookie_args(args))
            video = app.make_video({"bvid": "BV1234567890", "title": "demo"})
            command = app.build_mpv_command(video, args, "audio", mpv_path="mpv")
            self.assertNotIn("secret", " ".join(command))
            self.assertIn(f"--ytdl-raw-options-append=cookies={args.cookies}", command)
            paths.append(args.cookies)
            return 0

        with mock.patch("bili_audio.dispatch_public", side_effect=dispatch):
            self.assertEqual(app.main(["hot", "--first"]), 0)
        self.assertFalse(Path(paths[0]).exists())
        self.assertIsNone(app._AUTH_SESSION)

    def test_network_diagnostics_do_not_echo_credentials(self):
        with auth.Session("test") as session:
            response = subprocess.CompletedProcess([], 22, "", "curl: requested URL returned error: 412 secret")
            with mock.patch("bili_auth.shutil.which", return_value="curl"), mock.patch("bili_auth.subprocess.run", return_value=response):
                with self.assertRaises(SystemExit) as error:
                    session.request(auth.NAV_URL, 1)
            self.assertIn("412", str(error.exception))
            self.assertNotIn("secret", str(error.exception))

    def test_qr_wait_scan_confirm_and_expiry(self):
        with auth.Session("test", empty=True) as session:
            responses = [
                {"code": 0, "data": {"url": "https://example.test/qr", "qrcode_key": "key"}},
                {"code": 0, "data": {"code": 86101}},
                {"code": 0, "data": {"code": 86090}},
                {"code": 0, "data": {"code": 0}},
            ]

            def request(*_):
                payload = responses.pop(0)
                if payload["data"].get("code") == 0:
                    session.load_header("SESSDATA=secret")
                return payload

            emit = mock.Mock()
            with mock.patch.object(session, "request", side_effect=request), mock.patch("bili_auth.shutil.which", return_value="qrencode"), mock.patch("bili_auth.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "QR", "")), mock.patch("bili_auth.time.sleep"):
                auth.login_qr(session, 1, emit)
            self.assertTrue(session.has_login)
            self.assertTrue(any("已掃描" in call.args[0] for call in emit.call_args_list))
            self.assertFalse(auth.session_path().exists(), "QR polling must not save before validation")

            responses[:] = [
                {"code": 0, "data": {"url": "url", "qrcode_key": "key"}},
                {"code": 0, "data": {"code": 86038}},
            ]
            with mock.patch.object(session, "request", side_effect=request), mock.patch("bili_auth.shutil.which", return_value="qrencode"), mock.patch("bili_auth.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "QR", "")):
                with self.assertRaisesRegex(SystemExit, "已過期"):
                    auth.login_qr(session, 1, emit)


class WbiTests(unittest.TestCase):
    def test_mixin_key_is_a_32_char_fold_of_both_keys(self):
        session = auth.Session.__new__(auth.Session)
        session._wbi = None
        payload = {"code": 0, "data": {"wbi_img": WBI_IMG}}
        session.absorb_wbi(payload)
        key = session._wbi[1]
        self.assertEqual(len(key), 32)
        self.assertTrue(set(key) <= set("ab"))  # Folded from the two stub keys only.

    def test_nav_without_wbi_img_leaves_the_key_alone(self):
        session = auth.Session.__new__(auth.Session)
        session._wbi = (0.0, "cached-key")
        session.absorb_wbi({"code": 0, "data": {"isLogin": True}})
        self.assertEqual(session._wbi[1], "cached-key")

    def test_key_url_must_look_like_a_wbi_image(self):
        self.assertIsNone(auth.wbi_key_from_url("https://evil.example/payload.png"))
        self.assertIsNone(auth.wbi_key_from_url(None))
        self.assertEqual(auth.wbi_key_from_url(WBI_IMG["img_url"]), "a" * 32)

    def test_fixed_vector_matches_the_published_algorithm(self):
        # Keys as actually served by nav; mixin key and w_rid cross-checked
        # against an independent implementation of the documented algorithm.
        session = auth.Session.__new__(auth.Session)
        session._wbi = None
        session.absorb_wbi({"code": 0, "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }}})
        self.assertEqual(session._wbi[1], "ea1db124af3c7062474693fa704f4ff8")
        with mock.patch("bili_auth.time.time", return_value=1700000000.9):
            query = session.wbi_query(
                {"fresh_idx": 1, "ps": 12, "web_location": 1430650}, 5
            )
        self.assertEqual(
            query,
            "fresh_idx=1&ps=12&web_location=1430650&wts=1700000000"
            "&w_rid=3c162ec4d5e1e65d311aa2e8bd5695a2",
        )

    def test_signed_query_is_sorted_filtered_and_stamped(self):
        session = mock.Mock()
        session.wbi_mixin_key.return_value = "0" * 32
        query = auth.Session.wbi_query(
            session, {"keyword": "a b!c'(d)", "ps": 12, "brush": 0}, 5
        )
        fields = [pair.split("=", 1)[0] for pair in query.split("&")]
        self.assertEqual(fields, ["brush", "keyword", "ps", "wts", "w_rid"])
        self.assertIn("keyword=a%20bcd", query)  # !'()* dropped, space as %20.
        self.assertRegex(query, r"&w_rid=[0-9a-f]{32}$")

    def test_unsigned_fallback_when_the_key_is_unavailable(self):
        session = mock.Mock()
        session.wbi_mixin_key.return_value = None
        query = auth.Session.wbi_query(session, {"ps": 12}, 5)
        self.assertNotIn("w_rid", query)
        self.assertIn("wts=", query)


class RecommendationTests(unittest.TestCase):
    def test_refresh_advances_server_batch_and_passes_preview_reason(self):
        session = signing_session()
        session.status.return_value = {"logged_in": True, "name": "demo"}
        session.request.return_value = {"code": 0, "data": {"item": [
            {"goto": "av", "bvid": "BV1234567890", "rcmd_reason": {"content": "已關注"}},
        ]}}

        def interactive(args, pages, title, *, page_loader):
            self.assertEqual(pages[0][0].description, "已關注")
            page_loader(1)
            page_loader(2)
            return 0

        with (mock.patch("bili_audio.interactive_playback", side_effect=interactive),
              mock.patch("bili_audio.MIN_API_INTERVAL", 0)):
            app.run_recommend(app.parse_args(["recommend"]), session)
        for index, call in enumerate(session.request.call_args_list, 1):
            self.assertIn(f"fresh_idx={index}&", call.args[0])
            # Every feed call must be WBI-signed, or risk control answers 412.
            self.assertRegex(call.args[0], r"&w_rid=[0-9a-f]{32}$")

    def test_authenticated_recommendations_filter_ads_and_canonicalize_urls(self):
        session = signing_session()
        session.status.return_value = {"logged_in": True, "name": "測試帳號"}
        session.request.return_value = {"code": 0, "data": {"item": [
            {"goto": "av", "bvid": "BV1234567890", "title": "音樂", "url": "https://evil.example"},
            {"goto": "ad", "bvid": "BV0987654321", "title": "廣告"},
            {"goto": "av", "bvid": "invalid"},
            {"goto": "av", "bvid": "BV1234567890", "title": "重複"},
        ]}}
        args = app.parse_args(["recommend", "--json", "--page", "2"])
        with (contextlib.redirect_stdout(io.StringIO()) as output,
              mock.patch("bili_audio.MIN_API_INTERVAL", 0)):
            self.assertEqual(app.run_recommend(args, session), 0)
        videos = json.loads(output.getvalue())
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["url"], "https://www.bilibili.com/video/BV1234567890")
        self.assertIn("fresh_idx=2", session.request.call_args.args[0])

    def test_recommendations_do_not_fall_back_to_anonymous_hot(self):
        session = mock.Mock()
        session.status.return_value = {"logged_in": False}
        with self.assertRaisesRegex(SystemExit, "需要有效登入"):
            app.run_recommend(app.parse_args(["recommend", "--json"]), session)
        session.request.assert_not_called()

    def test_queue_and_paging_then_refresh(self):
        videos = app.videos_from_items([
            {"bvid": "BV1234567890", "title": "first"},
            {"bvid": "BV0987654321", "title": "second"},
        ])
        selected = app.parse_fzf_result("enter\n0\tfirst\n1\tsecond\n", videos)
        self.assertEqual(selected.selected, tuple(videos))
        with self.assertRaises(SystemExit):
            app.parse_fzf_result("enter\n-1\tinvalid\n", videos)
        args = app.parse_args(["recommend"])
        loader = mock.Mock(side_effect=[[videos[1]], [videos[0]]])
        actions = [app.FzfResult(app.FZF_NEXT_PAGE), app.FzfResult(app.FZF_REFRESH),
                   selected, app.FzfResult(app.FZF_CANCEL)]
        pages = [[videos[0]]]
        with mock.patch("bili_audio.run_fzf", side_effect=actions), mock.patch("bili_audio.play_selected", return_value=0) as play, contextlib.redirect_stderr(io.StringIO()):
            app.interactive_playback(args, pages, "test", page_loader=loader)
        self.assertEqual(loader.call_args_list, [mock.call(2), mock.call(2)])
        self.assertEqual(play.call_count, 2)
        self.assertEqual(pages[1], [videos[0]])


if __name__ == "__main__":
    unittest.main()
