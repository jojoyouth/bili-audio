#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Callable
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from bili_backend import BiliCliBackend, BiliCliError, find_bili_cli
from bili_auth import Session, login_qr, session_path

SEARCH_API_URL = "https://api.bilibili.com/x/web-interface/search/type"
HOT_API_URL = "https://api.bilibili.com/x/web-interface/popular"
RANK_API_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
USER_CARD_API_URL = "https://api.bilibili.com/x/web-interface/card"
RECOMMEND_API_URL = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"
WATCHLATER_API_URL = "https://api.bilibili.com/x/v2/history/toview"
FAV_FOLDERS_API_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FAV_RESOURCE_API_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
FAV_PAGE_SIZE = 20
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
UNLIMITED_PAGE_COUNT = 100
# Bursts of back-to-back calls are what trips Bilibili risk control, so space them out.
MIN_API_INTERVAL = 0.8
_last_api_call = 0.0
HISTORY_LIMIT = 200
_history_warned = False
OSC_SEQUENCE_RE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)",
    re.DOTALL,
)
ANSI_SEQUENCE_RE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|"
    r"\x1b[@-_]"
)
TERMINAL_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f"
    r"\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_COOKIE_TEMP_DIR: tempfile.TemporaryDirectory[str] | None = None
_AUTH_SESSION: Session | None = None


@dataclasses.dataclass(frozen=True)
class Video:
    title: str
    author: str
    bvid: str
    aid: str
    url: str
    duration: str
    play: Any
    pubdate: int | None
    description: str
    like: Any = None
    score: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "bvid": self.bvid,
            "aid": self.aid,
            "url": self.url,
            "duration": self.duration,
            "play": self.play,
            "pubdate": self.pubdate,
            "pubdate_text": format_pubdate(self.pubdate),
            "description": self.description,
            "like": self.like,
            "score": self.score,
        }


@dataclasses.dataclass(frozen=True)
class UserSummary:
    uid: str
    name: str
    sign: str = ""
    fans: Any = 0
    following: Any = 0
    videos: Any = 0
    level: Any = 0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class FzfResult:
    action: str
    video: Video | None = None
    selected: tuple[Video, ...] = ()


FZF_PLAY = "play"
FZF_PLAY_AUDIO = "play-audio"
FZF_PLAY_VIDEO = "play-video"
FZF_CANCEL = "cancel"
FZF_NEXT_PAGE = "next-page"
FZF_PREV_PAGE = "prev-page"
FZF_REFRESH = "refresh"


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def is_interrupt_returncode(returncode: int) -> bool:
    return returncode in (130, -signal.SIGINT)


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = OSC_SEQUENCE_RE.sub("", text)
    text = ANSI_SEQUENCE_RE.sub("", text)
    text = TERMINAL_CONTROL_RE.sub("", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def table_field(value: Any) -> str:
    return clean_text(value).replace("\t", " ").replace("\n", " ")


def format_count(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value or "-")

    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}億"
    if number >= 10_000:
        return f"{number / 10_000:.1f}萬"
    return str(number)


def format_pubdate(value: int | None) -> str:
    if not value:
        return "-"
    local_timezone = dt.datetime.now().astimezone().tzinfo
    return dt.datetime.fromtimestamp(value, tz=local_timezone).strftime("%Y-%m-%d")


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_duration(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if ":" in stripped:
            return stripped
        if not stripped:
            return "-"
        seconds = to_int(stripped, -1)
    else:
        seconds = to_int(value, -1)

    if seconds < 0:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def make_video(item: dict[str, Any]) -> Video | None:
    bvid = str(item.get("bvid") or "").strip()
    aid_value = item.get("aid")
    if not aid_value and not bvid:
        id_value = str(item.get("id") or "").strip()
        aid_value = id_value if id_value.isdigit() else ""
    aid = str(aid_value or "").strip()
    arcurl = str(item.get("arcurl") or "").strip()
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    upper = item.get("upper") if isinstance(item.get("upper"), dict) else {}
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}

    if bvid:
        url = str(item.get("url") or f"https://www.bilibili.com/video/{bvid}")
    elif arcurl:
        url = arcurl.replace("http://", "https://", 1)
    else:
        return None

    pubdate = item.get("pubdate") or item.get("ctime") or item.get("created")
    if not isinstance(pubdate, int):
        pubdate = None

    author = (
        item.get("author")
        or owner.get("name")
        or owner.get("uname")
        or upper.get("name")
        or ""
    )
    play = (
        item.get("play")
        if item.get("play") is not None
        else stats.get("view", stat.get("view"))
    )
    like = (
        item.get("like")
        if item.get("like") is not None
        else stats.get("like", stat.get("like"))
    )

    return Video(
        title=clean_text(item.get("title")),
        author=clean_text(author),
        bvid=bvid,
        aid=aid,
        url=url,
        duration=format_duration(
            item.get("duration", item.get("duration_seconds", item.get("length")))
        ),
        play=play,
        pubdate=pubdate,
        description=clean_text(item.get("description") or item.get("desc")),
        like=like,
        score=item.get("score"),
    )


def make_user(item: dict[str, Any]) -> UserSummary | None:
    user = item.get("user") if isinstance(item.get("user"), dict) else item
    relation = (
        item.get("relation") if isinstance(item.get("relation"), dict) else {}
    )
    level_info = (
        user.get("level_info") if isinstance(user.get("level_info"), dict) else {}
    )
    uid = str(user.get("id") or user.get("mid") or "").strip()
    name = clean_text(user.get("name") or user.get("username") or user.get("uname"))
    if not uid and not name:
        return None
    return UserSummary(
        uid=uid,
        name=name,
        sign=clean_text(user.get("sign") or user.get("usign")),
        fans=user.get(
            "fans",
            relation.get("follower", item.get("follower", 0)),
        ),
        following=relation.get(
            "following",
            user.get("friend", user.get("attention", 0)),
        ),
        videos=user.get("videos", 0),
        level=user.get("level", level_info.get("current_level", 0)),
    )


def pace_api_calls() -> None:
    global _last_api_call
    remaining = MIN_API_INTERVAL - (time.monotonic() - _last_api_call)
    if remaining > 0:
        time.sleep(remaining)
    _last_api_call = time.monotonic()


def terminal_cells() -> tuple[int, int] | None:
    for stream in (sys.stdout, sys.stderr):
        try:
            packed = fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        except (OSError, ValueError, AttributeError):
            continue
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        if cols and rows:
            return cols, rows
    return None


def request_headers() -> dict[str, str]:
    headers = {
        "User-Agent": os.environ.get("BILI_USER_AGENT", DEFAULT_USER_AGENT),
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json,text/plain,*/*",
    }

    cookie = os.environ.get("BILIBILI_COOKIE") or os.environ.get("BILI_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    return headers


def config_dir() -> Path:
    return session_path().parent


def history_path() -> Path:
    return config_dir() / "history.json"


def watch_later_dir() -> Path:
    """mpv keeps resume positions here, keyed by the video URL."""
    return config_dir() / "watch-later"


def load_history() -> list[dict[str, Any]]:
    """A damaged or hand-edited file must not take the whole command down."""
    global _history_warned
    try:
        raw = history_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        if not _history_warned:
            eprint("無法讀取本機播放紀錄，這次先當成空清單。")
            _history_warned = True
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        if not _history_warned:
            eprint(f"本機播放紀錄格式不正確，已忽略：{history_path()}")
            _history_warned = True
        return []
    if not isinstance(entries, list):
        return []
    clean = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        bvid = entry.get("bvid")
        if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            continue
        clean.append({
            "bvid": bvid,
            "title": clean_text(str(entry.get("title", ""))),
            "author": clean_text(str(entry.get("author", ""))),
            "started": entry.get("started") if isinstance(entry.get("started"), str) else "",
        })
    return clean[:HISTORY_LIMIT]


def write_history(entries: list[dict[str, Any]]) -> None:
    target = history_path()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".history-", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(entries[:HISTORY_LIMIT], output, ensure_ascii=False, indent=1)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_history(video: Video) -> None:
    """Records that playback started. Never blocks playback, and never claims it finished."""
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", video.bvid or ""):
        return
    entry = {
        "bvid": video.bvid,
        "title": video.title,
        "author": video.author,
        "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        entries = [item for item in load_history() if item["bvid"] != video.bvid]
        write_history([entry] + entries)
    except OSError as exc:
        eprint(f"播放紀錄寫入失敗，不影響播放：{exc}")


def clear_history() -> int:
    try:
        history_path().unlink(missing_ok=True)
    except OSError as exc:
        raise SystemExit("無法清除本機播放紀錄，請確認設定目錄權限。") from exc
    return 0


def videos_from_history(entries: list[dict[str, Any]]) -> list[Video]:
    return [
        Video(
            title=entry["title"] or entry["bvid"],
            author=entry["author"],
            bvid=entry["bvid"],
            aid="",
            url=f"https://www.bilibili.com/video/{entry['bvid']}",
            duration="",
            play=0,
            pubdate=None,
            description=f"上次播放：{entry['started']}" if entry["started"] else "",
        )
        for entry in entries
    ]


def env_cookie() -> str | None:
    return os.environ.get("BILIBILI_COOKIE") or os.environ.get("BILI_COOKIE")


def cookie_jar_path() -> str:
    override = os.environ.get("BILI_COOKIE_JAR")
    if override:
        return os.path.abspath(os.path.expanduser(override))

    global _COOKIE_TEMP_DIR
    if _COOKIE_TEMP_DIR is None:
        _COOKIE_TEMP_DIR = tempfile.TemporaryDirectory(
            prefix="bili_audio_cookie_"
        )
    return os.path.join(_COOKIE_TEMP_DIR.name, "cookies.txt")


def fetch_json_url(url: str, timeout: float) -> dict[str, Any]:
    if _AUTH_SESSION is not None:
        return _AUTH_SESSION.request(url, timeout)
    if shutil.which("curl"):
        return fetch_page_with_curl(url, timeout)
    return fetch_page_with_urllib(url, timeout)


def fetch_search_page(
    keyword: str,
    page: int,
    timeout: float,
    search_type: str = "video",
) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "search_type": search_type,
            "keyword": keyword,
            "page": page,
        }
    )
    return fetch_json_url(f"{SEARCH_API_URL}?{params}", timeout)


def fetch_page(keyword: str, page: int, timeout: float) -> dict[str, Any]:
    """Backward-compatible direct video search helper."""
    return fetch_search_page(keyword, page, timeout, "video")


def direct_hot(page: int, count: int, timeout: float) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"pn": page, "ps": count})
    payload = fetch_json_url(f"{HOT_API_URL}?{params}", timeout)
    require_api_success(payload)
    results = payload.get("data", {}).get("list", [])
    return [item for item in results if isinstance(item, dict)]


def direct_rank(timeout: float) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"rid": 0, "type": "all"})
    payload = fetch_json_url(f"{RANK_API_URL}?{params}", timeout)
    require_api_success(payload)
    results = payload.get("data", {}).get("list", [])
    return [item for item in results if isinstance(item, dict)]


def direct_search_users(
    keyword: str,
    page: int,
    timeout: float,
) -> list[dict[str, Any]]:
    payload = fetch_search_page(keyword, page, timeout, "bili_user")
    require_api_success(payload)
    results = payload.get("data", {}).get("result", [])
    return [item for item in results if isinstance(item, dict)]


def direct_user_card(uid: str, timeout: float) -> dict[str, Any]:
    params = urllib.parse.urlencode({"mid": uid, "photo": "true"})
    payload = fetch_json_url(f"{USER_CARD_API_URL}?{params}", timeout)
    require_api_success(payload)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit("Bilibili API 沒有回傳使用者資料。")
    card = data.get("card")
    if not isinstance(card, dict):
        raise SystemExit("Bilibili API 沒有回傳使用者名片。")
    merged = dict(card)
    if not merged.get("videos"):
        merged["videos"] = data.get("archive_count", 0)
    return merged


def require_api_success(payload: dict[str, Any]) -> None:
    code = payload.get("code")
    if code == 0:
        return
    if code in (-412, 412):
        raise SystemExit(
            "Bilibili 回應 412，通常是反爬檢查。可以稍後再試，"
            "或設定 BILIBILI_COOKIE 後重跑。"
        )
    if code in (-429, 429):
        raise SystemExit(
            "Bilibili 回應 429，請降低查詢頻率並稍後再試。"
        )
    if code == -101:
        raise SystemExit("登入已失效，請重新執行 login。")
    message = clean_text(payload.get("message") or payload.get("msg") or "unknown error")
    raise SystemExit(f"Bilibili API 錯誤：{message}")


def fetch_page_with_curl(url: str, timeout: float) -> dict[str, Any]:
    warmed = False
    while True:
        result = run_curl_json(url, timeout)
        if is_interrupt_returncode(result.returncode):
            raise KeyboardInterrupt

        if result.returncode != 0:
            if (
                "412" in result.stderr
                and not env_cookie()
                and not warmed
            ):
                warm_bilibili_cookie_jar(timeout)
                warmed = True
                continue
            if "412" in result.stderr:
                raise SystemExit(
                    "Bilibili 回應 412，通常是反爬檢查。可以稍後再試，"
                    "或設定 BILIBILI_COOKIE 後重跑。"
                )
            if "429" in result.stderr:
                raise SystemExit(
                    "Bilibili 回應 429，請降低查詢頻率並稍後再試。"
                )
            detail = result.stderr.strip() or f"curl exit {result.returncode}"
            raise SystemExit(f"連不上 Bilibili API：{detail}")

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SystemExit("Bilibili API 回傳不是有效 JSON。") from exc

        if (
            payload.get("code") in (-412, 412)
            and not env_cookie()
            and not warmed
        ):
            warm_bilibili_cookie_jar(timeout)
            warmed = True
            continue
        return payload


def run_curl_json(url: str, timeout: float) -> subprocess.CompletedProcess[str]:
    headers = request_headers()
    cookie = headers.pop("Cookie", None)
    command = [
        "curl",
        "-sS",
        "-L",
        "--fail",
        "--max-time",
        str(timeout),
        "-A",
        headers.pop("User-Agent"),
    ]
    for key, value in headers.items():
        command.extend(["-H", f"{key}: {value}"])
    standard_input: str | None = None
    if cookie:
        command.extend(["-H", "@-"])
        standard_input = f"Cookie: {cookie}\n"
    else:
        command.extend(["-b", cookie_jar_path(), "-c", cookie_jar_path()])
    command.append(url)

    return subprocess.run(
        command,
        input=standard_input,
        text=True,
        capture_output=True,
        check=False,
    )


def warm_bilibili_cookie_jar(timeout: float) -> None:
    headers = request_headers()
    command = [
        "curl",
        "-sS",
        "-L",
        "--max-time",
        str(timeout),
        "-A",
        headers["User-Agent"],
        "-c",
        cookie_jar_path(),
        "https://www.bilibili.com/",
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, check=False)


def fetch_page_with_urllib(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=request_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            raise SystemExit(
                "Bilibili 回應 412，通常是反爬檢查。可以稍後再試，"
                "或設定 BILIBILI_COOKIE 後重跑。"
            ) from exc
        if exc.code == 429:
            raise SystemExit(
                "Bilibili 回應 429，請降低查詢頻率並稍後再試。"
            ) from exc
        raise SystemExit(
            f"Bilibili API HTTP 錯誤：{exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"連不上 Bilibili API：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit("Bilibili API 回傳不是有效 JSON。") from exc


class BiliSource:
    """Use bilibili-cli when available, with public API fallbacks."""

    def __init__(self, args: argparse.Namespace):
        self.mode = args.backend
        self.timeout = args.timeout
        self._warned = False
        self.upstream: BiliCliBackend | None = None

        try:
            executable = find_bili_cli(args.bili_cli)
        except BiliCliError as exc:
            raise SystemExit(str(exc)) from exc

        if self.mode == "bili-cli" and not executable:
            raise SystemExit(
                "找不到 bilibili-cli 的 bili 指令。請先安裝，"
                "或改用 --backend direct。"
            )
        if self.mode != "direct" and executable:
            self.upstream = BiliCliBackend(executable, timeout=self.timeout)

    @property
    def backend_name(self) -> str:
        return "bilibili-cli" if self.upstream else "direct"

    def _fallback(self, action: str, error: BiliCliError) -> None:
        if self.mode == "bili-cli":
            raise SystemExit(f"bilibili-cli {action}失敗：{error}") from error
        self.upstream = None
        if not self._warned:
            eprint(f"bilibili-cli {action}失敗，改用公開 API：{error}")
            self._warned = True

    def search_videos(self, keyword: str, page: int) -> list[dict[str, Any]]:
        if self.upstream:
            try:
                return self.upstream.search_videos(keyword, page=page, count=50)
            except BiliCliError as exc:
                self._fallback("搜尋", exc)

        payload = fetch_page(keyword, page, self.timeout)
        require_api_success(payload)
        results = payload.get("data", {}).get("result", [])
        return [item for item in results if isinstance(item, dict)]

    def search_users(self, keyword: str, page: int = 1) -> list[dict[str, Any]]:
        if self.upstream:
            try:
                return self.upstream.search_users(keyword, page=page, count=20)
            except BiliCliError as exc:
                self._fallback("使用者搜尋", exc)
        return direct_search_users(keyword, page, self.timeout)

    def user(self, uid_or_name: str) -> UserSummary:
        if self.upstream:
            try:
                user = make_user(self.upstream.user(uid_or_name))
                if user:
                    return user
                raise BiliCliError("回傳內容缺少使用者資料")
            except BiliCliError as exc:
                self._fallback("使用者資料查詢", exc)

        candidate: UserSummary | None = None
        uid = uid_or_name
        if not uid_or_name.isdigit():
            results = self.search_users(uid_or_name)
            if not results:
                raise SystemExit(f"找不到使用者：{uid_or_name}")
            candidate = make_user(results[0])
            uid = str(results[0].get("id") or results[0].get("mid") or "")
            if not uid and candidate:
                uid = candidate.uid
        if not uid:
            raise SystemExit(f"找不到使用者：{uid_or_name}")

        try:
            card = direct_user_card(uid, self.timeout)
        except SystemExit:
            if candidate:
                return candidate
            raise
        user = make_user(card)
        if not user:
            raise SystemExit(f"找不到使用者：{uid_or_name}")
        return user

    def user_videos(
        self,
        uid_or_name: str,
        count: int,
    ) -> list[dict[str, Any]]:
        if self.upstream:
            try:
                return self.upstream.user_videos(uid_or_name, count=count)
            except BiliCliError as exc:
                self._fallback("使用者影片查詢", exc)
        raise SystemExit(
            "使用者影片列表需要 bilibili-cli 後端。"
            "請安裝 bili 指令，或用一般 search 搜尋影片。"
        )

    def hot(self, page: int, count: int) -> list[dict[str, Any]]:
        if self.upstream:
            try:
                return self.upstream.hot(page=page, count=count)
            except BiliCliError as exc:
                self._fallback("熱門影片查詢", exc)
        return direct_hot(page, count, self.timeout)

    def rank(self, count: int) -> list[dict[str, Any]]:
        if self.upstream:
            try:
                return self.upstream.rank(count=count)
            except BiliCliError as exc:
                self._fallback("排行榜查詢", exc)
        return direct_rank(self.timeout)[:count]


def video_key(video: Video) -> str:
    return video.bvid or video.aid or video.url


def fetch_video_page(
    source: BiliSource,
    keyword: str,
    page: int,
    seen: set[str],
) -> tuple[list[Video], bool]:
    results = source.search_videos(keyword, page)
    if not results:
        return [], False

    videos: list[Video] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        video = make_video(item)
        if not video:
            continue
        key = video_key(video)
        if key in seen:
            continue
        seen.add(key)
        videos.append(video)

    return videos, bool(videos)


def search_video_pages(
    source: BiliSource,
    keyword: str,
    pages: int,
) -> list[list[Video]]:
    video_pages: list[list[Video]] = []
    seen: set[str] = set()

    for page in range(1, pages + 1):
        videos, has_results = fetch_video_page(source, keyword, page, seen)
        if not has_results:
            break
        video_pages.append(videos)

    return video_pages


def flatten_video_pages(video_pages: list[list[Video]], limit: int = 0) -> list[Video]:
    videos = [video for page in video_pages for video in page]
    if limit:
        return videos[:limit]
    return videos


def limit_video_pages(
    video_pages: list[list[Video]],
    limit: int,
) -> list[list[Video]]:
    if not limit:
        return video_pages

    remaining = limit
    limited: list[list[Video]] = []
    for page in video_pages:
        selected = page[:remaining]
        if selected:
            limited.append(selected)
        remaining -= len(selected)
        if remaining <= 0:
            break
    return limited


def search_videos(source: BiliSource, keyword: str, pages: int) -> list[Video]:
    return flatten_video_pages(search_video_pages(source, keyword, pages))


def render_fzf_rows(videos: list[Video]) -> str:
    rows = []
    for index, video in enumerate(videos):
        rows.append(
            "\t".join(
                [
                    str(index),
                    table_field(video.title),
                    table_field(video.author or "-"),
                    table_field(video.duration),
                    format_count(video.play),
                    format_count(video.like),
                    format_pubdate(video.pubdate),
                    table_field(video.bvid or video.aid),
                    table_field(video.description or "沒有影片簡介"),
                ]
            )
        )
    return "\n".join(rows) + "\n"


def parse_fzf_result(output: str, videos: list[Video]) -> FzfResult:
    lines = output.splitlines()
    if not lines:
        return FzfResult(FZF_CANCEL)

    key = lines[0].strip()
    if key in ("ctrl-f", "alt-n"):
        return FzfResult(FZF_NEXT_PAGE)
    if key in ("ctrl-b", "alt-p"):
        return FzfResult(FZF_PREV_PAGE)
    if key == "ctrl-r":
        return FzfResult(FZF_REFRESH)
    if key == "ctrl-a":
        action = FZF_PLAY_AUDIO
    elif key in ("ctrl-v", "alt-v"):
        action = FZF_PLAY_VIDEO
    else:
        action = FZF_PLAY

    try:
        rows = lines[1:] if len(lines) > 1 else lines
        indexes = [int(row.split("\t", 1)[0].strip()) for row in rows]
        if any(index < 0 or index >= len(videos) for index in indexes):
            raise IndexError
        selected = tuple(videos[index] for index in indexes)
        return FzfResult(action, selected[0], selected)
    except (ValueError, IndexError):
        raise SystemExit("fzf 回傳了無法解析的選項。")


FZF_HELP_ENV = "BILI_AUDIO_FZF_HELP"
FZF_PREVIEW = (
    "printf '%s\\n作者：%s | 長度：%s | 觀看：%s\\n%s\\n' {2} {3} {4} {5} {9}"
)


def fzf_help_text(paging: bool, playback_keys: bool, refresh: bool) -> str:
    lines = ["清單按鍵", "  Enter 播放    Esc 取消    ? 這份說明"]
    if playback_keys:
        lines.append("  Tab 多選連播  Ctrl-A 只聽音訊  Ctrl-V 終端影片  Alt-V 視窗播放")
    if paging:
        lines.append("  Ctrl-F／Alt-N 下一頁    Ctrl-B／Alt-P 上一頁")
    if refresh:
        lines.append("  Ctrl-R 換一批")
    lines += [
        "",
        "常用參數",
        "  --play-mode audio|video|window   --video-output kitty|tct",
        "  --stealth 隱蔽播放（mpv 內按 b 收畫面）   --max-height 480 降來源畫質",
        "  --volume 0-100   --audio-device 裝置   -n／--limit 筆數   --json",
        "",
        "全部參數：./bili_audio.py completion --list",
        "shell 補全：eval \"$(./bili_audio.py completion zsh)\"",
        "",
        "移動游標即可回到影片簡介。",
    ]
    return "\n".join(lines)


def run_fzf(
    videos: list[Video],
    header: str,
    paging: bool = False,
    playback_keys: bool = False,
    refresh: bool = False,
) -> FzfResult:
    if not shutil.which("fzf"):
        raise SystemExit("找不到 fzf。可以先安裝：brew install fzf")

    expected_keys = "enter"
    if playback_keys:
        expected_keys = "enter,ctrl-a,ctrl-v,alt-v"
    if paging:
        expected_keys += ",ctrl-f,ctrl-b,alt-n,alt-p"
    if refresh:
        expected_keys += ",ctrl-r"

    fzf = [
        "fzf",
        "--delimiter=\t",
        "--with-nth=2,3,4,5,6,7,8",
        f"--expect={expected_keys}",
        "--prompt=Bilibili> ",
        "--height=85%",
        "--layout=reverse",
        "--border",
        f"--header={header}",
        f"--preview={FZF_PREVIEW}",
        "--preview-window=down,4,wrap",
        # ? swaps the preview for a key and parameter reference; moving restores it.
        f"--bind=?:change-preview-window(down,16,wrap)+change-preview:"
        f'printf \'%s\\n\' "${FZF_HELP_ENV}"',
        f"--bind=up:up+change-preview-window(down,4,wrap)+change-preview:{FZF_PREVIEW}",
        f"--bind=down:down+change-preview-window(down,4,wrap)+change-preview:{FZF_PREVIEW}",
    ]
    if playback_keys:
        fzf.append("--multi")

    result = subprocess.run(
        fzf,
        input=render_fzf_rows(videos),
        text=True,
        stdout=subprocess.PIPE,
        check=False,
        env={**os.environ, FZF_HELP_ENV: fzf_help_text(paging, playback_keys, refresh)},
    )
    if result.returncode not in (0, 1, 130, -signal.SIGINT):
        raise SystemExit(f"fzf 無法開啟清單（exit {result.returncode}），請確認 terminal 可用。")
    if result.returncode != 0 or not result.stdout.strip():
        return FzfResult(FZF_CANCEL)

    return parse_fzf_result(result.stdout, videos)


def choose_with_fzf(videos: list[Video]) -> Video | None:
    result = run_fzf(videos, "Enter 播放 / Esc 取消 / ? 說明")
    if result.action not in (FZF_PLAY, FZF_PLAY_AUDIO, FZF_PLAY_VIDEO):
        return None
    return result.video


def print_json(videos: list[Video]) -> None:
    print(json.dumps([video.as_dict()
          for video in videos], ensure_ascii=False, indent=2))


def print_user_json(user: UserSummary, videos: list[Video] | None = None) -> None:
    payload: dict[str, Any] = {"user": user.as_dict()}
    if videos is not None:
        payload["videos"] = [video.as_dict() for video in videos]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def print_user(user: UserSummary) -> None:
    print(f"{user.name}（UID: {user.uid or '-'}）")
    print(
        "  "
        f"等級 {user.level or '-'}  "
        f"粉絲 {format_count(user.fans)}  "
        f"關注 {format_count(user.following)}  "
        f"影片 {format_count(user.videos)}"
    )
    if user.sign:
        print(f"  {user.sign}")


def require_command(command: str, install_hint: str) -> str:
    path = shutil.which(command)
    if path:
        return path
    raise SystemExit(f"找不到 {command}。可以先安裝：{install_hint}")


def find_mpv(explicit: str | None = None) -> str:
    if explicit:
        expanded = os.path.expanduser(explicit)
        if os.path.sep in expanded:
            candidate = os.path.abspath(expanded)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            candidate = shutil.which(explicit)
            if candidate:
                return candidate
        raise SystemExit(f"找不到指定的 mpv 或檔案無法執行：{explicit}")

    candidate = shutil.which("mpv")
    if candidate:
        return candidate
    raise SystemExit("找不到 mpv。可以先安裝：brew install mpv")


def yt_dlp_cookie_args(args: argparse.Namespace) -> list[str]:
    result: list[str] = []
    if args.cookies_from_browser:
        result.extend(["--cookies-from-browser", args.cookies_from_browser])
    if args.cookies:
        result.extend(["--cookies", args.cookies])
    return result


def mpv_ytdl_options(args: argparse.Namespace) -> list[str]:
    options: list[str] = []
    if args.cookies_from_browser:
        options.append(
            "--ytdl-raw-options-append="
            f"cookies-from-browser={args.cookies_from_browser}"
        )
    if args.cookies:
        options.append(f"--ytdl-raw-options-append=cookies={args.cookies}")
    return options


def terminal_video_output(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    in_multiplexer = any(
        os.environ.get(name) for name in ("TMUX", "STY", "ZELLIJ")
    )
    if (
        not in_multiplexer
        and (
            os.environ.get("KITTY_WINDOW_ID")
            or "kitty" in term
            or term_program == "kitty"
        )
    ):
        return "kitty"
    return "tct"


def terminal_supports_kitty_shm() -> bool:
    """Only kitty itself reads frames out of shared memory. Ghostty and WezTerm
    speak the graphics protocol but ignore the shm transmission, so enabling it
    there leaves the picture blank instead of making it faster."""
    if any(os.environ.get(name) for name in ("TMUX", "STY", "ZELLIJ")):
        return False
    return bool(
        os.environ.get("KITTY_WINDOW_ID")
        or os.environ.get("TERM", "").lower() == "xterm-kitty"
        or os.environ.get("TERM_PROGRAM", "").lower() == "kitty"
    )


def resolve_kitty_shm(requested: str) -> bool:
    if requested == "yes":
        return True
    if requested == "no":
        return False
    return terminal_supports_kitty_shm()


def probe_mpv_video_outputs(mpv: str) -> set[str]:
    try:
        result = subprocess.run(
            [mpv, "--no-config", "--vo=help"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise SystemExit(f"無法執行 mpv：{exc}") from exc
    output = f"{result.stdout}\n{result.stderr}"
    return {
        name
        for name in ("tct", "kitty", "sixel")
        if re.search(rf"(?m)^\s*{re.escape(name)}\s", output)
    }


def resolve_mpv_video_output(mpv: str, requested: str) -> str:
    available = probe_mpv_video_outputs(mpv)
    if requested != "auto":
        if requested not in available:
            raise SystemExit(
                f"這份 mpv 沒有 {requested} video output；"
                "請執行 mpv --vo=help 檢查可用項目。"
            )
        return requested

    detected = terminal_video_output("auto")
    if detected in available:
        return detected
    if "tct" in available:
        return "tct"
    raise SystemExit(
        "這份 mpv 沒有可用的 terminal video output（需要 tct 或 kitty）。"
    )


def terminal_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def terminal_ytdl_format(args: argparse.Namespace) -> str:
    """Highest available stream by default; --max-height trades detail for throughput."""
    max_height = getattr(args, "max_height", 0) or 0
    if max_height <= 0:
        return "bestvideo+bestaudio/best"
    return f"bestvideo[height<=?{max_height}]+bestaudio/best"


def stealth_kitty_placement() -> list[str]:
    """A quarter-size picture parked in the bottom-right corner."""
    cells = terminal_cells()
    if not cells:
        return ["--vo-kitty-cols=24", "--vo-kitty-rows=10"]
    cols, rows = cells
    width = max(16, cols // 4)
    height = max(6, rows // 4)
    return [
        f"--vo-kitty-cols={width}",
        f"--vo-kitty-rows={height}",
        f"--vo-kitty-left={max(1, cols - width)}",
        f"--vo-kitty-top={max(1, rows - height)}",
    ]


def watch_later_directory_for_mpv() -> str:
    directory = watch_later_dir()
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as exc:
        eprint(f"無法建立續播位置目錄，這次不保存播放位置：{exc}")
    return str(directory)


def build_mpv_command(
    video: Video,
    args: argparse.Namespace,
    mode: str,
    resolved_video_output: str | None = None,
    mpv_path: str | None = None,
) -> list[str]:
    mpv = mpv_path or find_mpv(args.mpv)

    command = [
        mpv,
        "--force-window=no",
        "--keep-open=no",
        "--referrer=https://www.bilibili.com/",
        f"--user-agent={os.environ.get('BILI_USER_AGENT', DEFAULT_USER_AGENT)}",
        f"--volume={args.volume}",
        "--mute=no",
        "--aid=auto",
        "--osd-duration=2000",
        "--input-conf=" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpv-input.conf"),
        f"--audio-device={getattr(args, 'audio_device', 'auto')}",
        f"--network-timeout={getattr(args, 'timeout', 20)}",
        f"--ytdl-raw-options-append=socket-timeout={getattr(args, 'timeout', 20)}",
        "--ytdl-raw-options-append=retries=1",
        # mpv's own resume: it saves on quit and drops the file when a video plays out,
        # so nothing here ever marks a video as finished on our behalf.
        "--save-position-on-quit=yes",
        f"--watch-later-directory={watch_later_directory_for_mpv()}",
        *mpv_ytdl_options(args),
    ]
    if mode == "audio":
        command.append("--no-video")
    elif mode == "window":
        command.extend(["--no-config", "--force-window=yes", "--osc=yes",
                        "--osd-level=1", "--ytdl-format=bestvideo+bestaudio/best"])
    else:
        video_output = resolved_video_output or terminal_video_output(
            args.video_output
        )
        stealth = getattr(args, "stealth", False)
        command.extend(
            [
                "--no-config",
                f"--vo={video_output}",
                "--profile=sw-fast",
                # CoreText probes a reserved PingFangUI.ttc on macOS and logs an
                # error each time before falling back to a font that works.
                "--msg-level=all=info,osd/libass=fatal",
                f"--ytdl-format={terminal_ytdl_format(args)}",
                "--osc=no",
                "--term-status-msg=",
            ]
        )
        if video_output == "kitty":
            # Shared memory keeps whole frames off the PTY: base64 costs ~230 KB per
            # frame. Only kitty reads it, so anywhere else this must stay off or the
            # picture never appears.
            use_shm = resolve_kitty_shm(getattr(args, "kitty_shm", "auto"))
            command.append(f"--vo-kitty-use-shm={'yes' if use_shm else 'no'}")
        if video_output == "tct":
            command.append("--vo-tct-buffering=frame")
        if stealth:
            command.extend([
                "--osd-level=0",
                "--term-playing-msg=",
                "--term-title=",
            ])
            if video_output == "kitty":
                command.extend(stealth_kitty_placement())
        else:
            command.extend([
                "--osd-level=1",
                "--osd-msg1=${playback-time} / ${duration} | 音量 ${volume}% | ${?mute==yes:靜音}${?mute==no:有聲} | ${?pause==yes:暫停}${?pause==no:播放中}",
            ])
    command.extend(["--", video.url])
    return command


def print_direct_url(video: Video, args: argparse.Namespace) -> int:
    yt_dlp = require_command("yt-dlp", "brew install yt-dlp")
    command = [
        yt_dlp,
        "--no-playlist",
        "-f",
        args.format,
        *yt_dlp_cookie_args(args),
        "--get-url",
        video.url,
    ]
    returncode = subprocess.run(command, check=False).returncode
    if is_interrupt_returncode(returncode):
        raise KeyboardInterrupt
    return returncode


def play_audio(video: Video, args: argparse.Namespace) -> int:
    command = build_mpv_command(video, args, "audio")
    eprint(f"播放音訊：{video.title}")
    eprint(video.url)
    try:
        returncode = subprocess.run(command, check=False).returncode
    except OSError as exc:
        raise SystemExit(f"無法執行 mpv：{exc}") from exc
    if is_interrupt_returncode(returncode):
        raise KeyboardInterrupt
    return returncode


def play_video(video: Video, args: argparse.Namespace, mode: str = "video") -> int:
    if mode == "video" and not terminal_is_interactive():
        raise SystemExit("terminal 影片模式需要互動式 TTY，不能輸出到 pipe。")
    mpv = find_mpv(args.mpv)
    video_output = resolve_mpv_video_output(mpv, args.video_output) if mode == "video" else None
    command = build_mpv_command(
        video,
        args,
        mode,
        resolved_video_output=video_output,
        mpv_path=mpv,
    )
    eprint(f"終端播放影片（{video_output}）：{video.title}" if mode == "video"
           else f"視窗播放影片：{video.title}")
    eprint("正在解析與載入影片；若持續沒有畫面，按 Ctrl-C 中止。")
    if not getattr(args, "stealth", False):
        if video_output == "kitty":
            eprint("若 kitty 沒有畫面，可重開並指定 --video-output tct。")
        eprint("mpv：Space 暫停、←/→ 快退快進、9/0 音量、m 靜音、b 收畫面、q 離開")
    if mode == "video":
        eprint("terminal 內用鍵盤控制；需要滑鼠控制列請改用 --play-mode window。")
    eprint(video.url)
    try:
        returncode = subprocess.run(command, check=False).returncode
    except OSError as exc:
        raise SystemExit(f"無法執行 mpv：{exc}") from exc
    if is_interrupt_returncode(returncode):
        raise KeyboardInterrupt
    return returncode


def play_selected(
    video: Video,
    args: argparse.Namespace,
    action: str = FZF_PLAY,
) -> int:
    record_history(video)
    if action == FZF_PLAY_AUDIO:
        mode = "audio"
    elif action == FZF_PLAY_VIDEO:
        mode = "video"
    else:
        mode = args.play_mode
    if mode in ("video", "window"):
        return play_video(video, args) if mode == "video" else play_video(video, args, mode="window")
    return play_audio(video, args)


def seen_video_keys(video_pages: list[list[Video]]) -> set[str]:
    return {video_key(video) for page in video_pages for video in page}


def load_next_video_page(
    source: BiliSource,
    keyword: str,
    args: argparse.Namespace,
    video_pages: list[list[Video]],
) -> bool:
    next_page = len(video_pages) + 1
    videos, has_results = fetch_video_page(
        source,
        keyword,
        next_page,
        seen_video_keys(video_pages),
    )
    if not has_results:
        return False
    video_pages.append(videos)
    return True


def interactive_playback(
    args: argparse.Namespace,
    video_pages: list[list[Video]],
    title: str,
    source: BiliSource | None = None,
    keyword: str | None = None,
    page_loader: Callable[[int], list[Video]] | None = None,
) -> int:
    current_page = 0
    can_load_pages = page_loader is not None or (source is not None and keyword is not None)
    can_page = len(video_pages) > 1 or can_load_pages

    while True:
        videos = video_pages[current_page]
        if args.limit:
            videos = videos[: args.limit]
        controls = "Tab 多選 / Enter 播放 / Ctrl-A 音訊 / Ctrl-V 影片\nEsc 離開 / ? 說明"
        if can_page:
            controls += " / Ctrl-F 下一頁 / Ctrl-B 上一頁"
        if page_loader:
            controls += " / Ctrl-R 換一批"
        header = (
            f"{title} / 第 {current_page + 1} 頁 {len(videos)} 筆\n{controls}"
        )
        result = run_fzf(
            videos,
            header,
            paging=can_page,
            playback_keys=True,
            refresh=page_loader is not None,
        )

        if result.action == FZF_CANCEL:
            eprint("已取消。")
            return 130
        if result.action == FZF_NEXT_PAGE:
            if current_page + 1 < len(video_pages):
                current_page += 1
                continue
            if page_loader:
                next_videos = page_loader(len(video_pages) + 1)
                seen = seen_video_keys(video_pages)
                next_videos = [v for v in next_videos if video_key(v) not in seen]
                if next_videos:
                    video_pages.append(next_videos)
                    current_page += 1
                else:
                    eprint("這一批沒有新影片；可按 Ctrl-R 重新整理。")
                continue
            if (
                can_load_pages
                and source is not None
                and keyword is not None
                and load_next_video_page(source, keyword, args, video_pages)
            ):
                current_page += 1
                eprint(f"已載入第 {current_page + 1} 頁。")
            else:
                eprint("沒有更多結果。")
            continue
        if result.action == FZF_PREV_PAGE:
            if current_page > 0:
                current_page -= 1
            else:
                eprint("已經在第一頁。")
            continue
        if result.action == FZF_REFRESH and page_loader:
            refreshed = page_loader(current_page + 1)
            if refreshed:
                video_pages[current_page] = refreshed
            else:
                eprint("這一批沒有影片，保留原清單。")
            continue
        if result.action in (FZF_PLAY, FZF_PLAY_AUDIO, FZF_PLAY_VIDEO) and result.video:
            for video in result.selected or (result.video,):
                if play_selected(video, args, result.action):
                    eprint("播放失敗，已停止這批佇列並返回清單。")
                    break
            continue

        eprint("已取消。")
        return 130


# Must stay in step with build_parser's subcommands; test_commands_match_parser checks it.
COMMANDS = {"search", "users", "user", "hot", "rank", "backend-info", "history",
            "login", "logout", "status", "recommend", "home", "doctor", "completion",
            "watchlater", "favorites", "install"}


def parser_subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def completion_table() -> list[tuple[str, list[str]]]:
    """Command name plus its option flags, read straight off the parser."""
    table = []
    for name, subparser in sorted(parser_subcommands(build_parser()).items()):
        flags = sorted({
            option
            for action in subparser._actions
            for option in action.option_strings
            if option.startswith("--")
        })
        table.append((name, flags))
    return table


CLI_NAME = "bili-audio"
COMPLETION_TARGETS = (CLI_NAME, "bili_audio.py", "./bili_audio.py")


def completion_script(shell: str, autoload: bool = False) -> str:
    """autoload=True emits the #compdef form zsh picks up from fpath; otherwise
    the script registers itself and can simply be sourced."""
    table = completion_table()
    names = " ".join(name for name, _ in table)
    if shell == "bash":
        cases = "\n".join(
            f'    {name}) options="{" ".join(flags)}" ;;' for name, flags in table
        )
        return f"""# bili-audio bash completion
# eval "$(./bili_audio.py completion bash)"
_bili_audio_complete() {{
  local current options
  current="${{COMP_WORDS[COMP_CWORD]}}"
  COMPREPLY=()
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "{names}" -- "$current"))
    return
  fi
  case "${{COMP_WORDS[1]}}" in
{cases}
    *) options="" ;;
  esac
  COMPREPLY=($(compgen -W "$options" -- "$current"))
}}
complete -F _bili_audio_complete bili_audio.py
complete -F _bili_audio_complete ./bili_audio.py
"""
    cases = "\n".join(
        f'      {name}) compadd -- {" ".join(flags)} ;;' for name, flags in table
    )
    targets = " ".join(COMPLETION_TARGETS)
    body = f"""  if (( CURRENT == 2 )); then
    compadd -- {names}
    return
  fi
  case "${{words[2]}}" in
{cases}
  esac"""
    if autoload:
        # A file in fpath named _bili-audio: the body is the function itself.
        return f"""#compdef {targets}
# Generated by `{CLI_NAME} completion zsh`; regenerate after adding options.
{body}
"""
    return f"""# bili-audio zsh completion
# eval "$({CLI_NAME} completion zsh)"
_bili_audio_complete() {{
{body}
}}
compdef _bili_audio_complete {targets}
"""


def install_paths(prefix: str | None) -> tuple[Path, Path]:
    """Both defaults are already on PATH and fpath, so no shell config needs editing."""
    if prefix:
        root = Path(prefix).expanduser()
        return (root / "bin" / CLI_NAME,
                root / "share" / "zsh" / "site-functions" / f"_{CLI_NAME}")
    return (
        Path.home() / ".local" / "bin" / CLI_NAME,
        Path("/opt/homebrew/share/zsh/site-functions") / f"_{CLI_NAME}",
    )


def run_install(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    link, completion = install_paths(args.prefix)

    if args.uninstall:
        removed = []
        for path in (link, completion):
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
                    removed.append(str(path))
            except OSError as exc:
                eprint(f"無法移除 {path}：{exc}")
        print("已移除：\n  " + "\n  ".join(removed) if removed else "沒有找到已安裝的檔案。")
        return 0

    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(script)
    except OSError as exc:
        raise SystemExit(f"無法建立 {link}：{exc}") from exc
    print(f"已安裝指令：{link} -> {script}")

    try:
        completion.parent.mkdir(parents=True, exist_ok=True)
        completion.write_text(completion_script("zsh", autoload=True), encoding="utf-8")
    except OSError as exc:
        eprint(f"無法寫入補全檔 {completion}：{exc}")
        print(f'補全可改用：eval "$({CLI_NAME} completion zsh)"')
    else:
        print(f"已安裝 zsh 補全：{completion}")
        print(f"開一個新的 terminal 之後，{CLI_NAME} 加 Tab 就會列出子命令。")

    if str(link.parent) not in os.environ.get("PATH", "").split(":"):
        eprint(f"注意：{link.parent} 不在 PATH，請自行加入。")
    print(f"要移除：{CLI_NAME} install --uninstall")
    return 0


def run_completion(args: argparse.Namespace) -> int:
    if args.list:
        for name, flags in completion_table():
            print(name)
            for flag in flags:
                print(f"    {flag}")
        return 0
    print(completion_script(args.shell), end="")
    return 0


def normalize_argv(argv: list[str]) -> list[str]:
    """Keep the original ``bili_audio.py KEYWORD`` syntax working."""
    if not argv:
        return ["recommend"]
    if argv in (["-h"], ["--help"], ["--version"]):
        return argv
    if argv[0] not in COMMANDS:
        return ["search", *argv]
    return argv


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        choices=("auto", "bili-cli", "direct"),
        default="auto",
        help="資料後端：auto 優先使用 bili 指令，失敗改用公開 API",
    )
    parser.add_argument("--bili-cli", help="指定 bilibili-cli 的 bili 執行檔")
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="API／bili 指令逾時秒數，預設 20",
    )


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="只輸出 JSON，不進入互動介面",
    )


def add_playback_arguments(parser: argparse.ArgumentParser) -> None:
    add_backend_arguments(parser)
    add_output_arguments(parser)
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=0,
        help="最多顯示幾筆；0 表示不限制",
    )
    parser.add_argument("--first", action="store_true", help="直接選第一筆")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--print-url",
        action="store_true",
        help="選擇後只印出影片頁網址",
    )
    output.add_argument(
        "--direct-url",
        action="store_true",
        help="選擇後用 yt-dlp 印出串流網址",
    )
    parser.add_argument(
        "--format",
        default="bestaudio/best",
        help="yt-dlp 格式，預設 bestaudio/best",
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="讓 yt-dlp/mpv 讀瀏覽器 cookies，例如 chrome",
    )
    parser.add_argument(
        "--cookies",
        help="Netscape cookies.txt 路徑，同時供 API 與 yt-dlp/mpv 使用",
    )
    parser.add_argument("--mpv", help="指定 mpv 路徑")
    parser.add_argument("--audio-device", default="auto",
                        help="mpv 音訊裝置；用 mpv --audio-device=help 查詢")
    parser.add_argument(
        "--volume",
        type=int,
        default=40,
        help="mpv 音量 0–100，預設 40",
    )
    parser.add_argument(
        "--play-mode",
        choices=("audio", "video", "window"),
        default="video",
        help="Enter／--first 的模式：預設 video 終端影片；audio 音訊、window 滑鼠控制視窗",
    )
    parser.add_argument(
        "--video-output",
        choices=("auto", "tct", "kitty", "sixel"),
        default="kitty",
        help="預設 kitty 圖片輸出；auto 自動判斷，tct 為字元方塊備用模式",
    )
    parser.add_argument(
        "--kitty-shm",
        choices=("auto", "yes", "no"),
        default="auto",
        help="kitty 輸出是否用共享記憶體；auto 只在真正的 kitty 開啟，Ghostty 會關閉",
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        help="隱蔽播放：畫面縮到右下角、關掉 OSD 與檔名輸出；mpv 內按 b 可立刻收掉畫面",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=0,
        metavar="PX",
        help="限制來源影像高度（例如 480）；預設 0 不限制，取最高可用串流",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bilibili 終端瀏覽器：登入、個人推薦、搜尋、熱門、排行榜，"
            "並用 mpv 聽音訊或在 terminal 播放影片。"
        )
    )
    parser.add_argument("--version", action="version", version="bili-audio 2.1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("home", help="功能選單（搜尋、熱門、帳號等）")
    subparsers.add_parser("doctor", help="檢查工具與設定，不顯示 cookies")
    subparsers.add_parser("logout", help="移除本機儲存的登入，不登出瀏覽器")
    status = subparsers.add_parser("status", help="驗證目前登入帳號")
    add_backend_arguments(status)
    add_output_arguments(status)
    status.add_argument("--cookies", help="驗證指定 Netscape cookie 檔")
    login = subparsers.add_parser("login", help="Bilibili App 掃碼登入，或匯入 cookies")
    add_backend_arguments(login)
    login_method = login.add_mutually_exclusive_group()
    login_method.add_argument("--cookies", help="匯入 Netscape cookie 檔")
    login_method.add_argument("--cookie-stdin", action="store_true", help="從 stdin 讀取 Cookie header")
    recommend = subparsers.add_parser("recommend", help="開啟推薦，需要時自動登入（預設入口）")
    add_playback_arguments(recommend)
    recommend.add_argument("-p", "--page", type=int, default=1)
    recommend.set_defaults(limit=20)

    search = subparsers.add_parser(
        "search",
        help="搜尋影片（也支援直接輸入關鍵字）",
    )
    search.add_argument("keyword", nargs="*", help="搜尋關鍵字；留空會詢問")
    search.add_argument(
        "-p",
        "--pages",
        type=int,
        default=1,
        help="預先搜尋頁數，預設 1",
    )
    add_playback_arguments(search)

    users = subparsers.add_parser("users", help="搜尋 UP 主")
    users.add_argument("keyword", nargs="+", help="使用者名稱關鍵字")
    users.add_argument("-p", "--page", type=int, default=1, help="頁碼")
    users.add_argument("-n", "--limit", type=int, default=20, help="最多顯示筆數")
    add_backend_arguments(users)
    add_output_arguments(users)

    user = subparsers.add_parser("user", help="查看 UP 主資料與影片")
    user.add_argument("uid_or_name", nargs="+", help="UID 或使用者名稱")
    user.add_argument(
        "--videos",
        type=int,
        default=0,
        metavar="N",
        help="另外載入 N 部影片供選擇播放",
    )
    add_playback_arguments(user)

    watchlater = subparsers.add_parser("watchlater", help="帳號的稍後再看（唯讀）")
    add_playback_arguments(watchlater)
    watchlater.set_defaults(limit=0)

    favorites = subparsers.add_parser("favorites", help="帳號的收藏夾（唯讀）")
    favorites.add_argument(
        "--media-id",
        type=int,
        default=None,
        help="直接開啟指定收藏夾；省略時先選收藏夾",
    )
    add_playback_arguments(favorites)
    favorites.set_defaults(limit=0)

    install = subparsers.add_parser(
        "install", help=f"把自己連結成 {CLI_NAME} 指令並安裝 zsh 補全",
    )
    install.add_argument("--prefix", help="安裝到指定前綴的 bin／share，預設家目錄與 Homebrew")
    install.add_argument("--uninstall", action="store_true", help="移除連結與補全檔")

    completion = subparsers.add_parser(
        "completion",
        help="輸出 shell 補全腳本，或用 --list 直接列出可用參數",
    )
    completion.add_argument(
        "shell",
        nargs="?",
        choices=("zsh", "bash"),
        default="zsh",
        help="要產生哪個 shell 的補全腳本，預設 zsh",
    )
    completion.add_argument(
        "--list",
        action="store_true",
        help="改成人看的清單：列出每個子命令與它的參數",
    )

    history = subparsers.add_parser(
        "history",
        help="本機播放紀錄；Enter 會從 mpv 記住的位置繼續",
    )
    history.add_argument(
        "--clear",
        action="store_true",
        help="清除本機播放紀錄後結束；不影響登入與續播位置",
    )
    add_playback_arguments(history)
    history.set_defaults(limit=0)

    hot = subparsers.add_parser("hot", help="熱門影片")
    hot.add_argument("-p", "--page", type=int, default=1, help="頁碼")
    add_playback_arguments(hot)
    hot.set_defaults(limit=20)

    rank = subparsers.add_parser("rank", help="全站排行榜")
    rank.add_argument(
        "--day",
        type=int,
        choices=(3, 7),
        default=None,
        help="相容參數；目前全站榜不分 3／7 日，指定時會忽略",
    )
    add_playback_arguments(rank)
    rank.set_defaults(limit=20)

    backend_info = subparsers.add_parser(
        "backend-info",
        help="顯示目前會使用的資料後端",
    )
    add_backend_arguments(backend_info)
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(normalize_argv(argv))

    timeout = getattr(args, "timeout", 1)
    if not math.isfinite(timeout) or timeout <= 0:
        parser.error("--timeout 必須大於 0")
    if getattr(args, "pages", 1) < 1:
        parser.error("--pages 必須大於 0")
    if getattr(args, "page", 1) < 1:
        parser.error("--page 必須大於 0")
    if getattr(args, "limit", 0) < 0:
        parser.error("--limit 不可小於 0")
    if args.command == "recommend" and args.limit > 30:
        parser.error("recommend 每批最多 30 筆，請用 --page 或 Ctrl-F 換下一批")
    if getattr(args, "videos", 0) < 0:
        parser.error("--videos 不可小於 0")
    volume = getattr(args, "volume", 40)
    if not 0 <= volume <= 100:
        parser.error("--volume 必須介於 0 和 100")
    return args


def videos_from_items(items: list[dict[str, Any]]) -> list[Video]:
    videos: list[Video] = []
    seen: set[str] = set()
    for item in items:
        video = make_video(item)
        if not video:
            continue
        key = video_key(video)
        if key in seen:
            continue
        seen.add(key)
        videos.append(video)
    return videos


def require_account(session: Session, args: argparse.Namespace, feature: str) -> None:
    if not session.status(args.timeout, cached=True)["logged_in"]:
        raise SystemExit(f"{feature}需要有效登入，請先執行 ./bili_audio.py login。")
    session.ensure_buvid(args.timeout)


def watchlater_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Observed shape: data.list[], with a negative state on videos that no longer play."""
    data = payload.get("data")
    entries = data.get("list") if isinstance(data, dict) else None
    if entries is None and isinstance(data, dict):
        entries = []
    if not isinstance(entries, list):
        raise SystemExit("稍後再看 API 格式已變更，請更新程式。")
    items: list[dict[str, Any]] = []
    skipped = 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("bvid"):
            skipped += 1
            continue
        state = entry.get("state")
        if isinstance(state, int) and state < 0:
            skipped += 1
            continue
        items.append(entry)
    return items, skipped


def favorite_folders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    folders = data.get("list") if isinstance(data, dict) else None
    if folders is None:
        return []
    if not isinstance(folders, list):
        raise SystemExit("收藏夾 API 格式已變更，請更新程式。")
    clean = []
    for folder in folders:
        if not isinstance(folder, dict) or not isinstance(folder.get("id"), int):
            continue
        clean.append({
            "id": folder["id"],
            "title": clean_text(folder.get("title")) or str(folder["id"]),
            "media_count": folder.get("media_count") or 0,
        })
    return clean


def favorite_media_items(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, bool]:
    """Reshapes data.medias[] into the keys make_video already understands."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit("收藏夾 API 格式已變更，請更新程式。")
    medias = data.get("medias")
    if medias is None:
        medias = []  # An empty folder answers with null rather than [].
    if not isinstance(medias, list):
        raise SystemExit("收藏夾 API 格式已變更，請更新程式。")
    items: list[dict[str, Any]] = []
    skipped = 0
    for media in medias:
        if not isinstance(media, dict):
            skipped += 1
            continue
        # type 2 is a normal UGC video; attr was 0 on every readable entry observed,
        # so anything non-zero is treated as not playable rather than guessed at.
        if media.get("type") != 2 or not media.get("bvid") or media.get("attr"):
            skipped += 1
            continue
        counts = media.get("cnt_info") if isinstance(media.get("cnt_info"), dict) else {}
        items.append({
            **media,
            "pubdate": media.get("pubtime"),
            "desc": media.get("intro"),
            "stat": {"view": counts.get("play")},
        })
    return items, skipped, bool(data.get("has_more"))


def report_skipped(skipped: int, args: argparse.Namespace) -> None:
    if skipped and not args.json:
        eprint(f"已略過 {skipped} 筆失效或非影片項目。")


def finish_video_action(
    args: argparse.Namespace,
    videos: list[Video],
    title: str,
    video_pages: list[list[Video]] | None = None,
    source: BiliSource | None = None,
    keyword: str | None = None,
) -> int:
    if args.json:
        print_json(videos)
        return 0
    if not videos:
        eprint("找不到影片。")
        return 1

    if not (args.first or args.print_url or args.direct_url):
        return interactive_playback(
            args,
            video_pages or [videos],
            title,
            source=source,
            keyword=keyword,
        )

    video = videos[0] if args.first else choose_with_fzf(videos)
    if not video:
        eprint("已取消。")
        return 130
    if args.print_url:
        print(video.url)
        return 0
    if args.direct_url:
        return print_direct_url(video, args)
    return play_selected(video, args)


def run_search(args: argparse.Namespace, source: BiliSource) -> int:
    keyword = " ".join(args.keyword).strip()
    if not keyword:
        try:
            if args.json:
                eprint("搜尋關鍵字：")
                keyword = input().strip()
            else:
                keyword = input("搜尋關鍵字：").strip()
        except EOFError as exc:
            raise SystemExit(
                "需要輸入搜尋關鍵字；也可以直接寫在 search 後面。"
            ) from exc
    if not keyword:
        raise SystemExit("需要輸入搜尋關鍵字。")

    video_pages = search_video_pages(source, keyword, args.pages)
    video_pages = limit_video_pages(video_pages, args.limit)
    videos = flatten_video_pages(video_pages)
    return finish_video_action(
        args,
        videos,
        f"搜尋：{keyword}",
        video_pages=video_pages,
        source=source if not args.limit else None,
        keyword=keyword if not args.limit else None,
    )


def run_users(args: argparse.Namespace, source: BiliSource) -> int:
    keyword = " ".join(args.keyword).strip()
    users = [
        user
        for item in source.search_users(keyword, args.page)
        if (user := make_user(item)) is not None
    ]
    if args.limit:
        users = users[: args.limit]
    if args.json:
        print(
            json.dumps(
                [user.as_dict() for user in users],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not users:
        eprint("找不到使用者。")
        return 1
    for user in users:
        print(
            f"{user.uid:>12}  {user.name}  "
            f"粉絲 {format_count(user.fans):>8}  "
            f"影片 {format_count(user.videos):>6}"
        )
        if user.sign:
            print(f"{'':>14}{user.sign}")
    return 0


def run_user(args: argparse.Namespace, source: BiliSource) -> int:
    uid_or_name = " ".join(args.uid_or_name).strip()
    user = source.user(uid_or_name)
    videos: list[Video] | None = None
    if args.videos:
        videos = videos_from_items(source.user_videos(uid_or_name, args.videos))
        videos = [
            video
            if video.author
            else dataclasses.replace(video, author=user.name)
            for video in videos
        ]
        if args.limit:
            videos = videos[: args.limit]

    if args.json:
        print_user_json(user, videos)
        return 0

    print_user(user)
    if videos is None:
        return 0
    if not videos:
        eprint("這位 UP 主目前沒有可播放的影片。")
        return 1
    return finish_video_action(args, videos, f"{user.name} 的影片")


def run_history(args: argparse.Namespace) -> int:
    if args.clear:
        clear_history()
        print("已清除本機播放紀錄；登入與 mpv 續播位置不受影響。")
        return 0
    entries = load_history()
    if args.limit:
        entries = entries[: args.limit]
    videos = videos_from_history(entries)
    if args.json:
        print_json(videos)
        return 0
    if not videos:
        eprint("還沒有本機播放紀錄；播放任何一部影片後就會出現在這裡。")
        return 1
    return finish_video_action(args, videos, "本機播放紀錄（新的在最前，Enter 從上次位置繼續）")


def run_hot(args: argparse.Namespace, source: BiliSource) -> int:
    count = UNLIMITED_PAGE_COUNT if args.limit == 0 else args.limit
    videos = videos_from_items(source.hot(args.page, count))
    if args.limit:
        videos = videos[: args.limit]
    return finish_video_action(args, videos, f"熱門影片（第 {args.page} 頁）")


def run_rank(args: argparse.Namespace, source: BiliSource) -> int:
    if args.day is not None:
        eprint("注意：Bilibili 全站榜目前不分 3／7 日，已忽略 --day。")
    count = UNLIMITED_PAGE_COUNT if args.limit == 0 else args.limit
    videos = videos_from_items(source.rank(count))
    if args.limit:
        videos = videos[: args.limit]
    return finish_video_action(args, videos, "全站即時榜")


def run_login(args: argparse.Namespace) -> int:
    with Session(DEFAULT_USER_AGENT, args.cookies, empty=not args.cookies) as session:
        if args.cookie_stdin:
            if sys.stdin.isatty():
                import getpass
                session.load_header(getpass.getpass("貼上 Cookie header（不會顯示）："))
            else:
                session.load_header(sys.stdin.read(65537).rstrip("\r\n"))
        elif not args.cookies:
            login_qr(session, args.timeout, eprint)
        status = session.status(args.timeout)
        if not status["logged_in"]:
            raise SystemExit("登入憑證無效或已過期；原本儲存的帳號未變更。")
        session.save()
        print(f"已登入：{clean_text(status['name'])}（UID: {status['uid']}）")
        print("執行 ./bili_audio.py recommend 查看個人推薦。")
        if env_cookie():
            eprint("注意：環境變數 Cookie 仍會優先於儲存的登入；切換帳號前請 unset BILIBILI_COOKIE BILI_COOKIE。")
    return 0


def ensure_recommend_login(args: argparse.Namespace, session: Session, *, expired: bool = False) -> dict:
    status = {"logged_in": False} if expired else session.status(args.timeout, cached=True)
    if not status["logged_in"]:
        if args.json or args.first or args.print_url or args.direct_url or not terminal_is_interactive():
            raise SystemExit("個人推薦需要有效登入，請先執行 ./bili_audio.py login。")
        if session.source in ("file", "environment"):
            raise SystemExit("指定的 Cookie 已失效；請更新 --cookies／環境變數，或移除設定後重新開啟。")
        require_command("fzf", "brew install fzf")
        eprint("尚未登入或登入已過期，掃碼一次即可繼續。")
        # Keep the previous saved account until the replacement is verified.
        with Session(session.user_agent, empty=True) as fresh:
            login_qr(fresh, args.timeout, eprint)
            status = fresh.status(args.timeout)
            if not status["logged_in"]:
                raise SystemExit("登入驗證未通過，請重新開啟。")
            fresh.adopt_buvid(session)
            fresh.save()
            session.jar.clear()
            session.load_file(session_path())
            session.source = "saved"
            session.flush()
            session.adopt_wbi(fresh)
        eprint(f"已登入：{clean_text(status['name'])}，正在載入推薦。")
    if not args.cookies_from_browser:
        args.cookies = session.path
    return status


class RecommendationLoginExpired(Exception):
    """Restart the recommendation list after renewing an expired account."""


def run_recommend(args: argparse.Namespace, session: Session) -> int:
    status = ensure_recommend_login(args, session)

    next_batch = args.page

    def load_page(page: int) -> list[Video]:
        nonlocal next_batch
        if not session.status(args.timeout, cached=True)["logged_in"]:
            session.invalidate()
            raise RecommendationLoginExpired
        batch = max(next_batch, args.page + page - 1)
        session.ensure_buvid(args.timeout)
        query = session.wbi_query({
            "fresh_idx": batch, "fresh_idx_1h": batch, "fresh_type": 4,
            "feed_version": "V8", "homepage_ver": 1, "web_location": 1430650,
            "brush": 0, "y_num": 5, "ps": args.limit or 30,
        }, args.timeout)
        pace_api_calls()
        payload = session.request(RECOMMEND_API_URL + "?" + query, args.timeout)
        if payload.get("code") == -101:
            session.invalidate()
            raise RecommendationLoginExpired
        require_api_success(payload)
        next_batch = batch + 1
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("item"), list):
            raise SystemExit("Bilibili 推薦 API 格式已變更，請更新程式。")
        items = []
        for item in data["item"]:
            if not isinstance(item, dict) or item.get("goto") != "av":
                continue
            bvid = item.get("bvid")
            if not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                continue
            # Ignore promotional/external URLs in recommendation cards.
            reason = item.get("rcmd_reason")
            description = reason.get("content", "") if isinstance(reason, dict) else reason
            items.append({**item, "url": f"https://www.bilibili.com/video/{bvid}",
                          "description": item.get("description") or description or ""})
        videos = videos_from_items(items)
        return videos[:args.limit] if args.limit else videos

    videos = load_page(1)
    title = f"{clean_text(status['name'])} 的推薦"
    if args.json or args.first or args.print_url or args.direct_url or not videos:
        return finish_video_action(args, videos, title)
    return interactive_playback(args, [videos], title, page_loader=load_page)


def run_watchlater(args: argparse.Namespace, session: Session) -> int:
    require_account(session, args, "稍後再看")
    pace_api_calls()
    payload = session.request(WATCHLATER_API_URL, args.timeout)
    require_api_success(payload)
    items, skipped = watchlater_items(payload)
    report_skipped(skipped, args)
    videos = videos_from_items(items)
    if args.limit:
        videos = videos[: args.limit]
    if args.json:
        print_json(videos)
        return 0
    if not videos:
        eprint("稍後再看是空的。")
        return 1
    return finish_video_action(args, videos, "稍後再看")


def choose_favorite_folder(folders: list[dict[str, Any]]) -> dict[str, Any] | None:
    require_command("fzf", "brew install fzf")
    rows = [f"{folder['id']}\t{folder['title']}（{folder['media_count']} 部）"
            for folder in folders]
    result = subprocess.run(
        ["fzf", "--delimiter=\t", "--with-nth=2", "--layout=reverse",
         "--height=60%", "--border", "--prompt=收藏夾> ",
         "--header=Enter 開啟 / Esc 離開"],
        input="\n".join(rows) + "\n", text=True, stdout=subprocess.PIPE, check=False,
    )
    if result.returncode not in (0, 1, 130, -signal.SIGINT):
        raise SystemExit(f"fzf 無法開啟收藏夾清單（exit {result.returncode}）。")
    if result.returncode or not result.stdout.strip():
        return None
    chosen = result.stdout.split("\t", 1)[0].strip()
    for folder in folders:
        if str(folder["id"]) == chosen:
            return folder
    raise SystemExit("收藏夾選項無效。")


def run_favorites(args: argparse.Namespace, session: Session) -> int:
    require_account(session, args, "收藏夾")
    media_id = args.media_id

    if media_id is None:
        status = session.status(args.timeout, cached=True)
        pace_api_calls()
        payload = session.request(
            f"{FAV_FOLDERS_API_URL}?up_mid={status['uid']}", args.timeout
        )
        require_api_success(payload)
        folders = favorite_folders(payload)
        if not folders:
            eprint("這個帳號沒有自建收藏夾。")
            return 1
        if args.json:
            print(json.dumps(folders, ensure_ascii=False, indent=2))
            return 0
        chosen = choose_favorite_folder(folders)
        if not chosen:
            return 0
        media_id = chosen["id"]
        title = f"收藏夾：{chosen['title']}"
    else:
        title = f"收藏夾 {media_id}"

    more = {"has_more": True}

    def load_page(page: int) -> list[Video]:
        if page > 1 and not more["has_more"]:
            return []
        pace_api_calls()
        payload = session.request(
            f"{FAV_RESOURCE_API_URL}?media_id={media_id}"
            f"&pn={page}&ps={FAV_PAGE_SIZE}&platform=web",
            args.timeout,
        )
        require_api_success(payload)
        items, skipped, has_more = favorite_media_items(payload)
        more["has_more"] = has_more
        report_skipped(skipped, args)
        return videos_from_items(items)

    videos = load_page(1)
    if args.limit:
        videos = videos[: args.limit]
    if args.json:
        print_json(videos)
        return 0
    if not videos:
        eprint("這個收藏夾沒有可播放的影片。")
        return 1
    return interactive_playback(args, [videos], title, page_loader=load_page)


def run_doctor() -> int:
    missing = []
    print(f"Python：{sys.version.split()[0]}")
    for command in ("fzf", "mpv", "yt-dlp", "curl", "qrencode"):
        path = shutil.which(command)
        optional = "（QR 登入需要）" if command == "qrencode" else ""
        print(f"{command}{optional}：{path or '未安裝'}")
        if not path and command in ("fzf", "mpv", "yt-dlp"):
            missing.append(command)
    print(f"本機登入：{'已儲存，使用 status 驗證' if session_path().exists() else '尚未儲存'}")
    print(f"環境變數 Cookie：{'已設定（優先於本機登入）' if env_cookie() else '未設定'}")
    if missing:
        print("安裝缺少的工具：brew install " + " ".join(missing))
    return 1 if missing else 0


def run_home() -> int:
    require_command("fzf", "brew install fzf")
    if not terminal_is_interactive():
        raise SystemExit("互動首頁需要 TTY；腳本請使用 search／recommend --json 等明確命令。")
    choices = [
        "recommend\t個人推薦", "search\t搜尋影片", "hot\t熱門影片",
        "rank\t全站排行榜", "watchlater\t稍後再看", "favorites\t收藏夾",
        "login\t登入／切換帳號", "status\t驗證登入狀態",
        "logout\t移除本機登入", "doctor\t檢查環境",
    ]
    while True:
        result = subprocess.run(
            ["fzf", "--delimiter=\t", "--with-nth=2", "--layout=reverse",
             "--height=60%", "--border", "--prompt=bili-audio> ",
             "--header=Enter 開啟 / Esc 離開"],
            input="\n".join(choices) + "\n", text=True, stdout=subprocess.PIPE, check=False,
        )
        if result.returncode not in (0, 1, 130, -signal.SIGINT):
            raise SystemExit(f"fzf 無法開啟首頁（exit {result.returncode}），請確認 terminal 可用。")
        if result.returncode or not result.stdout.strip():
            return 0
        command = result.stdout.split("\t", 1)[0].strip()
        if command not in COMMANDS or command == "home":
            raise SystemExit("首頁選項無效。")
        try:
            main([command])
        except KeyboardInterrupt:
            eprint("已中斷操作，返回首頁。")
        except SystemExit as exc:
            eprint(clean_text(exc))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "completion":
        return run_completion(args)
    if args.command == "install":
        return run_install(args)
    if args.command == "home":
        return run_home()
    if args.command == "doctor":
        return run_doctor()
    if args.command == "login":
        return run_login(args)
    if args.command == "logout":
        try:
            session_path().unlink(missing_ok=True)
        except OSError as exc:
            raise SystemExit("無法移除本機登入，請確認設定目錄權限。") from exc
        print("已移除本機登入；不影響瀏覽器或其他裝置。")
        if env_cookie():
            eprint("環境變數 Cookie 仍有效；請 unset BILIBILI_COOKIE BILI_COOKIE。")
        return 0

    global _AUTH_SESSION
    with Session(os.environ.get("BILI_USER_AGENT", DEFAULT_USER_AGENT), getattr(args, "cookies", None)) as session:
        previous = _AUTH_SESSION
        _AUTH_SESSION = session if session.source != "none" else None
        try:
            if args.command == "status":
                status = session.status(args.timeout)
                status["name"] = clean_text(status.get("name", ""))
                if args.json:
                    print(json.dumps(status, ensure_ascii=False, indent=2))
                else:
                    print(f"已登入：{status['name']}（UID: {status['uid']}，來源: {status['source']}）"
                          if status["logged_in"] else "尚未登入或登入已過期，請執行 login。")
                return 0 if status["logged_in"] else 1
            if hasattr(args, "cookies") and not args.cookies_from_browser and session.has_login:
                args.cookies = session.path
            if args.command == "watchlater":
                return run_watchlater(args, session)
            if args.command == "favorites":
                return run_favorites(args, session)
            if args.command == "recommend":
                for attempt in range(2):
                    try:
                        return run_recommend(args, session)
                    except RecommendationLoginExpired:
                        if attempt:
                            raise SystemExit("重新登入後仍被拒絕，請稍後重試。") from None
                        ensure_recommend_login(args, session, expired=True)
            # Use this account consistently instead of an unrelated optional CLI session.
            if session.source != "none" and args.backend == "auto":
                args.backend = "direct"
            return dispatch_public(args)
        finally:
            _AUTH_SESSION = previous


def dispatch_public(args: argparse.Namespace) -> int:
    source = BiliSource(args)

    if args.command == "history":
        return run_history(args)
    if args.command == "backend-info":
        if source.upstream:
            print(f"bilibili-cli: {source.upstream.executable}")
        else:
            print("direct: Bilibili 公開 API")
        return 0
    if args.command == "search":
        return run_search(args, source)
    if args.command == "users":
        return run_users(args, source)
    if args.command == "user":
        return run_user(args, source)
    if args.command == "hot":
        return run_hot(args, source)
    if args.command == "rank":
        return run_rank(args, source)
    raise SystemExit(f"不支援的命令：{args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中斷。", file=sys.stderr)
        raise SystemExit(130)
