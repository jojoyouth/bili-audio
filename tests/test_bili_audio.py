from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import stat
import subprocess
import tempfile
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

    def test_defaults_and_playback_feedback(self):
        args = bili_audio.parse_args([])
        self.assertEqual(args.play_mode, "video")
        self.assertEqual(args.video_output, "kitty")
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        self.assertIn("--osd-duration=2000", command)
        self.assertTrue(any(arg.startswith("--input-conf=") for arg in command))
        overlay = next(arg for arg in command if arg.startswith("--osd-msg1="))
        self.assertIn("${?mute==yes:靜音}", overlay)
        self.assertIn("${?pause==no:播放中}", overlay)
        self.assertEqual(bili_audio.parse_args(["recommend", "--play-mode", "audio"]).play_mode, "audio")

    def test_video_command_uses_terminal_vo(self) -> None:
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        self.assertIn("--no-config", command)
        self.assertIn("--vo=tct", command)
        self.assertIn("--profile=sw-fast", command)
        self.assertNotIn("--really-quiet", command)
        self.assertIn("--msg-level=all=info,osd/libass=fatal", command)
        self.assertIn("--osd-level=1", command)
        self.assertIn("--osc=no", command)
        self.assertIn("--ytdl-format=bestvideo+bestaudio/best", command)
        self.assertIn("--network-timeout=20", command)
        self.assertIn("--ytdl-raw-options-append=socket-timeout=20", command)
        self.assertIn("--term-status-msg=", command)
        self.assertIn("--vo-tct-buffering=frame", command)
        self.assertNotIn("--no-video", command)

    def test_kitty_output_uses_shared_memory(self) -> None:
        # base64 frames cost ~230 KB each on the PTY, which caps playback well under
        # 60 fps — but only kitty itself can read the shared-memory alternative.
        with mock.patch.dict(os.environ,
                             {"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "1",
                              "PATH": os.environ.get("PATH", "")}, clear=True):
            command = bili_audio.build_mpv_command(
                self.video, self.args, "video", resolved_video_output="kitty"
            )
        self.assertIn("--vo=kitty", command)
        self.assertIn("--vo-kitty-use-shm=yes", command)
        self.assertNotIn("--vo-tct-buffering=frame", command)

    def test_shm_only_where_the_terminal_reads_it(self) -> None:
        # Ghostty speaks the kitty graphics protocol but ignores shm, so turning
        # it on there blanks the picture entirely.
        cases = {
            "no": [{"TERM_PROGRAM": "ghostty", "TERM": "xterm-ghostty"},
                   {"TERM": "xterm-256color"},
                   {"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "1", "TMUX": "/tmp/s"}],
            "yes": [{"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "1"},
                    {"TERM_PROGRAM": "kitty"}],
        }
        for expected, environments in cases.items():
            for environment in environments:
                with mock.patch.dict(os.environ, {**environment,
                                                  "PATH": os.environ.get("PATH", "")},
                                     clear=True):
                    command = bili_audio.build_mpv_command(
                        self.video, self.args, "video", resolved_video_output="kitty"
                    )
                self.assertIn(f"--vo-kitty-use-shm={expected}", command,
                              f"{environment} 應該是 {expected}")

    def test_kitty_shm_can_be_forced(self) -> None:
        self.args.kitty_shm = "yes"
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "ghostty",
                                          "PATH": os.environ.get("PATH", "")}, clear=True):
            command = bili_audio.build_mpv_command(
                self.video, self.args, "video", resolved_video_output="kitty"
            )
        self.assertIn("--vo-kitty-use-shm=yes", command)

    def test_libass_probe_errors_are_silenced(self) -> None:
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        self.assertIn("--msg-level=all=info,osd/libass=fatal", command)

    def test_input_conf_resolves_through_a_symlink(self) -> None:
        # Installed as ~/.local/bin/bili-audio, abspath would look for the key
        # bindings next to the symlink and every custom key would die silently.
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        conf = [item for item in command if item.startswith("--input-conf=")][0]
        expected = os.path.join(
            os.path.dirname(os.path.realpath(bili_audio.__file__)), "mpv-input.conf"
        )
        self.assertEqual(conf, f"--input-conf={expected}")
        self.assertTrue(os.path.exists(expected), "mpv-input.conf 應該在 repo 裡")

    def test_stealth_size_does_not_follow_the_window(self) -> None:
        # Snapping from half-screen to a bottom-right quarter must keep the same
        # picture, not shrink it along with the terminal.
        heights = []
        for cols, rows in ((90, 52), (90, 26), (60, 17)):
            with mock.patch.object(bili_audio, "terminal_metrics",
                                   return_value=(cols, rows, 8, 17)):
                placement = bili_audio.stealth_kitty_placement()
            heights.append(int([item for item in placement
                                if item.startswith("--vo-kitty-height=")][0].split("=")[1]))
        self.assertEqual(heights, [bili_audio.STEALTH_HEIGHT_ROWS * 17] * 3)

    def test_stealth_size_shrinks_only_when_it_cannot_fit(self) -> None:
        with mock.patch.object(bili_audio, "terminal_metrics",
                               return_value=(30, 12, 8, 17)):
            placement = bili_audio.stealth_kitty_placement()
        height = int([item for item in placement
                      if item.startswith("--vo-kitty-height=")][0].split("=")[1])
        self.assertLess(height, bili_audio.STEALTH_HEIGHT_ROWS * 17)
        self.assertGreaterEqual(height, 34)

    def test_stealth_size_is_configurable(self) -> None:
        args = bili_audio.parse_args(["recommend", "--stealth", "--stealth-rows", "6"])
        self.assertEqual(args.stealth_rows, 6)
        with mock.patch.object(bili_audio, "terminal_metrics",
                               return_value=(90, 52, 8, 17)):
            command = bili_audio.build_mpv_command(
                self.video, args, "video", resolved_video_output="kitty"
            )
        self.assertIn("--vo-kitty-height=102", command)

    def test_stealth_sits_against_the_corner(self) -> None:
        # mpv positions with ESC[top;leftf, which is 1-based, and transmits with
        # C=1 on the alternate screen, so the last column is safe to use.
        # Metrics here are what Ghostty actually reported.
        for cols, rows, cell_w, cell_h in ((110, 26, 17, 37), (90, 52, 17, 37),
                                           (110, 13, 17, 37), (80, 24, 8, 17)):
            with mock.patch.object(bili_audio, "terminal_metrics",
                                   return_value=(cols, rows, cell_w, cell_h)):
                placement = dict(
                    item.removeprefix("--vo-kitty-").split("=")
                    for item in bili_audio.stealth_kitty_placement()
                )
            height = int(placement["height"])
            left, top = int(placement["left"]), int(placement["top"])
            used_rows = -(-height // cell_h)
            used_cols = -(-(height * 16 // 9) // cell_w)
            last_row, last_col = top + used_rows - 1, left + used_cols - 1
            self.assertLessEqual(last_row, rows - 1, f"{cols}x{rows} 超出底部")
            self.assertLessEqual(last_col, cols, f"{cols}x{rows} 超出右緣")
            if used_cols <= cols:
                self.assertEqual(last_col, cols, f"{cols}x{rows} 沒有貼齊右緣")
            self.assertGreaterEqual(top, 1)

    def test_stealth_placement_stays_inside_the_terminal(self) -> None:
        # mpv keeps the aspect ratio and ignores a cell box, so the reserved
        # area has to cover the widest shape it can return.
        for cols, rows, cell_w, cell_h in ((120, 40, 8, 17), (80, 24, 10, 20),
                                           (200, 50, 7, 15)):
            with mock.patch.object(bili_audio, "terminal_metrics",
                                   return_value=(cols, rows, cell_w, cell_h)):
                placement = dict(
                    item.removeprefix("--vo-kitty-").split("=")
                    for item in bili_audio.stealth_kitty_placement()
                )
            height_px = int(placement["height"])
            left, top = int(placement["left"]), int(placement["top"])
            used_rows = -(-height_px // cell_h)
            used_cols = -(-(height_px * 16 // 9) // cell_w)
            self.assertLessEqual(top + used_rows - 1, rows - 1, f"{cols}x{rows} 垂直超出")
            self.assertLessEqual(left + used_cols - 1, cols, f"{cols}x{rows} 水平超出")
            self.assertGreaterEqual(height_px, cell_h * 2)

    def test_stealth_placement_survives_an_unknown_terminal(self) -> None:
        with mock.patch.object(bili_audio, "terminal_metrics", return_value=None):
            self.assertEqual(bili_audio.stealth_kitty_placement(),
                             [f"--vo-kitty-height={bili_audio.STEALTH_HEIGHT_ROWS * 17}"])

    def test_max_height_is_opt_in(self) -> None:
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        self.assertIn("--ytdl-format=bestvideo+bestaudio/best", command)
        self.args.max_height = 480
        command = bili_audio.build_mpv_command(self.video, self.args, "video")
        self.assertIn("--ytdl-format=bestvideo[height<=?480]+bestaudio/best", command)

    def test_stealth_hides_osd_and_shrinks_the_picture(self) -> None:
        self.args.stealth = True
        with mock.patch.object(bili_audio, "terminal_metrics",
                               return_value=(120, 40, 8, 17)):
            command = bili_audio.build_mpv_command(
                self.video, self.args, "video", resolved_video_output="kitty"
            )
        self.assertIn("--osd-level=0", command)
        self.assertIn("--term-playing-msg=", command)
        self.assertIn("--term-title=", command)
        self.assertNotIn("--osd-level=1", command)
        self.assertFalse([item for item in command if item.startswith("--osd-msg1=")])
        self.assertTrue([item for item in command if item.startswith("--vo-kitty-height=")])
        self.assertTrue([item for item in command if item.startswith("--vo-kitty-left=")])
        # A forced cell box would make mpv overflow the corner and get clipped.
        self.assertFalse([item for item in command if item.startswith("--vo-kitty-cols=")])

    def test_stealth_prints_no_title_or_url(self) -> None:
        self.args.stealth = True
        with mock.patch("bili_audio.subprocess.run") as runner, \
             mock.patch("bili_audio.find_mpv", return_value="/bin/echo"), \
             mock.patch("bili_audio.resolve_mpv_video_output", return_value="kitty"), \
             mock.patch("bili_audio.terminal_is_interactive", return_value=True), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            runner.return_value = mock.Mock(returncode=0)
            bili_audio.play_video(self.video, self.args)
        printed = errors.getvalue()
        self.assertNotIn(self.video.url, printed)
        self.assertNotIn(self.video.title, printed)
        self.assertNotIn("--play-mode window", printed)

    def test_normal_mode_still_shows_the_url(self) -> None:
        with mock.patch("bili_audio.subprocess.run") as runner, \
             mock.patch("bili_audio.find_mpv", return_value="/bin/echo"), \
             mock.patch("bili_audio.resolve_mpv_video_output", return_value="tct"), \
             mock.patch("bili_audio.terminal_is_interactive", return_value=True), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            runner.return_value = mock.Mock(returncode=0)
            bili_audio.play_video(self.video, self.args)
        self.assertIn(self.video.url, errors.getvalue())

    def test_stealth_leaves_audio_mode_untouched(self) -> None:
        self.args.stealth = True
        command = bili_audio.build_mpv_command(self.video, self.args, "audio")
        self.assertIn("--no-video", command)
        self.assertNotIn("--osd-level=0", command)

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

    def test_window_mode_has_mouse_controls_and_selected_audio(self):
        self.args.audio_device = "coreaudio/BuiltInSpeakerDevice"
        command = bili_audio.build_mpv_command(self.video, self.args, "window")
        self.assertIn("--force-window=yes", command)
        self.assertIn("--osc=yes", command)
        self.assertIn("--mute=no", command)
        self.assertIn("--aid=auto", command)
        self.assertIn("--audio-device=coreaudio/BuiltInSpeakerDevice", command)
        self.assertFalse(any(arg.startswith("--vo=") for arg in command))

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

    def test_auto_uses_tct_for_other_graphics_terminals(self) -> None:
        for program in ("ghostty", "WezTerm"):
            with self.subTest(program=program), mock.patch.dict(
                "os.environ", {"TERM_PROGRAM": program, "TERM": "xterm-256color"}, clear=True
            ):
                self.assertEqual(bili_audio.terminal_video_output("auto"), "tct")
                self.assertEqual(bili_audio.terminal_video_output("kitty"), "kitty")

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


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # clear=True would drop PATH, and build_mpv_command has to find mpv.
        environment = mock.patch.dict(
            os.environ,
            {"BILI_AUDIO_CONFIG_DIR": self.directory.name,
             "PATH": os.environ.get("PATH", "")},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        bili_audio._history_warned = False

    @staticmethod
    def video(bvid: str, title: str = "標題") -> bili_audio.Video:
        return bili_audio.Video(
            title=title, author="UP", bvid=bvid, aid="",
            url=f"https://www.bilibili.com/video/{bvid}",
            duration="1:00", play=1, pubdate=None, description="",
        )

    def test_replay_moves_to_the_front_without_duplicating(self) -> None:
        bili_audio.record_history(self.video("BV1111111111", "第一"))
        bili_audio.record_history(self.video("BV2222222222", "第二"))
        bili_audio.record_history(self.video("BV1111111111", "第一重播"))
        entries = bili_audio.load_history()
        self.assertEqual([entry["bvid"] for entry in entries],
                         ["BV1111111111", "BV2222222222"])
        self.assertEqual(entries[0]["title"], "第一重播")

    def test_history_is_capped_and_private(self) -> None:
        for index in range(bili_audio.HISTORY_LIMIT + 30):
            bili_audio.record_history(self.video(f"BV{index:010d}"))
        self.assertEqual(len(bili_audio.load_history()), bili_audio.HISTORY_LIMIT)
        mode = stat.S_IMODE(os.stat(bili_audio.history_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_damaged_file_warns_once_and_still_accepts_writes(self) -> None:
        bili_audio.history_path().parent.mkdir(parents=True, exist_ok=True)
        bili_audio.history_path().write_text("not json at all", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(bili_audio.load_history(), [])
            bili_audio.load_history()
        self.assertEqual(errors.getvalue().count("格式不正確"), 1)
        bili_audio.record_history(self.video("BV3333333333"))
        self.assertEqual([e["bvid"] for e in bili_audio.load_history()],
                         ["BV3333333333"])

    def test_entries_without_a_real_bvid_are_dropped(self) -> None:
        bili_audio.record_history(self.video("not-a-bvid"))
        bili_audio.record_history(self.video(""))
        self.assertEqual(bili_audio.load_history(), [])
        bili_audio.write_history([{"bvid": "../escape", "title": "x"}])
        self.assertEqual(bili_audio.load_history(), [])

    def test_clear_empties_the_list(self) -> None:
        bili_audio.record_history(self.video("BV1111111111"))
        bili_audio.clear_history()
        self.assertEqual(bili_audio.load_history(), [])
        self.assertFalse(bili_audio.history_path().exists())

    def test_history_survives_a_reload(self) -> None:
        bili_audio.record_history(self.video("BV1111111111", "留下來"))
        reloaded = bili_audio.videos_from_history(bili_audio.load_history())
        self.assertEqual(reloaded[0].title, "留下來")
        self.assertEqual(reloaded[0].url,
                         "https://www.bilibili.com/video/BV1111111111")

    def test_playback_records_before_starting_mpv(self) -> None:
        args = bili_audio.parse_args(["hot"])
        with mock.patch("bili_audio.play_audio", return_value=0) as player:
            bili_audio.play_selected(self.video("BV1111111111"), args,
                                     bili_audio.FZF_PLAY_AUDIO)
        player.assert_called_once()
        self.assertEqual(len(bili_audio.load_history()), 1)

    def test_mpv_resumes_through_its_own_watch_later(self) -> None:
        args = bili_audio.parse_args(["hot"])
        for mode in ("audio", "video", "window"):
            command = bili_audio.build_mpv_command(
                self.video("BV1111111111"), args, mode,
                resolved_video_output="tct" if mode == "video" else None,
            )
            self.assertIn("--save-position-on-quit=yes", command)
            self.assertIn(f"--watch-later-directory={bili_audio.watch_later_dir()}",
                          command)


class WatchlaterFavoriteTests(unittest.TestCase):
    """Shapes here mirror what the live API actually returned."""

    def test_watchlater_drops_unplayable_and_keeps_order(self) -> None:
        payload = {"code": 0, "data": {"count": 3, "list": [
            {"bvid": "BV1111111111", "title": "可播", "duration": 346,
             "owner": {"name": "UP"}, "stat": {"view": 10}, "pubdate": 1,
             "desc": "簡介", "state": 0, "progress": 20},
            {"bvid": "BV2222222222", "title": "已下架", "state": -10},
            {"title": "沒有 bvid", "state": 0},
        ]}}
        items, skipped = bili_audio.watchlater_items(payload)
        self.assertEqual(skipped, 2)
        videos = bili_audio.videos_from_items(items)
        self.assertEqual([video.bvid for video in videos], ["BV1111111111"])
        self.assertEqual(videos[0].duration, "05:46")
        self.assertEqual(videos[0].author, "UP")

    def test_watchlater_accepts_an_empty_list(self) -> None:
        self.assertEqual(bili_audio.watchlater_items({"data": {"list": None}}), ([], 0))
        with self.assertRaisesRegex(SystemExit, "格式已變更"):
            bili_audio.watchlater_items({"data": "nonsense"})

    def test_favorite_media_is_reshaped_for_make_video(self) -> None:
        payload = {"code": 0, "data": {"has_more": True, "medias": [
            {"bvid": "BV1111111111", "type": 2, "title": "影片", "intro": "簡介",
             "duration": 130, "upper": {"name": "UP"}, "attr": 0,
             "cnt_info": {"play": 999}, "pubtime": 1700000000},
            {"bvid": "BV2222222222", "type": 2, "title": "已失效", "attr": 9},
            {"bvid": "BV3333333333", "type": 24, "title": "合集", "attr": 0},
        ]}}
        items, skipped, has_more = bili_audio.favorite_media_items(payload)
        self.assertEqual((skipped, has_more), (2, True))
        videos = bili_audio.videos_from_items(items)
        self.assertEqual([video.bvid for video in videos], ["BV1111111111"])
        self.assertEqual(videos[0].play, 999)
        self.assertEqual(videos[0].duration, "02:10")
        self.assertEqual(videos[0].description, "簡介")
        self.assertEqual(videos[0].pubdate, 1700000000)

    def test_empty_folder_answers_with_null_medias(self) -> None:
        items, skipped, has_more = bili_audio.favorite_media_items(
            {"data": {"medias": None, "has_more": False}}
        )
        self.assertEqual((items, skipped, has_more), ([], 0, False))

    def test_folder_list_keeps_only_usable_entries(self) -> None:
        folders = bili_audio.favorite_folders({"data": {"list": [
            {"id": 1, "title": "默认收藏夹", "media_count": 39},
            {"id": 2, "title": "", "media_count": 0},
            {"title": "沒有 id"},
        ]}})
        self.assertEqual(folders, [
            {"id": 1, "title": "默认收藏夹", "media_count": 39},
            {"id": 2, "title": "2", "media_count": 0},
        ])
        self.assertEqual(bili_audio.favorite_folders({"data": {"list": None}}), [])

    def test_both_commands_refuse_without_a_login(self) -> None:
        session = mock.Mock()
        session.status.return_value = {"logged_in": False}
        for command, runner in (
            (["watchlater"], bili_audio.run_watchlater),
            (["favorites"], bili_audio.run_favorites),
        ):
            with self.assertRaisesRegex(SystemExit, "需要有效登入"):
                runner(bili_audio.parse_args(command), session)
        session.request.assert_not_called()


class FakeMpv:
    """A tiny mpv JSON IPC server holding just the properties boss touches."""

    def __init__(self, path: str) -> None:
        import socket
        import threading

        self.state = {"pause": False, "mute": False, "window-minimized": False}
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection:
                buffer = b""
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        command = json.loads(line)["command"]
                        # An async event first, the way real mpv interleaves them.
                        connection.sendall(b'{"event":"property-change"}\n')
                        if command[0] == "get_property":
                            reply = {"error": "success", "data": self.state.get(command[1])}
                        else:
                            self.state[command[1]] = command[2]
                            reply = {"error": "success"}
                        connection.sendall((json.dumps(reply) + "\n").encode())

    def close(self) -> None:
        self.server.close()


class FloatWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        # Short on purpose: macOS caps a socket path at 104 bytes.
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        environment = mock.patch.dict(
            os.environ,
            {"BILI_AUDIO_CONFIG_DIR": self.directory.name,
             "PATH": os.environ.get("PATH", "")},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.video = bili_audio.Video(
            title="不該出現的片名", author="UP", bvid="BV1ABcsztEcY", aid="",
            url="https://www.bilibili.com/video/BV1ABcsztEcY",
            duration="01:00", play=1, pubdate=None, description="",
        )

    def test_float_window_is_pinned_to_the_screen_corner(self) -> None:
        args = bili_audio.parse_args(["hot", "--play-mode", "float"])
        command = bili_audio.build_mpv_command(self.video, args, "float")
        for option in ("--geometry=100%:100%", "--autofit=28%", "--ontop",
                       "--border=no", "--title=zsh", "--focus-on=never",
                       "--macos-app-activation-policy=accessory", "--hwdec=auto-safe"):
            self.assertIn(option, command)
        self.assertIn(f"--input-ipc-server={bili_audio.ipc_socket_path()}", command)
        # Resume and history behave the same as every other mode.
        self.assertIn("--save-position-on-quit=yes", command)
        self.assertFalse([item for item in command if item.startswith("--vo=")])

    def test_float_size_is_configurable_and_bounded(self) -> None:
        args = bili_audio.parse_args(["hot", "--play-mode", "float", "--float-size", "40"])
        self.assertIn("--autofit=40%", bili_audio.build_mpv_command(self.video, args, "float"))
        for bad in ("2", "150"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bili_audio.parse_args(["hot", "--float-size", bad])

    def test_a_long_config_path_skips_ipc_instead_of_breaking_mpv(self) -> None:
        with mock.patch.dict(os.environ, {"BILI_AUDIO_CONFIG_DIR": "/tmp/" + "x" * 120}):
            self.assertIsNone(bili_audio.ipc_socket_path())
            command = bili_audio.build_mpv_command(
                self.video, bili_audio.parse_args(["hot"]), "float"
            )
        self.assertFalse([item for item in command if "ipc-server" in item])

    def test_float_needs_no_tty_and_prints_no_title_or_url(self) -> None:
        args = bili_audio.parse_args(["hot", "--play-mode", "float"])
        with mock.patch("bili_audio.subprocess.run") as runner, \
             mock.patch("bili_audio.find_mpv", return_value="/bin/echo"), \
             mock.patch("bili_audio.terminal_is_interactive", return_value=False), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            runner.return_value = mock.Mock(returncode=0)
            self.assertEqual(bili_audio.play_selected(self.video, args), 0)
        self.assertIn("--geometry=100%:100%", runner.call_args.args[0])
        self.assertNotIn(self.video.url, errors.getvalue())
        self.assertNotIn(self.video.title, errors.getvalue())

    def test_stale_socket_is_removed_before_mpv_starts(self) -> None:
        socket_path = bili_audio.ipc_socket_path()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.write_text("left over")
        args = bili_audio.parse_args(["hot", "--play-mode", "float"])
        with mock.patch("bili_audio.subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch("bili_audio.find_mpv", return_value="/bin/echo"), \
             contextlib.redirect_stderr(io.StringIO()):
            bili_audio.play_video(self.video, args, mode="float")
        self.assertFalse(socket_path.exists())

    def test_boss_without_a_player(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bili_audio.run_boss(bili_audio.parse_args(["boss"])), 1)
        self.assertIn("沒有正在播放", output.getvalue())

    def test_boss_clears_a_socket_nobody_listens_on(self) -> None:
        import socket

        socket_path = bili_audio.ipc_socket_path()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(socket_path))
        dead.close()  # The file stays, but nothing accepts connections.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bili_audio.run_boss(bili_audio.parse_args(["boss"])), 1)
        self.assertFalse(socket_path.exists())

    def test_boss_hides_then_restores_without_touching_a_manual_mute(self) -> None:
        socket_path = bili_audio.ipc_socket_path()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        player = FakeMpv(str(socket_path))
        self.addCleanup(player.close)
        player.state["mute"] = True  # Muted on purpose before hiding.

        args = bili_audio.parse_args(["boss"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bili_audio.run_boss(args), 0)
        self.assertEqual(
            {k: player.state[k] for k in ("pause", "mute", "window-minimized")},
            {"pause": True, "mute": True, "window-minimized": True},
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bili_audio.run_boss(args), 0)
        self.assertEqual(
            {k: player.state[k] for k in ("pause", "mute", "window-minimized")},
            {"pause": False, "mute": True, "window-minimized": False},
        )

    def test_boss_restores_sound_that_it_muted_itself(self) -> None:
        socket_path = bili_audio.ipc_socket_path()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        player = FakeMpv(str(socket_path))
        self.addCleanup(player.close)
        args = bili_audio.parse_args(["boss"])
        with contextlib.redirect_stdout(io.StringIO()):
            bili_audio.run_boss(args)
            self.assertTrue(player.state["mute"])
            bili_audio.run_boss(args)
        self.assertFalse(player.state["mute"])


class CompletionTests(unittest.TestCase):
    def test_commands_match_parser(self) -> None:
        # A new subparser that is missing from COMMANDS gets treated as a
        # search keyword instead of a command, which is silent and confusing.
        parser_names = set(bili_audio.parser_subcommands(bili_audio.build_parser()))
        self.assertEqual(parser_names, bili_audio.COMMANDS)

    def test_table_is_read_off_the_parser(self) -> None:
        table = dict(bili_audio.completion_table())
        self.assertIn("history", table)
        self.assertIn("--clear", table["history"])
        self.assertIn("--stealth", table["recommend"])
        self.assertIn("--max-height", table["hot"])

    def test_scripts_name_every_command(self) -> None:
        for shell in ("zsh", "bash"):
            script = bili_audio.completion_script(shell)
            for name in bili_audio.COMMANDS:
                self.assertIn(name, script, f"{shell} 腳本缺少 {name}")

    def test_list_prints_commands_and_flags(self) -> None:
        args = bili_audio.parse_args(["completion", "--list"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bili_audio.run_completion(args), 0)
        printed = output.getvalue()
        self.assertIn("history", printed)
        self.assertIn("    --clear", printed)

    def test_install_links_the_command_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as prefix:
            args = bili_audio.parse_args(["install", "--prefix", prefix])
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bili_audio.run_install(args), 0)
            link, completion = bili_audio.install_paths(prefix)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), pathlib.Path(bili_audio.__file__).resolve())
            text = completion.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#compdef bili-audio"))
            self.assertIn("history", text)

            # Running it twice must not fail on the existing symlink.
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bili_audio.run_install(args), 0)

            remove = bili_audio.parse_args(
                ["install", "--prefix", prefix, "--uninstall"]
            )
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bili_audio.run_install(remove), 0)
            self.assertFalse(link.exists())
            self.assertFalse(completion.exists())

    def test_autoload_and_sourced_scripts_differ(self) -> None:
        autoload = bili_audio.completion_script("zsh", autoload=True)
        sourced = bili_audio.completion_script("zsh")
        self.assertTrue(autoload.startswith("#compdef"))
        self.assertNotIn("compdef _bili_audio_complete", autoload)
        self.assertIn("compdef _bili_audio_complete", sourced)
        for script in (autoload, sourced):
            self.assertIn("watchlater", script)

    def test_help_text_matches_the_offered_keys(self) -> None:
        full = bili_audio.fzf_help_text(paging=True, playback_keys=True, refresh=True)
        self.assertIn("Ctrl-R", full)
        self.assertIn("Tab", full)
        plain = bili_audio.fzf_help_text(paging=False, playback_keys=False, refresh=False)
        self.assertNotIn("Ctrl-R", plain)
        self.assertNotIn("Tab 多選", plain)


if __name__ == "__main__":
    unittest.main()
