#!/usr/bin/env python3
"""
Telegram Bot for v2ray log monitoring.
- On boot, schedules auto-send of /var/log/v2ray/access.log after 5h50m
- /start - register user
- /log   - manually request log file
- /uptime - show system uptime and countdown

Configuration via environment variables:
  BOT_TOKEN    - Telegram Bot API token (required)
  CHAT_IDS     - Comma-separated hardcoded chat IDs, e.g. "123456789" (required)
"""

import os
import sys
import time
import json
import threading
import subprocess
import urllib.request
import urllib.error
import logging
import tempfile
import shutil
from datetime import datetime

# ============ Configuration ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/v2ray/access.log")
USERS_FILE = os.environ.get("USERS_FILE", "/opt/tgbot/users.txt")
AUTO_SEND_DELAY = 5 * 3600 + 50 * 60  # 5h50m = 21000 seconds

# Hardcoded known users from env - ensures auto-send works on fresh runners without /start
# Format: comma-separated, e.g. ",123456789"
_chat_ids_env = os.environ.get("CHAT_IDS", "")
KNOWN_USERS = [int(x.strip()) for x in _chat_ids_env.split(",") if x.strip()] if _chat_ids_env else []

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

# ============ Logging ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("tgbot")


# ============ System helpers ============
def get_boot_time():
    """Get system boot time as Unix timestamp from /proc/stat."""
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('btime'):
                    return int(line.split()[1])
    except Exception as e:
        log.warning(f"Failed to read btime: {e}")
    return int(time.time())  # fallback


def get_uptime():
    """Get system uptime in seconds."""
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def format_duration(seconds):
    """Format seconds into 'Xh Ym Zs'."""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"


# ============ User management ============
def load_users():
    """Load all users: hardcoded known users + registered users from file."""
    users = list(KNOWN_USERS)  # start with hardcoded users
    try:
        with open(USERS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    uid = int(line)
                    if uid not in users:
                        users.append(uid)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error(f"Error loading users: {e}")
    return users


def add_user(chat_id):
    """Register a new user if not already known."""
    users = load_users()
    if chat_id not in users:
        os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
        with open(USERS_FILE, 'a') as f:
            f.write(f"{chat_id}\n")
        log.info(f"New user registered: {chat_id}")
        return True
    return False


# ============ Telegram API ============
def tg_send_message(chat_id, text):
    """Send a text message via Telegram API."""
    try:
        data = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"{API_BASE}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        log.error(f"sendMessage failed: {e}")
        return False


def tg_send_document(chat_id, file_path, caption=""):
    """Send a file as document via Telegram API using curl.
    Copies file to a temp location first to avoid permission issues with mode 600 files."""
    tmp_path = None
    try:
        # Copy to temp file with read permissions to avoid curl issues with restricted files
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.log', prefix='tgbot_')
        shutil.copy2(file_path, tmp_path)
        os.chmod(tmp_path, 0o644)
        os.close(tmp_fd)

        cmd = [
            "curl", "-s", "-X", "POST",
            f"{API_BASE}/sendDocument",
            "-F", f"chat_id={chat_id}",
            "-F", f"document=@{tmp_path};filename={os.path.basename(file_path)}",
            "-F", f"caption={caption}",
            "--max-time", "120"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=130)

        if not result.stdout:
            log.error(f"sendDocument: curl returned empty stdout, stderr: {result.stderr}")
            return False

        resp = json.loads(result.stdout)
        if resp.get("ok"):
            log.info(f"Document sent to {chat_id}: {file_path} ({os.path.getsize(file_path)} bytes)")
            return True
        else:
            log.error(f"sendDocument failed: {resp}")
            return False
    except Exception as e:
        log.error(f"sendDocument error: {e}")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ============ Auto-send timer ============
def auto_send_log():
    """Background thread: wait until boot+5h50m, then send log to all users."""
    boot_time = get_boot_time()
    target_time = boot_time + AUTO_SEND_DELAY
    now = time.time()
    wait = target_time - now

    if wait > 0:
        log.info(f"Auto-send scheduled in {format_duration(wait)} "
                 f"(target: {datetime.fromtimestamp(target_time).strftime('%Y-%m-%d %H:%M:%S')})")
        time.sleep(wait)
    else:
        log.warning(f"Boot+5h50m already passed (overshot by {format_duration(-wait)}), sending now")

    users = load_users()
    if not users:
        log.warning("No registered users, skipping auto-send")
        return

    log.info(f"Auto-send to {len(users)} users: {users}")
    uptime = get_uptime()
    caption = f"Auto-sent v2ray log | uptime: {format_duration(uptime)} | {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    if not os.path.exists(LOG_FILE):
        log.error(f"Log file not found: {LOG_FILE}")
        for uid in users:
            tg_send_message(uid, f"⚠️ 自动发送失败：日志文件不存在 {LOG_FILE}")
        return

    for uid in users:
        success = tg_send_document(uid, LOG_FILE, caption)
        if not success:
            tg_send_message(uid, "⚠️ 自动发送日志文件失败，请手动使用 /log 获取")
    log.info("Auto-send complete")


# ============ Command handlers ============
def handle_start(chat_id):
    """Handle /start command - register user."""
    is_new = add_user(chat_id)
    if is_new:
        msg = ("✅ Bot 已启动，你已注册成功！\n\n"
               f"系统开机后 {format_duration(AUTO_SEND_DELAY)} 将自动发送 v2ray 日志文件。\n\n"
               "可用命令：\n"
               "📄 /log - 手动获取日志文件\n"
               "⏱ /uptime - 查看系统运行时长和倒计时")
    else:
        msg = "你已注册，无需重复注册。\n\n可用命令：\n📄 /log - 手动获取日志文件\n⏱ /uptime - 查看运行时长"
    tg_send_message(chat_id, msg)


def handle_log(chat_id):
    """Handle /log command - send log file."""
    if not os.path.exists(LOG_FILE):
        tg_send_message(chat_id, f"❌ 日志文件不存在: {LOG_FILE}")
        return

    file_size = os.path.getsize(LOG_FILE)
    size_str = f"{file_size / 1024 / 1024:.2f}MB" if file_size > 1024 * 1024 else f"{file_size / 1024:.1f}KB"
    tg_send_message(chat_id, f"📄 正在发送日志文件 ({size_str})...")

    uptime = get_uptime()
    caption = f"v2ray access.log | {size_str} | uptime: {format_duration(uptime)}"
    success = tg_send_document(chat_id, LOG_FILE, caption)
    if not success:
        tg_send_message(chat_id, "❌ 发送失败，请稍后重试 /log")


def handle_uptime(chat_id):
    """Handle /uptime command - show uptime and countdown."""
    uptime = get_uptime()
    boot_time = get_boot_time()
    target = boot_time + AUTO_SEND_DELAY
    remaining = target - time.time()

    msg = f"⏱ 系统运行时长: {format_duration(uptime)}\n"
    msg += f"📅 开机时间: {datetime.fromtimestamp(boot_time).strftime('%Y-%m-%d %H:%M:%S')}\n"

    if remaining > 0:
        msg += f"⏳ 距自动发送日志还有: {format_duration(remaining)}"
    else:
        msg += "✅ 自动发送已触发"
    tg_send_message(chat_id, msg)


def handle_update(update):
    """Process a single Telegram update."""
    message = update.get('message') or update.get('edited_message')
    if not message:
        return

    chat_id = message.get('chat', {}).get('id')
    text = message.get('text', '').strip()
    if not chat_id or not text:
        return

    log.info(f"Received from {chat_id}: {text}")

    if text == '/start':
        handle_start(chat_id)
    elif text == '/log':
        handle_log(chat_id)
    elif text == '/uptime':
        handle_uptime(chat_id)
    elif text.startswith('/'):
        tg_send_message(chat_id, "未知命令。可用命令：\n/start - 注册\n/log - 获取日志\n/uptime - 运行时长")


# ============ Main loop ============
def main():
    # Validate required config
    if not BOT_TOKEN:
        log.error("BOT_TOKEN environment variable is not set. Exiting.")
        sys.exit(1)
    if not KNOWN_USERS:
        log.warning("CHAT_IDS not set - no hardcoded users. Auto-send will only work for users registered via /start on this runner instance.")

    log.info(f"TG Bot starting | log file: {LOG_FILE} | auto-send delay: {format_duration(AUTO_SEND_DELAY)}")
    log.info(f"Known users: {KNOWN_USERS}")

    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)

    # Start auto-send background thread
    timer_thread = threading.Thread(target=auto_send_log, daemon=True)
    timer_thread.start()

    # Long polling loop
    offset = 0
    while True:
        try:
            data = json.dumps({"offset": offset, "timeout": 30}).encode()
            req = urllib.request.Request(
                f"{API_BASE}/getUpdates",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            resp = urllib.request.urlopen(req, timeout=35)
            result = json.loads(resp.read())

            for update in result.get('result', []):
                offset = update['update_id'] + 1
                handle_update(update)

        except urllib.error.URLError as e:
            log.error(f"Network error: {e}")
            time.sleep(10)
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            time.sleep(5)


if __name__ == '__main__':
    main()
