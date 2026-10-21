"""Local Bilibili sessions; credentials never go in subprocess arguments."""

from __future__ import annotations

import http.cookiejar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error


NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
QR_BASE = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
ACCOUNT_COMMENT = "# bili-audio-account: "
WBI_KEY_TTL = 3600
# Index table the web client uses to fold img_key + sub_key into the WBI mixin key.
WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def session_path() -> Path:
    root = os.environ.get("BILI_AUDIO_CONFIG_DIR")
    if root:
        return Path(root).expanduser() / "cookies.txt"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "bili-audio" / "cookies.txt"


def bilibili_domain(domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    return domain == "bilibili.com" or domain.endswith(".bilibili.com")


def cookie_from_pair(name: str, value: str) -> http.cookiejar.Cookie:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name) or re.search(r"[\x00-\x20\x7f;]", value):
        raise SystemExit("Cookie 格式錯誤；請提供單行 Cookie header。")
    return http.cookiejar.Cookie(
        0, name, value, None, False, ".bilibili.com", True, True,
        "/", True, True, None, True, None, None, {}, False,
    )


def wbi_key_from_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    name = url.rsplit("/", 1)[-1].split(".")[0]
    return name if re.fullmatch(r"[0-9a-f]{32}", name) else None


class Session:
    def __init__(self, user_agent: str, cookie_file: str | None = None, *, empty: bool = False):
        self.user_agent = user_agent
        self._temp = tempfile.TemporaryDirectory(prefix="bili_audio_session_")
        self.path = str(Path(self._temp.name) / "cookies.txt")
        self.jar = http.cookiejar.MozillaCookieJar(self.path)
        self.source = "none"
        self._verified: dict | None = None
        self._wbi: tuple[float, str | None] | None = None
        try:
            if not empty:
                raw = os.environ.get("BILIBILI_COOKIE") or os.environ.get("BILI_COOKIE")
                if cookie_file:
                    self.load_file(Path(cookie_file).expanduser())
                    self.source = "file"
                elif raw:
                    self.load_header(raw)
                    self.source = "environment"
                elif session_path().exists():
                    self.load_file(session_path())
                    self.source = "saved"
            self.flush()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._temp.cleanup()

    def load_header(self, raw: str) -> None:
        if len(raw) > 65536:
            raise SystemExit("Cookie header 過長。")
        if re.search(r"[\r\n\x00]", raw):
            raise SystemExit("Cookie 不可包含換行或控制字元。")
        for pair in raw.split(";"):
            if not pair.strip():
                continue
            name, separator, value = pair.strip().partition("=")
            if not separator:
                raise SystemExit("Cookie 格式錯誤；需要 name=value。")
            self.jar.set_cookie(cookie_from_pair(name, value))

    def load_file(self, path: Path) -> None:
        jar = http.cookiejar.MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise SystemExit("無法讀取 Netscape cookie 檔；請確認路徑與格式。") from exc
        for cookie in jar:
            # curl writes session-cookie expiry as 0; MozillaCookieJar expects an empty field.
            if cookie.expires == 0:
                cookie.expires = None
                cookie.discard = True
            if bilibili_domain(cookie.domain) and not cookie.is_expired():
                cookie_from_pair(cookie.name, cookie.value or "")
                self.jar.set_cookie(cookie)
        try:
            for line in path.read_text().splitlines():
                if line.startswith(ACCOUNT_COMMENT):
                    data = json.loads(line[len(ACCOUNT_COMMENT):])
                    if isinstance(data, dict):
                        self._verified = data
                    break
        except (OSError, ValueError):
            pass  # Missing/broken cache just requires a fresh account check.

    def flush(self) -> None:
        try:
            self.jar.save(ignore_discard=True)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SystemExit("無法寫入暫存登入憑證，請確認暫存目錄可用。") from exc

    @property
    def has_login(self) -> bool:
        return any(c.name == "SESSDATA" and c.value and not c.is_expired() for c in self.jar)

    def request(self, url: str, timeout: float) -> dict:
        parsed = urllib.parse.urlsplit(url)
        if re.search(r"[\x00-\x20\x7f]", url) or parsed.scheme != "https" or parsed.hostname not in ("api.bilibili.com", "passport.bilibili.com", "www.bilibili.com"):
            raise SystemExit("拒絕將登入 session 用於非 Bilibili API。")
        self.flush()
        try:
            if shutil.which("curl"):
                result = subprocess.run(
                    ["curl", "-sS", "--fail", "--max-time", str(timeout),
                     "--user-agent", self.user_agent, "--referer", "https://www.bilibili.com/",
                     "--cookie", self.path, "--cookie-jar", self.path, "--config", "-"],
                    input='url = "' + url.replace("\\", "\\\\").replace('"', '\\"') + '"\n',
                    text=True, capture_output=True, check=False, timeout=timeout + 2,
                )
                if result.returncode in (130, -2):
                    raise KeyboardInterrupt
                if result.returncode:
                    # Do not echo curl diagnostics: request URLs may contain QR secrets.
                    http_error = re.search(r"returned error: (\d{3})", result.stderr)
                    if http_error:
                        code = http_error.group(1)
                        hint = "反爬檢查，請稍後重試或重新登入" if code == "412" else "請稍後重試"
                        raise SystemExit(f"Bilibili HTTP {code}；{hint}。")
                    raise SystemExit(f"Bilibili 連線失敗（curl {result.returncode}）；請確認網路，若被限流請稍後再試。")
                self.jar.clear()
                self.load_file(Path(self.path))
                body = result.stdout
            else:
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
                request = urllib.request.Request(url, headers={
                    "User-Agent": self.user_agent, "Referer": "https://www.bilibili.com/",
                })
                with opener.open(request, timeout=timeout) as response:
                    body = response.read().decode("utf-8")
            payload = json.loads(body)
        except (OSError, urllib.error.URLError, subprocess.TimeoutExpired) as exc:
            raise SystemExit("Bilibili 連線失敗或逾時；請確認網路後重試。") from exc
        except (ValueError, UnicodeError) as exc:
            raise SystemExit("Bilibili API 回傳不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise SystemExit("Bilibili API 回傳格式不正確。")
        return payload

    def ensure_buvid(self, timeout: float) -> None:
        """Browsers always carry buvid3/buvid4; without them the API answers 412 much sooner."""
        if any(c.name == "buvid3" and c.value for c in self.jar):
            return
        try:
            payload = self.request(SPI_URL, timeout)
        except SystemExit:
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        stored = False
        for name, field in (("buvid3", "b_3"), ("buvid4", "b_4")):
            value = data.get(field)
            if isinstance(value, str) and value:
                self.jar.set_cookie(cookie_from_pair(name, value))
                stored = True
        self.flush()
        # A browser keeps one buvid3 for months; a fresh one each launch is itself a signal.
        if stored and self.source == "saved":
            self.save()

    @staticmethod
    def _wbi_key_from_payload(payload: dict) -> str | None:
        # nav answers -101 when anonymous but still carries wbi_img.
        data = payload.get("data")
        images = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(images, dict):
            return None
        img = wbi_key_from_url(images.get("img_url"))
        sub = wbi_key_from_url(images.get("sub_url"))
        if not img or not sub:
            return None
        raw = img + sub
        if len(raw) <= max(WBI_MIXIN_KEY_ENC_TAB):
            return None
        return "".join(raw[index] for index in WBI_MIXIN_KEY_ENC_TAB)[:32]

    def absorb_wbi(self, payload: dict) -> None:
        """Every nav response carries wbi_img, so reuse it instead of fetching nav twice."""
        key = self._wbi_key_from_payload(payload)
        if key:
            self._wbi = (time.time(), key)

    def adopt_buvid(self, other: "Session") -> None:
        """Keep the long-lived device id when a new login replaces the account."""
        for cookie in other.jar:
            if cookie.name in ("buvid3", "buvid4") and cookie.value:
                self.jar.set_cookie(cookie_from_pair(cookie.name, cookie.value))
        self.flush()

    def adopt_wbi(self, other: "Session") -> None:
        """Carry a key fetched by a throwaway login session instead of refetching nav."""
        if other._wbi and other._wbi[1]:
            self._wbi = other._wbi

    def wbi_mixin_key(self, timeout: float) -> str | None:
        if self._wbi and time.time() - self._wbi[0] < WBI_KEY_TTL:
            return self._wbi[1]
        try:
            payload = self.request(NAV_URL, timeout)
        except SystemExit:
            payload = {}
        # Cache misses too; a nav without wbi_img must not refetch on every signed call.
        self._wbi = (time.time(), self._wbi_key_from_payload(payload))
        return self._wbi[1]

    def wbi_query(self, params: dict, timeout: float) -> str:
        """Sign a query the way the web client does; /wbi/ calls without w_rid read as a bot."""
        items = {key: str(value) for key, value in params.items()}
        items["wts"] = str(int(time.time()))
        items = {
            key: "".join(ch for ch in value if ch not in "!'()*")
            for key, value in items.items()
        }
        query = "&".join(
            f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='')}"
            for key, value in sorted(items.items())
        )
        mixin_key = self.wbi_mixin_key(timeout)
        if not mixin_key:
            return query
        signature = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return f"{query}&w_rid={signature}"

    def login_fingerprint(self) -> str:
        values = sorted((c.domain, c.path, c.value, c.expires) for c in self.jar
                        if c.name == "SESSDATA" and not c.is_expired())
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def invalidate(self) -> None:
        self._verified = None
        if self.source == "saved":
            self.save()

    def status(self, timeout: float, *, cached: bool = False) -> dict:
        if not self.has_login:
            return {"logged_in": False, "source": self.source}
        verified = self._verified or {}
        until = verified.get("until")
        if (cached and isinstance(until, (int, float)) and time.time() < until
                and verified.get("fingerprint") == self.login_fingerprint()
                and isinstance(verified.get("uid"), int) and verified["uid"] > 0
                and isinstance(verified.get("name"), str)):
            return {"logged_in": True, "uid": verified["uid"],
                    "name": verified["name"], "source": self.source}
        payload = self.request(NAV_URL, timeout)
        self.absorb_wbi(payload)
        data = payload.get("data") or {}
        if payload.get("code") == -101:
            self.invalidate()
            return {"logged_in": False, "source": self.source}
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise SystemExit("無法驗證登入狀態；請稍後重試。")
        status = {"logged_in": data.get("isLogin") is True, "uid": data.get("mid"),
                  "name": data.get("uname", ""), "source": self.source}
        if status["logged_in"]:
            expiries = [c.expires for c in self.jar if c.name == "SESSDATA" and c.expires]
            # Session cookies without an expiry get a quiet recheck after one day.
            self._verified = {"uid": status["uid"], "name": status["name"],
                              "fingerprint": self.login_fingerprint(),
                              "until": min(expiries) if expiries else time.time() + 86400}
            if self.source == "saved":
                self.save()
        else:
            self.invalidate()
        return status

    def save(self) -> None:
        target = session_path()
        temporary = None
        try:
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.flush()
            fd, temporary = tempfile.mkstemp(prefix=".cookies-", dir=target.parent)
            with os.fdopen(fd, "wb") as output:
                output.write(Path(self.path).read_bytes())
                if self._verified:
                    output.write((ACCOUNT_COMMENT + json.dumps(self._verified, ensure_ascii=True) + "\n").encode())
            os.replace(temporary, target)
        except OSError as exc:
            raise SystemExit("無法儲存登入憑證；請確認設定目錄可寫入。") from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def qr_data(payload: dict) -> dict:
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise SystemExit("Bilibili 登入 API 回應錯誤；請稍後重試。")
    return payload["data"]


def login_qr(session: Session, timeout: float, emit, wait_seconds: float = 180) -> None:
    encoder = shutil.which("qrencode")
    if not encoder:
        raise SystemExit("QR code 登入需要 qrencode：brew install qrencode；或用 login --cookies 匯入。")
    data = qr_data(session.request(QR_BASE + "generate", timeout))
    url, key = data.get("url"), data.get("qrcode_key")
    if not isinstance(url, str) or not isinstance(key, str) or not key:
        raise SystemExit("登入 API 缺少 QR code 資料。")
    emit("請用 Bilibili 手機 App 掃描 QR code，並在手機確認登入：")
    rendered = subprocess.run([encoder, "-t", "UTF8", "-m", "2"], input=url,
                              text=True, capture_output=True, check=False)
    if rendered.returncode:
        raise SystemExit("QR code 產生失敗。")
    emit(rendered.stdout)
    deadline = time.monotonic() + wait_seconds
    scanned = False
    while time.monotonic() < deadline:
        data = qr_data(session.request(QR_BASE + "poll?" + urllib.parse.urlencode({"qrcode_key": key}), timeout))
        code = data.get("code")
        if code == 0:
            # The poll response sets cookies. Never follow its credential-bearing redirect URL.
            if not session.has_login:
                raise SystemExit("登入回應未提供有效 cookies，請重新執行 login。")
            return
        if code == 86038:
            raise SystemExit("QR code 已過期，請重新執行 login。")
        if code == 86090 and not scanned:
            emit("已掃描，請在手機確認登入。")
            scanned = True
        elif code not in (86090, 86101):
            raise SystemExit("QR code 登入失敗，請重新執行 login。")
        time.sleep(2)
    raise SystemExit("等待登入逾時，請重新執行 login。")
