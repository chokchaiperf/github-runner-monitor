# GitHub Runner Images Monitor

ติดตาม release ใหม่ของ [actions/runner-images](https://github.com/actions/runner-images) แล้วส่ง notification ไปที่ Discord อัตโนมัติ

## วิธีตั้งค่า

1. **เพิ่ม Secret** ใน repo นี้:
   - ไปที่ Settings → Secrets and variables → Actions
   - เพิ่ม `DISCORD_WEBHOOK_URL` = Discord webhook URL ของคุณ

2. **Push ขึ้น GitHub** แล้ว workflow จะรันอัตโนมัติทุก 6 ชั่วโมง

3. **ทดสอบ** ได้ที่ Actions → Monitor GitHub Runner Images → Run workflow

## ไฟล์สำคัญ

| ไฟล์ | หน้าที่ |
|------|---------|
| `.github/workflows/monitor-runner-images.yml` | Scheduled workflow |
| `scripts/check_runner_images.py` | Logic ดึง release และส่ง Discord |
| `.last_runner_release` | เก็บ tag ล่าสุดที่แจ้งไปแล้ว (auto-updated) |

## ปรับแต่ง

เปิดไฟล์ `scripts/check_runner_images.py` แล้วแก้ `WATCH_OS`:

```python
# ติดตามทุก OS
WATCH_OS = []

# ติดตามเฉพาะ macOS
WATCH_OS = ["macos"]

# ติดตาม Ubuntu และ Windows
WATCH_OS = ["ubuntu", "windows"]
```
