#!/usr/bin/env python3
"""
Telegram Server Bot — improved version v2.
Fixes: commands like `ping` that need Ctrl+C no longer hang.
  - /run always non-blocking (threaded), bot stays responsive
  - start_new_session=True for clean process-group isolation
  - On timeout: SIGINT first (Ctrl+C), wait 3s, then SIGKILL if needed
  - Distinguishes SIGINT vs SIGKILL in output
  - Configurable per-command timeout: /run 10 ping google.com
  - /ps correctly tracks running tasks
  - /getfile: auto gzip-compress and/or split large files to fit Telegram's 50MB limit
"""

import os
import signal
import time
import gzip
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import requests


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("请设置环境变量 BOT_TOKEN")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 默认命令最大执行时间（秒）
DEFAULT_COMMAND_TIMEOUT = 30

# 最大允许超时（秒）
MAX_COMMAND_TIMEOUT = 600

# Telegram 普通消息最大长度大约 4096
MAX_MESSAGE_LENGTH = 4000

# 文件上传超时
UPLOAD_TIMEOUT = 600

# Telegram Bot API sendDocument hard limit is 50 MB.
# Use 48 MB as the safe threshold to leave room for multipart encoding overhead.
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024   # 50 MB — Telegram's hard limit
SAFE_LIMIT          = 48 * 1024 * 1024   # 48 MB — our working threshold
CHUNK_RAW_SIZE      = 40 * 1024 * 1024   # 40 MB raw per chunk before gzip


# ============================================================
# Telegram API
# ============================================================

def telegram(method, **kwargs):
    url = f"{API}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=60)
        return r.json()
    except Exception as e:
        print(f"Telegram API error: {e}")
        return None


def send_message(chat_id, text):
    if len(text) > MAX_MESSAGE_LENGTH:
        return send_text_file(chat_id, text, filename="command-output.txt")
    return telegram("sendMessage", chat_id=chat_id, text=text)


def send_text_file(chat_id, text, filename="output.txt"):
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as f:
            f.write(text)
            tmp_path = f.name
        return send_file(chat_id, tmp_path, caption=filename, telegram_filename=filename)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def send_file(chat_id, file_path, caption=None, telegram_filename=None):
    """Low-level: send a single file via Telegram sendDocument.

    Caller must ensure the file is under TELEGRAM_FILE_LIMIT.
    """
    path = Path(file_path)
    if not path.exists():
        return {"ok": False, "error": f"文件不存在: {file_path}"}
    if not path.is_file():
        return {"ok": False, "error": f"不是普通文件: {file_path}"}
    if telegram_filename is None:
        telegram_filename = path.name
    url = f"{API}/sendDocument"
    try:
        with path.open("rb") as f:
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            response = requests.post(
                url, data=data, files={"document": (telegram_filename, f)},
                timeout=UPLOAD_TIMEOUT
            )
            return response.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================
# Smart file sending (compress + split)
# ============================================================

def _format_size(n):
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    elif n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _gzip_to_temp(src_path, suffix=".gz"):
    """Gzip-compress a file to a temp file, return (tmp_path, compressed_size)."""
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="tgbot_")
    os.close(fd)
    with open(src_path, "rb") as f_in:
        with gzip.open(tmp_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    return tmp_path, os.path.getsize(tmp_path)


def _gzip_bytes_to_temp(data, suffix=".gz"):
    """Gzip-compress raw bytes to a temp file, return (tmp_path, compressed_size)."""
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="tgbot_")
    os.close(fd)
    with gzip.open(tmp_path, "wb") as f:
        f.write(data)
    return tmp_path, os.path.getsize(tmp_path)


def _read_chunk_at_line_boundary(f, max_bytes):
    """Read up to max_bytes, extend to next newline if stopped mid-line."""
    chunk = f.read(max_bytes)
    if not chunk:
        return b""
    if len(chunk) == max_bytes and not chunk.endswith(b"\n"):
        rest = f.readline()
        chunk += rest
    return chunk


def send_file_smart(chat_id, file_path, caption=None):
    """Send a file with automatic gzip compression and splitting.

    Decision tree:
      file <= 48MB  -> send directly
      file >  48MB  -> gzip compress
        compressed <= 48MB -> send .gz
        compressed >  48MB -> split at line boundaries, gzip each chunk, send all
    """
    path = Path(file_path)
    if not path.exists():
        return {"ok": False, "error": f"文件不存在: {file_path}"}
    if not path.is_file():
        return {"ok": False, "error": f"不是普通文件: {file_path}"}

    file_size = path.stat().st_size
    print(f"[GETFILE] {file_path} ({_format_size(file_size)})")

    # ---- Case 1: small enough, send directly ----
    if file_size <= SAFE_LIMIT:
        return send_file(chat_id, file_path, caption=caption)

    # ---- Case 2: too large, try gzip compression ----
    print(f"[GETFILE] {_format_size(file_size)} > {_format_size(SAFE_LIMIT)}, compressing...")
    send_message(chat_id, f"📦 文件较大 ({_format_size(file_size)})，正在压缩...")

    gz_path = None
    try:
        gz_path, gz_size = _gzip_to_temp(file_path, suffix=path.suffix + ".gz")

        if gz_size <= SAFE_LIMIT:
            # ---- Case 2a: compressed fits ----
            print(f"[GETFILE] compressed to {_format_size(gz_size)}, sending single .gz")
            result = send_file(
                chat_id, gz_path,
                caption=f"{caption or ''} | gzip {_format_size(file_size)}→{_format_size(gz_size)}".strip(" |"),
                telegram_filename=path.name + ".gz",
            )
            return result
        else:
            # ---- Case 2b: still too large, split + per-chunk gzip ----
            print(f"[GETFILE] compressed still {_format_size(gz_size)} > {_format_size(SAFE_LIMIT)}, splitting...")
            return _send_file_split(chat_id, file_path, file_size, caption)

    except Exception as e:
        print(f"[GETFILE] compress/split error: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if gz_path and os.path.exists(gz_path):
            os.unlink(gz_path)


def _send_file_split(chat_id, file_path, file_size, caption=None):
    """Split file at line boundaries, gzip each chunk, send each separately."""
    estimated = max(1, (file_size + CHUNK_RAW_SIZE - 1) // CHUNK_RAW_SIZE)
    send_message(
       chat_id,
        f"📄 文件 {_format_size(file_size)} 压缩后仍超限，分割为约 {estimated} 部分逐个压缩发送"
    )

    part_num = 0
    all_ok = True

    with open(file_path, "rb") as f:
        while True:
            chunk = _read_chunk_at_line_boundary(f, CHUNK_RAW_SIZE)
            if not chunk:
                break

            part_num += 1
            gz_path, gz_size = _gzip_bytes_to_temp(chunk, suffix=f".part{part_num:02d}.gz")

            if gz_size <= SAFE_LIMIT:
                ok = send_file(
                    chat_id, gz_path,
                    caption=f"{caption or ''} | 部分 {part_num} | {_format_size(len(chunk))}→{_format_size(gz_size)}".strip(" |"),
                    telegram_filename=Path(file_path).name + f".part{part_num:02d}.gz",
                )
                if not ok or not ok.get("ok"):
                    all_ok = False
            else:
                # Edge case: even chunk gzip too large, binary-split compressed data
                print(f"[GETFILE] part {part_num} still {_format_size(gz_size)} > limit, binary-splitting")
                with open(gz_path, "rb") as gf:
                    gz_data = gf.read()
                sub = 0
                for start in range(0, len(gz_data), SAFE_LIMIT):
                    sub += 1
                    sub_data = gz_data[start:start + SAFE_LIMIT]
                    fd, sub_path = tempfile.mkstemp(suffix=f".part{part_num:02d}_{sub}.gz", prefix="tgbot_")
                    os.close(fd)
                    with open(sub_path, "wb") as sf:
                        sf.write(sub_data)
                    ok = send_file(
                        chat_id, sub_path,
                        caption=f"{caption or ''} | 部分 {part_num}-{sub} | {_format_size(len(sub_data))}".strip(" |"),
                        telegram_filename=Path(file_path).name + f".part{part_num:02d}_{sub}.gz",
                    )
                    if not ok or not ok.get("ok"):
                        all_ok = False
                    os.unlink(sub_path)

            os.unlink(gz_path)

    total = part_num
    if all_ok:
        send_message(chat_id, f"✅ 全部 {total} 部分发送完成")
    else:
        send_message(chat_id, f"⚠️ 部分发送失败，共 {total} 部分")
    return {"ok": all_ok}


# ============================================================
# Command execution
# ============================================================

def _kill_process_group(proc, sig):
    """Send signal to the entire process group."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, OSError):
        pass


def run_command(command, timeout=None):
    """
    Execute a shell command with proper timeout and process-group cleanup.
    On timeout: send SIGINT (Ctrl+C), wait 3s for graceful exit,
    collect partial output, then SIGKILL if still alive.
    Returns (output_string, timed_out, force_killed).
    """
    if timeout is None:
        timeout = DEFAULT_COMMAND_TIMEOUT
    timeout = min(timeout, MAX_COMMAND_TIMEOUT)

    print(f"[RUN] {command} (timeout={timeout}s)")

    try:
        # start_new_session=True is the Python-recommended way to
        # create a new process group (replaces preexec_fn=os.setsid)
        proc = subprocess.Popen(
            command,
            shell=True,
            executable="/bin/bash",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        timed_out = False
        force_killed = False

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            # Step 1: SIGINT (what Ctrl+C does)
            _kill_process_group(proc, signal.SIGINT)

            # Step 2: wait up to 3s for graceful exit + collect partial output
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                # Step 3: SIGKILL — force kill
                _kill_process_group(proc, signal.SIGKILL)
                force_killed = True
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except Exception:
                    stdout, stderr = "", ""

            returncode = proc.returncode
            timed_out = True

        # Build output
        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += "\n[stderr]\n" + stderr
        if not output:
            output = "(no output)"

        if timed_out:
            if force_killed:
                kill_info = "⏱ 超时 (timeout={}s)\n→ SIGINT 无法结束，已发送 SIGKILL\n"
            else:
                kill_info = "⏱ 超时 (timeout={}s)\n→ 已发送 SIGINT\n"
            header = (
                f"$ {command}\n"
                f"{kill_info.format(timeout)}"
                f"exit code: {returncode}\n\n"
            )
        else:
            header = (
                f"$ {command}\n"
                f"exit code: {returncode}\n\n"
            )

        return header + output

    except Exception as e:
        return f"$ {command}\n\nERROR: {e}"


# ============================================================
# Task tracking
# ============================================================

_bg_tasks = {}
_bg_lock = threading.Lock()
_task_counter = 0


def _next_task_id():
    global _task_counter
    with _bg_lock:
        _task_counter += 1
        return _task_counter


def start_task(chat_id, command, timeout=None):
    """
    Run a command in a background thread so the bot stays responsive.
    Adds the thread to _bg_tasks so /ps can track it.
    """
    task_id = _next_task_id()

    def worker():
        try:
            output = run_command(command, timeout=timeout)
            send_message(chat_id, output)
        except Exception as e:
            send_message(chat_id, f"$ {command}\n\nERROR: {e}")
        finally:
            with _bg_lock:
                # Keep finished tasks for a while, then they'll be
                # cleaned up naturally as daemon threads
                pass

    t = threading.Thread(target=worker, daemon=True)
    with _bg_lock:
        _bg_tasks[task_id] = t
    t.start()
    return task_id


# ============================================================
# Telegram message handling
# ============================================================

def parse_run_command(text):
    """
    Parse /run command, supporting optional timeout as first arg.
    Examples:
      /run ping google.com         -> timeout=30, command="ping google.com"
      /run 10 ping google.com      -> timeout=10, command="ping google.com"
      /run 5 top                   -> timeout=5, command="top"
    """
    body = text[len("/run "):].strip()

    # Try to parse leading number as timeout
    parts = body.split(None, 1)
    if parts and parts[0].isdigit():
        timeout = int(parts[0])
        command = parts[1].strip() if len(parts) > 1 else ""
    else:
        timeout = None
        command = body

    return command, timeout


def handle_message(message):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        return

    print(f"[MESSAGE] chat={chat_id} text={text}")

    # /start
    if text == "/start":
        send_message(chat_id, """🤖 Telegram Server Bot (v2)

可用命令：

/run <命令>
执行 Shell 命令（默认超时 30s）。
命令在后台线程执行，Bot 不会阻塞。

/run <秒数> <命令>
指定超时时间执行命令。
例如: /run 60 ping google.com

/getfile <文件路径>
发送服务器文件。
（超过 48MB 自动 gzip 压缩，压缩后仍超则分块发送）

/ps
查看当前任务。

示例：
  /run df -h
  /run 10 ping google.com
  /run systemctl status xray
  /getfile /var/log/v2ray/access.log
""")
        return

    # /run — always threaded, non-blocking
    if text.startswith("/run "):
        command, timeout = parse_run_command(text)
        if not command:
            send_message(chat_id, "用法:\n/run <命令>\n/run <秒数> <命令>")
            return
        effective_timeout = timeout or DEFAULT_COMMAND_TIMEOUT
        send_message(
            chat_id,
            f"⏳ 开始执行:\n\n$ {command}\n(超时: {effective_timeout}s)"
        )
        start_task(chat_id, command, timeout=timeout)
        return

    # /getfile — smart send with auto compress/split
    if text.startswith("/getfile "):
        file_path = text[len("/getfile "):].strip()
        if not file_path:
            send_message(chat_id, "用法:\n/getfile <文件路径>")
            return
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        send_message(chat_id, f"正在发送文件:\n{file_path} ({_format_size(file_size)})")
        result = send_file_smart(chat_id, file_path)
        if not result or not result.get("ok"):
            error = result.get("error") if isinstance(result, dict) else "未知错误"
            send_message(chat_id, f"发送失败:\n{error}")
        return

    # /ps
    if text == "/ps":
        with _bg_lock:
            active = [
                tid for tid, t in _bg_tasks.items() if t.is_alive()
            ]
            total = len(_bg_tasks)
        if active:
            send_message(
                chat_id,
                f"任务: {len(active)} 个运行中 / {total} 个总计"
            )
        else:
            send_message(chat_id, "没有正在运行的任务")
        return

    # Unknown command
    if text.startswith("/"):
        send_message(chat_id, "未知命令。\n发送 /start 查看帮助。")


# ============================================================
# Long polling
# ============================================================

def main():
    print("Telegram bot starting... (v2)")
    offset = None

    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset

            response = requests.get(f"{API}/getUpdates", params=params, timeout=40)
            data = response.json()

            if not data.get("ok"):
                print("getUpdates error:", data)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                try:
                    handle_message(message)
                except Exception as e:
                    print("Message handling error:", repr(e))
                    chat_id = message.get("chat", {}).get("id")
                    if chat_id:
                        send_message(chat_id, f"Bot error:\n{e}")

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            print("Polling error:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
