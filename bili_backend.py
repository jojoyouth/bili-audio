"""Adapter for the optional ``bilibili-cli`` executable.

The upstream project exposes a versioned JSON envelope.  Keeping the
integration behind a subprocess boundary lets bili-audio continue to work
without importing (or pinning itself to) the upstream SDK dependency tree.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class BiliCliError(RuntimeError):
    """Raised when the optional upstream CLI cannot satisfy a request."""


def find_bili_cli(explicit: str | None = None) -> str | None:
    """Return an executable path for bilibili-cli, if one is available."""
    if explicit:
        expanded = os.path.abspath(os.path.expanduser(explicit))
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        discovered = shutil.which(explicit)
        if discovered:
            return discovered
        raise BiliCliError(f"找不到指定的 bilibili-cli：{explicit}")

    local = Path(__file__).resolve().parent / ".venv" / "bin" / "bili"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("bili")


def _decode_envelope(stdout: str) -> Any:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BiliCliError("bilibili-cli 沒有回傳有效 JSON。") from exc

    if not isinstance(payload, dict):
        raise BiliCliError("bilibili-cli JSON 格式不正確。")

    schema_version = payload.get("schema_version")
    if schema_version != "1":
        raise BiliCliError(
            "bilibili-cli JSON schema 不相容"
            f"（需要 1，收到 {schema_version!r}）。"
        )

    if payload.get("ok") is False:
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "未知錯誤")
        else:
            message = str(error or "未知錯誤")
        raise BiliCliError(message)
    if payload.get("ok") is not True:
        raise BiliCliError("bilibili-cli JSON 缺少有效的 ok 欄位。")

    if "data" not in payload:
        raise BiliCliError("bilibili-cli JSON 缺少 data 欄位。")
    return payload["data"]


def _run_json(
    executable: str,
    arguments: Sequence[str],
    timeout: float,
) -> Any:
    command = [executable, *arguments, "--json"]
    environment = os.environ.copy()
    environment["OUTPUT"] = "json"

    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise BiliCliError(f"bilibili-cli 逾時（{timeout:g} 秒）。") from exc
    except OSError as exc:
        raise BiliCliError(f"無法執行 bilibili-cli：{exc}") from exc

    if result.returncode in (130, -signal.SIGINT):
        raise KeyboardInterrupt

    # Structured failures are normally printed to stdout, so decode first.
    try:
        data = _decode_envelope(result.stdout)
    except BiliCliError as decode_error:
        if result.returncode != 0 and result.stderr.strip():
            raise BiliCliError(result.stderr.strip()) from decode_error
        raise

    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise BiliCliError(detail)
    return data


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        values = data["items"]
    else:
        raise BiliCliError("bilibili-cli 回傳的清單格式不正確。")
    return [item for item in values if isinstance(item, dict)]


class BiliCliBackend:
    """Small synchronous client for the upstream CLI's public query commands."""

    def __init__(self, executable: str, timeout: float = 20.0):
        self.executable = executable
        self.timeout = timeout

    def search_videos(
        self,
        keyword: str,
        page: int = 1,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        data = _run_json(
            self.executable,
            [
                "search",
                keyword,
                "--type",
                "video",
                "--page",
                str(page),
                "--max",
                str(count),
            ],
            self.timeout,
        )
        return _items(data)

    def search_users(
        self,
        keyword: str,
        page: int = 1,
        count: int = 20,
    ) -> list[dict[str, Any]]:
        data = _run_json(
            self.executable,
            [
                "search",
                keyword,
                "--type",
                "user",
                "--page",
                str(page),
                "--max",
                str(count),
            ],
            self.timeout,
        )
        return _items(data)

    def user(self, uid_or_name: str) -> dict[str, Any]:
        data = _run_json(
            self.executable,
            ["user", uid_or_name],
            self.timeout,
        )
        if not isinstance(data, dict):
            raise BiliCliError("bilibili-cli 回傳的使用者格式不正確。")
        return data

    def user_videos(
        self,
        uid_or_name: str,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        data = _run_json(
            self.executable,
            ["user-videos", uid_or_name, "--max", str(count)],
            self.timeout,
        )
        return _items(data)

    def hot(self, page: int = 1, count: int = 20) -> list[dict[str, Any]]:
        data = _run_json(
            self.executable,
            ["hot", "--page", str(page), "--max", str(count)],
            self.timeout,
        )
        return _items(data)

    def rank(self, count: int = 20) -> list[dict[str, Any]]:
        data = _run_json(
            self.executable,
            ["rank", "--max", str(count)],
            self.timeout,
        )
        return _items(data)
