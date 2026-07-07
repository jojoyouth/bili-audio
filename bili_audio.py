#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_URL = "https://api.bilibili.com/x/web-interface/search/type"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


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
        }


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
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
    return dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def make_video(item: dict[str, Any]) -> Video | None:
    bvid = str(item.get("bvid") or "").strip()
    aid = str(item.get("aid") or item.get("id") or "").strip()
    arcurl = str(item.get("arcurl") or "").strip()

    if bvid:
        url = f"https://www.bilibili.com/video/{bvid}"
    elif arcurl:
        url = arcurl.replace("http://", "https://", 1)
    else:
        return None

    pubdate = item.get("pubdate")
    if not isinstance(pubdate, int):
        pubdate = None

    return Video(
        title=clean_text(item.get("title")),
        author=clean_text(item.get("author")),
        bvid=bvid,
        aid=aid,
        url=url,
        duration=clean_text(item.get("duration")) or "-",
        play=item.get("play"),
        pubdate=pubdate,
        description=clean_text(item.get("description")),
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
    return os.environ.get(
        "BILI_COOKIE_JAR",
        os.path.join(tempfile.gettempdir(), "bili_audio_cookiejar.txt"),
    )


def fetch_page(keyword: str, page: int, timeout: float) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "search_type": "video",
            "keyword": keyword,
            "page": page,
        }
    )
    url = f"{API_URL}?{params}"
    if shutil.which("curl"):
        return fetch_page_with_curl(url, timeout)
    return fetch_page_with_urllib(url, timeout)


def fetch_page_with_curl(url: str, timeout: float) -> dict[str, Any]:
    result = run_curl_json(url, timeout)
    if result.returncode != 0 and "412" in result.stderr and not env_cookie():
        warm_bilibili_cookie_jar(timeout)
        result = run_curl_json(url, timeout)

    if result.returncode != 0:
        if "412" in result.stderr:
            raise SystemExit(
                "Bilibili 回應 412，通常是反爬檢查。可以稍後再試，"
                "或設定 BILIBILI_COOKIE 後重跑。"
            )
        detail = result.stderr.strip() or f"curl exit {result.returncode}"
        raise SystemExit(f"連不上 Bilibili API：{detail}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("Bilibili API 回傳不是有效 JSON。") from exc


def run_curl_json(url: str, timeout: float) -> subprocess.CompletedProcess[str]:
    headers = request_headers()
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
    if not env_cookie():
        command.extend(["-b", cookie_jar_path(), "-c", cookie_jar_path()])
    command.append(url)

    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)


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
        raise SystemExit(f"Bilibili API HTTP 錯誤：{exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"連不上 Bilibili API：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit("Bilibili API 回傳不是有效 JSON。") from exc


def search_videos(keyword: str, pages: int, timeout: float) -> list[Video]:
    videos: list[Video] = []
    seen: set[str] = set()

    for page in range(1, pages + 1):
        payload = fetch_page(keyword, page, timeout)
        if payload.get("code") != 0:
            message = payload.get("message") or "unknown error"
            raise SystemExit(f"Bilibili API 錯誤：{message}")

        results = payload.get("data", {}).get("result", [])
        if not results:
            break

        for item in results:
            if not isinstance(item, dict):
                continue
            video = make_video(item)
            if not video:
                continue
            key = video.bvid or video.aid or video.url
            if key in seen:
                continue
            seen.add(key)
            videos.append(video)

    return videos


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
                    format_pubdate(video.pubdate),
                    table_field(video.bvid or video.aid),
                ]
            )
        )
    return "\n".join(rows) + "\n"


def choose_with_fzf(videos: list[Video]) -> Video | None:
    if not shutil.which("fzf"):
        raise SystemExit("找不到 fzf。可以先安裝：brew install fzf")

    fzf = [
        "fzf",
        "--delimiter=\t",
        "--with-nth=2,3,4,5,6,7",
        "--prompt=Bilibili> ",
        "--height=85%",
        "--layout=reverse",
        "--border",
        "--header=Enter 播放 / Esc 取消",
    ]

    result = subprocess.run(
        fzf,
        input=render_fzf_rows(videos),
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    index_text = result.stdout.split("\t", 1)[0].strip()
    try:
        return videos[int(index_text)]
    except (ValueError, IndexError):
        raise SystemExit("fzf 回傳了無法解析的選項。")


def print_json(videos: list[Video]) -> None:
    print(json.dumps([video.as_dict() for video in videos], ensure_ascii=False, indent=2))


def require_command(command: str, install_hint: str) -> str:
    path = shutil.which(command)
    if path:
        return path
    raise SystemExit(f"找不到 {command}。可以先安裝：{install_hint}")


def yt_dlp_cookie_args(args: argparse.Namespace) -> list[str]:
    result: list[str] = []
    if args.cookies_from_browser:
        result.extend(["--cookies-from-browser", args.cookies_from_browser])
    if args.cookies:
        result.extend(["--cookies", args.cookies])
    return result


def mpv_ytdl_options(args: argparse.Namespace) -> list[str]:
    options = []
    if args.cookies_from_browser:
        options.append(f"cookies-from-browser={args.cookies_from_browser}")
    if args.cookies:
        options.append(f"cookies={args.cookies}")
    if not options:
        return []
    return [f"--ytdl-raw-options={','.join(options)}"]


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
    return subprocess.run(command, check=False).returncode


def play_audio(video: Video, args: argparse.Namespace) -> int:
    mpv = args.mpv or shutil.which("mpv")
    if not mpv:
        raise SystemExit("找不到 mpv。可以先安裝：brew install mpv")

    command = [
        mpv,
        "--no-video",
        "--force-window=no",
        *mpv_ytdl_options(args),
        "--",
        video.url,
    ]
    eprint(f"播放：{video.title}")
    eprint(video.url)
    return subprocess.run(command, check=False).returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="搜尋 Bilibili 影片，用 fzf 選擇後以 mpv --no-video 播放。"
    )
    parser.add_argument("keyword", nargs="*", help="搜尋關鍵字；留空會互動詢問")
    parser.add_argument("-p", "--pages", type=int, default=1, help="搜尋頁數，預設 1")
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多顯示幾筆；0 表示不限制")
    parser.add_argument("--json", action="store_true", help="只輸出搜尋結果 JSON，不進入 fzf")
    parser.add_argument("--first", action="store_true", help="直接選第一筆，不進入 fzf")
    parser.add_argument("--print-url", action="store_true", help="選擇後只印出影片頁網址")
    parser.add_argument("--direct-url", action="store_true", help="選擇後用 yt-dlp 印出音訊串流網址")
    parser.add_argument("--format", default="bestaudio/best", help="yt-dlp 格式，預設 bestaudio/best")
    parser.add_argument("--cookies-from-browser", help="讓 yt-dlp/mpv 讀瀏覽器 cookies，例如 chrome")
    parser.add_argument("--cookies", help="Netscape cookies.txt 路徑，給 yt-dlp/mpv 使用")
    parser.add_argument("--mpv", help="指定 mpv 路徑")
    parser.add_argument("--timeout", type=float, default=12.0, help="API 逾時秒數，預設 12")
    args = parser.parse_args(argv)

    if args.pages < 1:
        parser.error("--pages 必須大於 0")
    if args.limit < 0:
        parser.error("--limit 不可小於 0")
    if args.print_url and args.direct_url:
        parser.error("--print-url 和 --direct-url 只能擇一")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    keyword = " ".join(args.keyword).strip()
    if not keyword:
        keyword = input("搜尋關鍵字：").strip()
    if not keyword:
        raise SystemExit("需要輸入搜尋關鍵字。")

    videos = search_videos(keyword, args.pages, args.timeout)
    if args.limit:
        videos = videos[: args.limit]
    if not videos:
        eprint("找不到影片。")
        return 1

    if args.json:
        print_json(videos)
        return 0

    video = videos[0] if args.first else choose_with_fzf(videos)
    if not video:
        eprint("已取消。")
        return 130

    if args.print_url:
        print(video.url)
        return 0
    if args.direct_url:
        return print_direct_url(video, args)
    return play_audio(video, args)


if __name__ == "__main__":
    raise SystemExit(main())
