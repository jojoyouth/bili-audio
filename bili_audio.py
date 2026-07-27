#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from bili_backend import BiliCliBackend, BiliCliError, find_bili_cli

SEARCH_API_URL = "https://api.bilibili.com/x/web-interface/search/type"
HOT_API_URL = "https://api.bilibili.com/x/web-interface/popular"
RANK_API_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
USER_CARD_API_URL = "https://api.bilibili.com/x/web-interface/card"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
UNLIMITED_PAGE_COUNT = 100
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


FZF_PLAY = "play"
FZF_PLAY_AUDIO = "play-audio"
FZF_PLAY_VIDEO = "play-video"
FZF_CANCEL = "cancel"
FZF_NEXT_PAGE = "next-page"
FZF_PREV_PAGE = "prev-page"


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


def request_headers() -> dict[str, str]:
    headers = {
        "User-Agent": os.environ.get("BILI_USER_AGENT", DEFAULT_USER_AGENT),
        "Referer": "https://search.bilibili.com/",
        "Accept": "application/json,text/plain,*/*",
    }

    cookie = os.environ.get("BILIBILI_COOKIE") or os.environ.get("BILI_COOKIE")
    if cookie:
        headers["Cookie"] = cookie

    return headers


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
    message = payload.get("message") or payload.get("msg") or "unknown error"
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
    if key == "ctrl-a":
        action = FZF_PLAY_AUDIO
    elif key in ("ctrl-v", "alt-v"):
        action = FZF_PLAY_VIDEO
    else:
        action = FZF_PLAY

    row = lines[1] if len(lines) > 1 else lines[0]
    index_text = row.split("\t", 1)[0].strip()
    try:
        return FzfResult(action, videos[int(index_text)])
    except (ValueError, IndexError):
        raise SystemExit("fzf 回傳了無法解析的選項。")


def run_fzf(
    videos: list[Video],
    header: str,
    paging: bool = False,
    playback_keys: bool = False,
) -> FzfResult:
    if not shutil.which("fzf"):
        raise SystemExit("找不到 fzf。可以先安裝：brew install fzf")

    expected_keys = "enter"
    if playback_keys:
        expected_keys = "enter,ctrl-a,ctrl-v,alt-v"
    if paging:
        expected_keys += ",ctrl-f,ctrl-b,alt-n,alt-p"

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
    ]

    result = subprocess.run(
        fzf,
        input=render_fzf_rows(videos),
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return FzfResult(FZF_CANCEL)

    return parse_fzf_result(result.stdout, videos)


def choose_with_fzf(videos: list[Video]) -> Video | None:
    result = run_fzf(videos, "Enter 播放 / Esc 取消")
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
            or "wezterm" in term_program
            or "ghostty" in term_program
            or os.environ.get("WEZTERM_PANE")
        )
    ):
        return "kitty"
    return "tct"


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
        *mpv_ytdl_options(args),
    ]
    if mode == "audio":
        command.append("--no-video")
    else:
        video_output = resolved_video_output or terminal_video_output(
            args.video_output
        )
        command.extend(
            [
                "--no-config",
                f"--vo={video_output}",
                "--profile=sw-fast",
                "--really-quiet",
                "--osd-level=0",
                "--term-status-msg=",
            ]
        )
        if video_output == "tct":
            command.append("--vo-tct-buffering=frame")
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


def play_video(video: Video, args: argparse.Namespace) -> int:
    if not terminal_is_interactive():
        raise SystemExit("terminal 影片模式需要互動式 TTY，不能輸出到 pipe。")
    mpv = find_mpv(args.mpv)
    video_output = resolve_mpv_video_output(mpv, args.video_output)
    command = build_mpv_command(
        video,
        args,
        "video",
        resolved_video_output=video_output,
        mpv_path=mpv,
    )
    eprint(f"終端播放影片（{video_output}）：{video.title}")
    eprint("mpv 快捷鍵：Space 暫停、←/→ 快退快進、q 離開")
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
    if action == FZF_PLAY_AUDIO:
        mode = "audio"
    elif action == FZF_PLAY_VIDEO:
        mode = "video"
    else:
        mode = args.play_mode
    if mode == "video":
        return play_video(video, args)
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
) -> int:
    current_page = 0
    can_load_pages = source is not None and keyword is not None
    can_page = len(video_pages) > 1 or can_load_pages

    while True:
        videos = video_pages[current_page]
        if args.limit:
            videos = videos[: args.limit]
        controls = "Enter 預設 / Ctrl-A 音訊 / Ctrl-V 影片 / Esc 離開"
        if can_page:
            controls += " / Ctrl-F 下一頁 / Ctrl-B 上一頁"
        header = (
            f"{title} / {controls} / "
            f"第 {current_page + 1} 頁 {len(videos)} 筆"
        )
        result = run_fzf(
            videos,
            header,
            paging=can_page,
            playback_keys=True,
        )

        if result.action == FZF_CANCEL:
            eprint("已取消。")
            return 130
        if result.action == FZF_NEXT_PAGE:
            if current_page + 1 < len(video_pages):
                current_page += 1
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
        if result.action in (FZF_PLAY, FZF_PLAY_AUDIO, FZF_PLAY_VIDEO) and result.video:
            play_selected(result.video, args, result.action)
            continue

        eprint("已取消。")
        return 130


COMMANDS = {"search", "users", "user", "hot", "rank", "backend-info"}


def normalize_argv(argv: list[str]) -> list[str]:
    """Keep the original ``bili_audio.py KEYWORD`` syntax working."""
    if not argv:
        return ["search"]
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
        help="Netscape cookies.txt 路徑，給 yt-dlp/mpv 使用",
    )
    parser.add_argument("--mpv", help="指定 mpv 路徑")
    parser.add_argument(
        "--volume",
        type=int,
        default=40,
        help="mpv 音量 0–100，預設 40",
    )
    parser.add_argument(
        "--play-mode",
        choices=("audio", "video"),
        default="audio",
        help="Enter／--first 的預設模式，預設 audio",
    )
    parser.add_argument(
        "--video-output",
        choices=("auto", "tct", "kitty", "sixel"),
        default="auto",
        help="終端影片輸出；auto 在 Kitty/WezTerm 用 kitty，其餘用 tct",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bilibili 終端瀏覽器：搜尋、使用者、熱門、排行榜，"
            "並用 mpv 聽音訊或在 terminal 播放影片。"
        )
    )
    parser.add_argument("--version", action="version", version="bili-audio 2.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser(
        "search",
        help="搜尋影片（也是向下相容的預設命令）",
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    source = BiliSource(args)

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
