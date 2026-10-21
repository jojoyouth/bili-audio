"""Optional network smoke test. Does not read or persist account credentials."""

from pathlib import Path
import subprocess
import sys
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bili_audio import DEFAULT_USER_AGENT, RECOMMEND_API_URL, videos_from_items  # noqa: E402
from bili_auth import NAV_URL, QR_BASE, Session, qr_data  # noqa: E402


def main():
    with Session(DEFAULT_USER_AGENT, empty=True) as session:
        data = qr_data(session.request(QR_BASE + "generate", 15))
        assert isinstance(data["url"], str) and data["qrcode_key"]
        rendered = subprocess.run(["qrencode", "-t", "UTF8"], input=data["url"],
                                  text=True, capture_output=True, check=True)
        assert rendered.stdout.strip()
        print("PASS: live QR generation and local rendering")
        poll = qr_data(session.request(QR_BASE + "poll?" + urllib.parse.urlencode({"qrcode_key": data["qrcode_key"]}), 15))
        assert poll["code"] == 86101, "fresh QR should be waiting for scan"
        print("PASS: live QR polling (waiting for scan)")
        nav = session.request(NAV_URL, 15)
        assert nav["code"] == -101
        print("PASS: anonymous account status")
        feed = session.request(RECOMMEND_API_URL + "?fresh_idx=1&fresh_type=4&feed_version=V8&homepage_ver=1&brush=0&y_num=5&ps=10", 15)
        assert feed["code"] == 0 and isinstance(feed["data"]["item"], list)
        videos = videos_from_items([item for item in feed["data"]["item"] if item.get("goto") == "av"])
        assert videos, "feed should include playable videos"
        print(f"PASS: live feed schema ({len(videos)} anonymous videos; personalization NOT verified)")


if __name__ == "__main__":
    main()
