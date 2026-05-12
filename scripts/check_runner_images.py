"""
check_runner_images.py

ดึง release ใหม่จาก actions/runner-images แล้วส่ง notification ไปที่ Discord
รัน on: GitHub Actions (scheduled / manual)
"""

import requests
import os
import sys

# ─── Config ──────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
GH_TOKEN        = os.environ.get("GH_TOKEN", "")
REPO            = "actions/runner-images"
STATE_FILE      = ".last_runner_release"

# OS filter — เปลี่ยนเป็น list ว่าง [] เพื่อดูทุก release
WATCH_OS = ["ubuntu", "macos", "windows"]

# สี embed ตาม OS
COLORS = {
    "ubuntu":  0xE95420,   # orange
    "macos":   0x555555,   # dark gray
    "windows": 0x0078D4,   # blue
}
DEFAULT_COLOR = 0x5865F2   # Discord purple


# ─── Helpers ──────────────────────────────────────────────────────────────────

def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


def get_color(tag: str) -> int:
    for key, color in COLORS.items():
        if key in tag.lower():
            return color
    return DEFAULT_COLOR


def load_last_seen() -> str:
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save_last_seen(tag: str):
    with open(STATE_FILE, "w") as f:
        f.write(tag)


def fetch_releases(per_page: int = 20) -> list:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/releases",
        headers=gh_headers(),
        params={"per_page": per_page},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def format_body(body: str, max_chars: int = 1500) -> str:
    """ตัด body ให้พอดี Discord embed limit"""
    if not body:
        return "_ไม่มีรายละเอียดใน release นี้_"
    if len(body) > max_chars:
        return body[:max_chars] + "\n\n_...ดูเพิ่มเติมที่ GitHub_"
    return body


def send_discord(release: dict):
    tag  = release["tag_name"]
    body = format_body(release.get("body", ""))

    payload = {
        "embeds": [{
            "title":       f"🖥️ Runner Image Update: `{tag}`",
            "url":         release["html_url"],
            "description": body,
            "color":       get_color(tag),
            "footer":      {"text": f"GitHub / {REPO}"},
            "timestamp":   release["published_at"],
        }]
    }

    resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"  ✅ Sent: {tag}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not DISCORD_WEBHOOK:
        print("❌ DISCORD_WEBHOOK is not set")
        sys.exit(1)

    releases   = fetch_releases()
    last_seen  = load_last_seen()
    print(f"Last seen release : {last_seen or '(none — first run)'}")
    print(f"Latest on GitHub  : {releases[0]['tag_name'] if releases else '(none)'}")

    # หา release ที่ยังไม่เคยแจ้ง (ใหม่กว่า last_seen)
    new_releases = []
    for r in releases:
        if r["tag_name"] == last_seen:
            break
        # filter เฉพาะ OS ที่สนใจ (ถ้า WATCH_OS ว่างให้ผ่านทุกตัว)
        if WATCH_OS and not any(os_key in r["tag_name"].lower() for os_key in WATCH_OS):
            continue
        new_releases.append(r)

    if not new_releases:
        print("✅ No new releases to notify.")
        return

    print(f"Found {len(new_releases)} new release(s) — sending to Discord...")

    # ส่งจากเก่าไปใหม่ เพื่อให้ Discord แสดงตามลำดับเวลา
    for r in reversed(new_releases):
        send_discord(r)

    # อัปเดต state (เป็น release ที่ใหม่ที่สุดจาก GitHub ไม่ใช่ filtered)
    # เพื่อไม่ให้ข้าม release ที่อาจ filter ไปแล้วในรอบถัดไป
    all_new = [r for r in releases if r["tag_name"] != last_seen]
    if all_new:
        # หยุดที่ last_seen จริงๆ
        idx = next((i for i, r in enumerate(releases) if r["tag_name"] == last_seen), len(releases))
        latest_tag = releases[0]["tag_name"]
        save_last_seen(latest_tag)
        print(f"State updated → {latest_tag}")


if __name__ == "__main__":
    main()
