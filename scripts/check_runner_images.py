"""
check_runner_images.py

ดึง release ใหม่จาก actions/runner-images แล้วส่ง notification ไปที่ Discord
format: Option B Full Detail — Announcements + Image Info fields + Changes per category
"""

import requests
import os
import sys
import re
import time
from html.parser import HTMLParser

# ─── Config ──────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
GH_TOKEN        = os.environ.get("GH_TOKEN", "")
REPO            = "actions/runner-images"
STATE_FILE      = ".last_runner_release"

WATCH_OS = ["ubuntu", "macos", "windows"]

MAX_NOTIFY_PER_RUN   = 5
DISCORD_RATE_LIMIT   = 1.5   # วินาที ระหว่าง request

COLORS = {
    "ubuntu":  0xE95420,
    "macos":   0x555555,
    "windows": 0x0078D4,
}
DEFAULT_COLOR = 0x5865F2

# ─── HTML Table Parser ────────────────────────────────────────────────────────

class ChangeTableParser(HTMLParser):
    """
    Parse <table> ของ GitHub runner-images release
    รองรับ <td rowspan="N"> — category cell จะปรากฏครั้งเดียวแล้วหายไปใน row ถัดๆ ไป
    ผลลัพธ์: list of dict {category, tool, prev, curr}
    """

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self._current_row: list[str] = []
        self._current_cell: str = ""
        self._in_cell: bool = False
        self._in_thead: bool = False
        self._current_category: str = ""

    def handle_starttag(self, tag, attrs):
        if tag == "thead":
            self._in_thead = True
        elif tag == "tbody":
            self._in_thead = False
        elif tag == "tr":
            self._current_row = []
        elif tag in ("th", "td"):
            self._current_cell = ""
            self._in_cell = True

    def handle_endtag(self, tag):
        if tag == "thead":
            self._in_thead = False
        elif tag in ("th", "td"):
            self._current_row.append(self._current_cell.strip())
            self._in_cell = False
        elif tag == "tr":
            if not self._in_thead:
                self._process_row(self._current_row)
            self._current_row = []

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell += data

    def _process_row(self, row: list[str]):
        if len(row) == 4:
            # [category, tool, prev, curr]
            cat = row[0].strip()
            if cat:
                self._current_category = cat
            tool, prev, curr = row[1], row[2], row[3]
        elif len(row) == 3:
            # rowspan → ไม่มี category cell
            tool, prev, curr = row[0], row[1], row[2]
        else:
            return

        if tool.strip():
            self.rows.append({
                "category": self._current_category,
                "tool":     tool.strip(),
                "prev":     prev.strip(),
                "curr":     curr.strip(),
            })


# ─── Body Parsers ─────────────────────────────────────────────────────────────

def parse_announcements(body: str) -> list[str]:
    """ดึง announcement lines จาก markdown table แบบนี้:
    | Announcements |
    |-|
    | some text |
    """
    lines = []
    in_announce = False
    for line in body.splitlines():
        s = line.strip()
        if re.match(r"\|\s*Announcements\s*\|", s, re.IGNORECASE):
            in_announce = True
            continue
        if in_announce:
            if re.match(r"\|[-\s|]+\|", s):   # separator row
                continue
            if s.startswith("|") and s.endswith("|"):
                content = s[1:-1].strip()
                # ลบ markdown link เหลือแค่ text
                content = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", content)
                if content:
                    lines.append(content)
            else:
                break   # ออกจาก table
    return lines


def parse_image_info(body: str) -> dict:
    """ดึง OS Version, Kernel Version, Image Version จากบรรทัดแบบ:
    • OS Version: macOS 26.3.1 (25D2128)
    """
    result = {}
    mapping = {
        "OS Version": r"[•*-]\s*OS Version:\s*(.+)",
        "Kernel":     r"[•*-]\s*Kernel Version:\s*(.+)",
        "Image":      r"[•*-]\s*Image Version:\s*(.+)",
    }
    for key, pattern in mapping.items():
        m = re.search(pattern, body)
        if m:
            result[key] = m.group(1).strip()
    return result


def parse_changes(body: str) -> dict[str, list[dict]]:
    """ดึง HTML tables แล้ว group by category → {category: [{tool, prev, curr}]}"""
    table_blocks = re.findall(r"<table>.*?</table>", body, re.DOTALL | re.IGNORECASE)
    groups: dict[str, list[dict]] = {}

    for block in table_blocks:
        parser = ChangeTableParser()
        parser.feed(block)
        for row in parser.rows:
            cat = row["category"] or "Other"
            groups.setdefault(cat, []).append(row)

    return groups


# ─── Discord Payload Builder ──────────────────────────────────────────────────

def get_color(tag: str) -> int:
    for key, color in COLORS.items():
        if key in tag.lower():
            return color
    return DEFAULT_COLOR


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def build_discord_payload(release: dict) -> dict:
    tag  = release["tag_name"]
    body = release.get("body", "") or ""

    # ── 1. Parse sections ──────────────────────────────────────────────────
    announcements = parse_announcements(body)
    image_info    = parse_image_info(body)
    changes       = parse_changes(body)

    # ── 2. Description — Announcements ────────────────────────────────────
    desc_parts = []
    for ann in announcements:
        is_warn = any(kw in ann.lower() for kw in
                      ["deprecat", "unsupport", "removal", "end of life", "will be"])
        prefix = "⚠️" if is_warn else "✅"
        desc_parts.append(f"{prefix} {ann}")

    description = "\n".join(desc_parts) if desc_parts else None

    # ── 3. Fields ──────────────────────────────────────────────────────────
    fields = []

    # 3a. Image Info (inline, 3 columns)
    for key in ("OS Version", "Kernel", "Image"):
        if key in image_info:
            fields.append({"name": key, "value": image_info[key], "inline": True})

    # 3b. Changes per category
    if changes:
        total_tools = sum(len(v) for v in changes.values())
        fields.append({
            "name":   "📢 What's changed?",
            "value":  f"{total_tools} tool(s) updated across {len(changes)} category(ies)",
            "inline": False,
        })

        for cat, rows in changes.items():
            lines = []
            for row in rows:
                prev = _truncate(row["prev"], 30)
                curr = _truncate(row["curr"], 30)
                lines.append(f"• **{row['tool']}**: `{prev}` → `{curr}`")

            value = _truncate("\n".join(lines), 1024)
            fields.append({"name": cat, "value": value, "inline": False})

            # Discord limit: max 25 fields per embed
            if len(fields) >= 24:
                fields.append({
                    "name":   "...",
                    "value":  "_ดูรายละเอียดทั้งหมดที่ GitHub_",
                    "inline": False,
                })
                break

    # ── 4. Assemble embed ─────────────────────────────────────────────────
    embed: dict = {
        "title":     f"🖥️ Runner Image Update: `{tag}`",
        "url":       release["html_url"],
        "color":     get_color(tag),
        "fields":    fields,
        "footer":    {"text": f"GitHub / {REPO}"},
        "timestamp": release["published_at"],
    }
    if description:
        embed["description"] = _truncate(description, 4096)

    return {"embeds": [embed]}


# ─── GitHub + State Helpers ───────────────────────────────────────────────────

def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    if GH_TOKEN:
        h["Authorization"] = f"Bearer {GH_TOKEN}"
    return h


def fetch_releases(per_page: int = 50) -> list:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/releases",
        headers=gh_headers(),
        params={"per_page": per_page},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def load_last_seen() -> str:
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save_last_seen(tag: str):
    with open(STATE_FILE, "w") as f:
        f.write(tag)


# ─── Discord Sender ───────────────────────────────────────────────────────────

def send_discord(release: dict):
    payload = build_discord_payload(release)
    resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"  ✅ Sent: {release['tag_name']}")
    time.sleep(DISCORD_RATE_LIMIT)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not DISCORD_WEBHOOK:
        print("❌ DISCORD_WEBHOOK is not set")
        sys.exit(1)

    releases  = fetch_releases()
    last_seen = load_last_seen()
    print(f"Last seen release : {last_seen or '(none — first run)'}")
    print(f"Latest on GitHub  : {releases[0]['tag_name'] if releases else '(none)'}")

    new_releases = []
    for r in releases:
        if r["tag_name"] == last_seen:
            break
        if WATCH_OS and not any(k in r["tag_name"].lower() for k in WATCH_OS):
            continue
        new_releases.append(r)

    if not new_releases:
        print("✅ No new releases to notify.")
        return

    total = len(new_releases)
    print(f"Found {total} new release(s).")

    if total > MAX_NOTIFY_PER_RUN:
        print(f"⚠️  Capping at {MAX_NOTIFY_PER_RUN} (oldest first) — remainder will follow next run.")
        to_send       = new_releases[-MAX_NOTIFY_PER_RUN:]
        next_last_seen = new_releases[-(MAX_NOTIFY_PER_RUN + 1)]["tag_name"]
    else:
        to_send        = new_releases
        next_last_seen = releases[0]["tag_name"]

    print(f"Sending {len(to_send)} notification(s)...")
    for r in reversed(to_send):   # เก่า → ใหม่
        send_discord(r)

    save_last_seen(next_last_seen)
    print(f"State updated → {next_last_seen}")


if __name__ == "__main__":
    main()
