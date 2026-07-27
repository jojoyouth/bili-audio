from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

import bili_audio
import bili_backend


class VideoNormalizationTests(unittest.TestCase):
    def test_normalizes_upstream_hot_video(self) -> None:
        video = bili_audio.make_video(
            {
                "bvid": "BV1ABcsztEcY",
                "title": "<em>測試</em>影片",
                "duration": 125,
                "owner": {"name": "UP 主"},
                "stat": {"view": 12345, "like": 678},
                "pubdate": 1_700_000_000,
                "desc": "介紹",
                "score": 999,
            }
        )

        self.assertIsNotNone(video)
        assert video is not None
        self.assertEqual(video.title, "測試影片")
        self.assertEqual(video.author, "UP 主")
        self.assertEqual(video.duration, "02:05")
        self.assertEqual(video.play, 12345)
        self.assertEqual(video.like, 678)
        self.assertEqual(video.score, 999)
        self.assertEqual(
            video.url,
            "https://www.bilibili.com/video/BV1ABcsztEcY",
        )

    def test_normalizes_upstream_search_video(self) -> None:
        video = bili_audio.make_video(
            {
                "id": "BV1ABcsztEcY",
                "bvid": "BV1ABcsztEcY",
                "title": "搜尋結果",
                "author": "作者",
                "duration": "3:04",
                "play": 8,
            }
        )

        self.assertIsNotNone(video)
        assert video is not None
        self.assertEqual(video.duration, "3:04")
        self.assertEqual(video.author, "作者")
        self.assertEqual(video.aid, "")

    def test_normalizes_user_envelope(self) -> None:
        user = bili_audio.make_user(
            {
                "user": {
                    "id": "946974",
                    "name": "影視颶風",
                    "level": 6,
                    "sign": "哈囉",
                },
                "relation": {"follower": 10, "following": 2},
            }
        )

        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.uid, "946974")
        self.assertEqual(user.fans, 10)
        self.assertEqual(user.following, 2)

    def test_remote_text_cannot_emit_terminal_control_sequences(self) -> None:
        value = (
            "安全"
            "\x1b]52;c;Y2xpcGJvYXJk\x07"
            "\x1b[31m紅色\x1b[0m"
            "\u202e"
        )

        self.assertEqual(bili_audio.clean_text(value), "安全紅色")


class CliCompatibilityTests(unittest.TestCase):
    def test_legacy_keyword_becomes_search(self) -> None:
        args = bili_audio.parse_args(["lofi", "--json"])
        self.assertEqual(args.command, "search")
        self.assertEqual(args.keyword, ["lofi"])
        self.assertTrue(args.json)

    def test_explicit_commands_remain_commands(self) -> None:
        args = bili_audio.parse_args(["hot", "--page", "2", "--limit", "5"])
        self.assertEqual(args.command, "hot")
        self.assertEqual(args.page, 2)
        self.assertEqual(args.limit, 5)

    def test_video_mode_options(self) -> None:
        args = bili_audio.parse_args(
            [
                "search",
                "demo",
                "--first",
                "--play-mode",
                "video",
                "--video-output",
                "tct",
            ]
        )
        self.assertEqual(args.play_mode, "video")
        self.assertEqual(args.video_output, "tct")

    def test_volume_range_is_validated(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            bili_audio.parse_args(["search", "demo", "--volume", "101"])

    def test_timeout_must_be_finite(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                bili_audio.parse_args(
                    ["search", "demo", "--timeout", value]
                )


class FzfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.video = bili_audio.Video(
            title="Demo",
            author="UP",
            bvid="BV1ABcsztEcY",
            aid="",
            url="https://www.bilibili.com/video/BV1ABcsztEcY",
            duration="01:00",
            play=1,
            pubdate=None,
            description="",
        )

    def test_ctrl_a_selects_audio(self) -> None:
        result = bili_audio.parse_fzf_result(
            "ctrl-a\n0\tDemo\n",
            [self.video],
        )
        self.assertEqual(result.action, bili_audio.FZF_PLAY_AUDIO)
        self.assertEqual(result.video, self.video)

    def test_ctrl_v_selects_terminal_video(self) -> None:
        result = bili_audio.parse_fzf_result(
            "ctrl-v\n0\tDemo\n",
            [self.video],
        )
        self.assertEqual(result.action, bili_audio.FZF_PLAY_VIDEO)

    def test_page_keys_are_preserved(self) -> None:
        result = bili_audio.parse_fzf_result("ctrl-f\n", [self.video])
        self.assertEqual(result.action, bili_audio.FZF_NEXT_PAGE)


class PlayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.video = bili_audio.Video(
            title="Demo",
            author="UP",
            bvid="BV1ABcsztEcY",
            aid="",
            url="https://www.bilibili.com/video/BV1ABcsztEcY",
            duration="01:00",
            play=1,
            pubdate=None,
            description="",
        )
        self.args = argparse.Namespace(
            mpv="/bin/echo",
            volume=40,
            cookies_from_browser=None,
            cookies=None,
            video_output="tct",
        )

    def test_audio_command_disables_video(self) -> None:
        command = bili_audio.build_mpv_command(self.video, self.args, "audio")
        self.assertIn("--no-video", command)
        self.assertIn("--referrer=https://www.bilibili.com/", command)
        self.assertNotIn("--vo=tct", command)

    def test_video_command_uses_terminal_vo(self) -> None:
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        self.assertIn("--no-config", command)
        self.assertIn("--vo=tct", command)
        self.assertIn("--profile=sw-fast", command)
        self.assertIn("--really-quiet", command)
        self.assertIn("--term-status-msg=", command)
        self.assertIn("--vo-tct-buffering=frame", command)
        self.assertNotIn("--no-video", command)

    def test_cookie_path_with_comma_uses_append_option(self) -> None:
        self.args.cookies = "/tmp/bili,cookies.txt"

        command = bili_audio.build_mpv_command(
            self.video,
            self.args,
            "audio",
        )

        self.assertIn(
            "--ytdl-raw-options-append=cookies=/tmp/bili,cookies.txt",
            command,
        )
        self.assertFalse(
            any(item.startswith("--ytdl-raw-options=") for item in command)
        )

    def test_invalid_explicit_mpv_has_actionable_error(self) -> None:
        self.args.mpv = "/definitely/missing/bili-audio-mpv"

        with self.assertRaisesRegex(SystemExit, "找不到指定的 mpv"):
            bili_audio.build_mpv_command(self.video, self.args, "audio")

    def test_auto_uses_kitty_when_available(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"KITTY_WINDOW_ID": "1"},
            clear=True,
        ):
            self.assertEqual(bili_audio.terminal_video_output("auto"), "kitty")

    def test_auto_falls_back_to_true_color_blocks(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bili_audio.terminal_video_output("auto"), "tct")

    def test_auto_avoids_kitty_inside_tmux(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"KITTY_WINDOW_ID": "1", "TMUX": "/tmp/tmux"},
            clear=True,
        ):
            self.assertEqual(bili_audio.terminal_video_output("auto"), "tct")

    @mock.patch("bili_audio.subprocess.run")
    def test_resolver_falls_back_when_mpv_lacks_kitty(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="  tct              true-color terminals\n",
            stderr="",
        )
        with mock.patch.dict(
            "os.environ",
            {"KITTY_WINDOW_ID": "1"},
            clear=True,
        ):
            self.assertEqual(
                bili_audio.resolve_mpv_video_output("/tmp/mpv", "auto"),
                "tct",
            )

    @mock.patch("bili_audio.subprocess.run")
    def test_explicit_missing_video_output_is_an_error(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="  tct              true-color terminals\n",
            stderr="",
        )
        with self.assertRaisesRegex(SystemExit, "沒有 kitty"):
            bili_audio.resolve_mpv_video_output("/tmp/mpv", "kitty")

    @mock.patch("bili_audio.subprocess.run")
    @mock.patch("bili_audio.resolve_mpv_video_output", return_value="tct")
    @mock.patch("bili_audio.terminal_is_interactive", return_value=True)
    def test_video_player_inherits_terminal_stdio(
        self,
        _interactive: mock.Mock,
        _resolve: mock.Mock,
        run: mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        with contextlib.redirect_stderr(io.StringIO()):
            returncode = bili_audio.play_video(self.video, self.args)

        self.assertEqual(returncode, 0)
        self.assertNotIn("stdout", run.call_args.kwargs)
        self.assertNotIn("stderr", run.call_args.kwargs)
        self.assertFalse(run.call_args.kwargs["check"])


class BackendEnvelopeTests(unittest.TestCase):
    def test_decodes_success_envelope(self) -> None:
        value = bili_backend._decode_envelope(
            json.dumps({"ok": True, "schema_version": "1", "data": [1, 2]})
        )
        self.assertEqual(value, [1, 2])

    def test_decodes_structured_error(self) -> None:
        with self.assertRaisesRegex(bili_backend.BiliCliError, "找不到"):
            bili_backend._decode_envelope(
                json.dumps(
                    {
                        "ok": False,
                        "schema_version": "1",
                        "error": {"code": "not_found", "message": "找不到"},
                    }
                )
            )

    @mock.patch("bili_backend.subprocess.run")
    def test_search_invokes_upstream_json_contract(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": [
                        {
                            "bvid": "BV1ABcsztEcY",
                            "title": "Demo",
                        }
                    ],
                }
            ),
            stderr="",
        )
        backend = bili_backend.BiliCliBackend("/tmp/bili", timeout=20)

        items = backend.search_videos("demo", page=2, count=5)

        self.assertEqual(items[0]["bvid"], "BV1ABcsztEcY")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/tmp/bili")
        self.assertIn("--json", command)
        self.assertIn("--page", command)
        self.assertIn("2", command)

    @mock.patch("bili_backend.subprocess.run")
    def test_backend_respects_requested_timeout(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"ok": True, "schema_version": "1", "data": []}
            ),
            stderr="",
        )
        backend = bili_backend.BiliCliBackend("/tmp/bili", timeout=0.25)

        backend.search_videos("demo")

        self.assertEqual(run.call_args.kwargs["timeout"], 0.25)

    def test_rejects_unsupported_schema_version(self) -> None:
        with self.assertRaisesRegex(
            bili_backend.BiliCliError,
            "schema 不相容",
        ):
            bili_backend._decode_envelope(
                json.dumps(
                    {
                        "ok": True,
                        "schema_version": "2",
                        "data": [],
                    }
                )
            )


class DirectApiErrorTests(unittest.TestCase):
    @mock.patch("bili_audio.run_curl_json")
    def test_http_429_gets_actionable_message(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=22,
            stdout="",
            stderr="curl: (22) The requested URL returned error: 429",
        )
        with self.assertRaisesRegex(SystemExit, "降低查詢頻率"):
            bili_audio.fetch_page_with_curl(
                "https://api.bilibili.com/example",
                1.0,
            )

    @mock.patch("bili_audio.warm_bilibili_cookie_jar")
    @mock.patch("bili_audio.run_curl_json")
    def test_json_412_warms_cookie_and_retries(
        self,
        run: mock.Mock,
        warm: mock.Mock,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"code": -412, "message": "risk"}),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps({"code": 0, "data": {}}),
                stderr="",
            ),
        ]

        with mock.patch.dict("os.environ", {}, clear=True):
            payload = bili_audio.fetch_page_with_curl(
                "https://api.bilibili.com/example",
                1.0,
            )

        self.assertEqual(payload["code"], 0)
        warm.assert_called_once_with(1.0)
        self.assertEqual(run.call_count, 2)

    @mock.patch("bili_audio.subprocess.run")
    def test_cookie_value_is_not_exposed_in_curl_argv(
        self,
        run: mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="{}",
            stderr="",
        )

        with mock.patch.dict(
            "os.environ",
            {"BILIBILI_COOKIE": "SESSDATA=top-secret"},
            clear=True,
        ):
            bili_audio.run_curl_json(
                "https://api.bilibili.com/example",
                1.0,
            )

        command = run.call_args.args[0]
        self.assertNotIn("top-secret", " ".join(command))
        self.assertEqual(
            run.call_args.kwargs["input"],
            "Cookie: SESSDATA=top-secret\n",
        )

    @mock.patch("bili_audio.fetch_json_url")
    def test_user_card_preserves_outer_archive_count(
        self,
        fetch: mock.Mock,
    ) -> None:
        fetch.return_value = {
            "code": 0,
            "data": {
                "card": {"mid": "1", "name": "Demo"},
                "archive_count": 42,
            },
        }

        card = bili_audio.direct_user_card("1", 1.0)
        user = bili_audio.make_user(card)

        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.videos, 42)


class JsonOutputTests(unittest.TestCase):
    def test_empty_video_result_is_valid_json(self) -> None:
        args = argparse.Namespace(json=True)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            returncode = bili_audio.finish_video_action(
                args,
                [],
                "empty",
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_prompt_does_not_contaminate_json_stdout(self) -> None:
        args = argparse.Namespace(
            keyword=[],
            json=True,
            pages=1,
            limit=0,
        )
        source = mock.Mock()
        source.search_videos.return_value = []
        output = io.StringIO()
        errors = io.StringIO()

        with (
            mock.patch("builtins.input", return_value="demo"),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            returncode = bili_audio.run_search(args, source)

        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(output.getvalue()), [])
        self.assertIn("搜尋關鍵字", errors.getvalue())

    def test_prompt_eof_has_clean_error(self) -> None:
        args = argparse.Namespace(
            keyword=[],
            json=True,
            pages=1,
            limit=0,
        )

        with (
            mock.patch("builtins.input", side_effect=EOFError),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(SystemExit, "需要輸入搜尋關鍵字"),
        ):
            bili_audio.run_search(args, mock.Mock())


class CommandLimitTests(unittest.TestCase):
    @staticmethod
    def items(count: int) -> list[dict[str, object]]:
        return [
            {
                "bvid": f"BV{index:010d}",
                "title": f"Video {index}",
            }
            for index in range(count)
        ]

    def test_hot_zero_limit_keeps_all_fetched_results(self) -> None:
        source = mock.Mock()
        source.hot.return_value = self.items(25)
        args = argparse.Namespace(limit=0, page=1, json=True)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            returncode = bili_audio.run_hot(args, source)

        self.assertEqual(returncode, 0)
        source.hot.assert_called_once_with(
            1,
            bili_audio.UNLIMITED_PAGE_COUNT,
        )
        self.assertEqual(len(json.loads(output.getvalue())), 25)

    def test_rank_zero_limit_keeps_all_fetched_results(self) -> None:
        source = mock.Mock()
        source.rank.return_value = self.items(25)
        args = argparse.Namespace(limit=0, day=None, json=True)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            returncode = bili_audio.run_rank(args, source)

        self.assertEqual(returncode, 0)
        source.rank.assert_called_once_with(bili_audio.UNLIMITED_PAGE_COUNT)
        self.assertEqual(len(json.loads(output.getvalue())), 25)

    def test_search_limit_is_global_across_pages(self) -> None:
        pages: list[list[bili_audio.Video]] = []
        for page_number in range(2):
            page: list[bili_audio.Video] = []
            for item_number in range(3):
                index = page_number * 3 + item_number
                page.append(
                    bili_audio.Video(
                        title=f"Video {index}",
                        author="Demo",
                        bvid=f"BV{index:010d}",
                        aid="",
                        url=f"https://example.com/{index}",
                        duration="00:01",
                        play=0,
                        pubdate=None,
                        description="",
                    )
                )
            pages.append(page)

        limited = bili_audio.limit_video_pages(pages, 5)

        self.assertEqual([len(page) for page in limited], [3, 2])
        self.assertEqual(len(bili_audio.flatten_video_pages(limited)), 5)

    def test_user_video_limit_applies_to_json(self) -> None:
        source = mock.Mock()
        source.user.return_value = bili_audio.UserSummary(
            uid="1",
            name="Demo",
        )
        source.user_videos.return_value = self.items(10)
        args = argparse.Namespace(
            uid_or_name=["1"],
            videos=10,
            limit=2,
            json=True,
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            returncode = bili_audio.run_user(args, source)

        self.assertEqual(returncode, 0)
        self.assertEqual(len(json.loads(output.getvalue())["videos"]), 2)


if __name__ == "__main__":
    unittest.main()
