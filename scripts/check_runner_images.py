"""
check_runner_images.py

ดึง release ใหม่จาก actions/runner-images แล้วส่ง notification ไปที่ Discord
รัน on: GitHub Actions (scheduled / manual)
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

# OS filter — เปลี่ยนเป็น list ว่าง [] เพื่อดูทุก release
WATCH_OS = ["ubuntu", "macos", "windows"]

# จำกัดจำนวน notification สูงสุดต่อรอบ (ป้องกัน flood เวลา first run หรือ clear state)
MAX_NOTIFY_PER_RUN = 5

# หน่วงเวลาระหว่าง Discord request (วินาที) — Discord rate limit ~5 req/2s per webhook
DISCORD_RATE_LIMIT_DELAY = 1.5

# สี embed ตาม OS
COLORS = {
    "ubuntu":  0xE95420,
    "macos":   0x555555,
    "windows": 0x0078D4,
}
DEFAULT_COLOR = 0x5865F2


# ─── HTML Table Parser ────────────────────────────────────────────────────────

class TableParser(HTMLParser):
    """แปลง HTML <table> เป็น list of rows (list of cells)"""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: str = ""
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._current_table = []
        elif tag in ("tr",):
            self._current_row = []
        elif tag in ("th", "td"):
            self._current_cell = ""
            self._in_cell = True

    def handle_endtag(self, tag):
        if tag == "table":
            self.tables.append(self._current_table)
            self._current_table = []
        elif tag == "tr":
            if self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = []
        elif tag in ("th", "td"):
            self._current_row.append(self._current_cell.strip())
            self._in_cell = False

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell += data


def table_to_text(rows: list[list[str]]) -> str:
    """แปลง rows เป็น text สำหรับ Discord"""
    if not rows:
        return ""

    lines = []
    headers = rows[0]
    data_rows = rows[1:]

    # หา index ของ column ที่สนใจ: Tool name, Previous, Current
    try:
        tool_idx = next(i for i, h in enumerate(headers) if "tool" in h.lower() or "name" in h.lower())
    except StopIteration:
        tool_idx = 0

    prev_idx = next((i for i, h in enumerate(headers) if "previous" in h.lower()), None)
    curr_idx = next((i for i, h in enumerate(headers) if "current" in h.lower()), None)

    for row in data_rows:
        if len(row) <= tool_idx:
            continue
        tool = row[tool_idx]
        if not tool:  # rowspan cell ว่าง — ข้ามได้
            continue

        if prev_idx is not None and curr_idx is not None:
            prev = row[prev_idx] if prev_idx < len(row) else "?"
            curr = row[curr_idx] if curr_idx < len(row) else "?"
            lines.append(f"• **{tool}**: `{prev}` → `{curr}`")
        else:
            lines.append(f"• {tool}")

    return "\n".join(lines)


def convert_html_tables(body: str) -> str:
    """แทน <table>...</table> ใน body ด้วย plain text"""
    # ดึง table blocks ทั้งหมด
    table_pattern = re.compile(r"<table>.*?</table>", re.DOTALL | re.IGNORECASE)
    table_blocks = table_pattern.findall(body)

    if not table_blocks:
        return body

    parser = TableParser()
    result = body

    for html_block in table_blocks:
        parser.__init__()
        parser.feed(html_block)
        if parser.tables:
            text = table_to_text(parser.tables[0])
            result = result.replace(html_block, text if text else "_(ไม่มีข้อมูล)_")

    return result


# ─── Body Formatter ───────────────────────────────────────────────────────────

def format_body(raw_body: str, max_chars: int = 1800) -> str:
    if not raw_body:
        return "_ไม่มีรายละเอียดใน release นี้_"

    body = convert_html_tables(raw_body)

    # ตัด section ยาวๆ ที่ไม่จำเป็น (Installed Software / full tool list)
    # เก็บแค่ Announcements + What's changed (Updated)
    keep_sections = []
    current_section = []
    skip = False

    for line in body.splitlines():
        # ข้าม section ที่ซ้ำซ้อน/ยาวเกิน
        if re.match(r"#+\s*(Installed Software|Full list|Cached tools)", line, re.IGNORECASE):
            skip = True
        elif re.match(r"#+\s", line):
            skip = False

        if not skip:
            keep_sections.append(line)

    body = "\n".join(keep_sections).strip()

    # ตัด blank lines เกิน 2 บรรทัด
    body = re.sub(r"\n{3,}", "\n\n", body)

    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n_...ดูเพิ่มเติมที่ GitHub_"

    return body


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


def fetch_releases(per_page: int = 50) -> list:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/releases",
        headers=gh_headers(),
        params={"per_page": per_page},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


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
    time.sleep(DISCORD_RATE_LIMIT_DELAY)  # หน่วงเพื่อหลีก Discord rate limit


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
        if WATCH_OS and not any(os_key in r["tag_name"].lower() for os_key in WATCH_OS):
            continue
        new_releases.append(r)

    if not new_releases:
        print("✅ No new releases to notify.")
        return

    total = len(new_releases)
    print(f"Found {total} new release(s).")

    if total > MAX_NOTIFY_PER_RUN:
        # ส่งแค่ batch แรก (เก่าสุด MAX_NOTIFY_PER_RUN อัน) แล้วรอบถัดไปค่อยส่งต่อ
        # new_releases[0] = newest, [-1] = oldest → เอา oldest batch ก่อน
        print(f"⚠️  Too many at once ({total}) — sending oldest {MAX_NOTIFY_PER_RUN} first, the rest will follow in the next run.")
        to_send = new_releases[-MAX_NOTIFY_PER_RUN:]   # oldest MAX_NOTIFY_PER_RUN
        next_last_seen = new_releases[-(MAX_NOTIFY_PER_RUN + 1)]["tag_name"] if len(new_releases) > MAX_NOTIFY_PER_RUN else releases[0]["tag_name"]
    else:
        to_send = new_releases
        next_last_seen = releases[0]["tag_name"]

    print(f"Sending {len(to_send)} notification(s) to Discord...")

    for r in reversed(to_send):  # ส่งจากเก่าไปใหม่
        send_discord(r)

    save_last_seen(next_last_seen)
    print(f"State updated → {next_last_seen}")


if __name__ == "__main__":
    main()
