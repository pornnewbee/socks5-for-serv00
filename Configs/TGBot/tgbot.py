#!/usr/bin/env python3

import os
import time
import subprocess
import tempfile
from pathlib import Path

import requests


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("请设置环境变量 BOT_TOKEN")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# 命令最大执行时间
COMMAND_TIMEOUT = 300

# Telegram 普通消息最大长度大约 4096
MAX_MESSAGE_LENGTH = 4000

# 文件上传超时
UPLOAD_TIMEOUT = 600


# ============================================================
# Telegram API
# ============================================================

def telegram(method, **kwargs):
    url = f"{API}/{method}"

    try:
        r = requests.post(
            url,
            json=kwargs,
            timeout=60
        )

        return r.json()

    except Exception as e:
        print(f"Telegram API error: {e}")
        return None


def send_message(chat_id, text):
    if len(text) > MAX_MESSAGE_LENGTH:
        return send_text_file(
            chat_id,
            text,
            filename="command-output.txt"
        )

    return telegram(
        "sendMessage",
        chat_id=chat_id,
        text=text
    )


def send_text_file(chat_id, text, filename="output.txt"):
    """
    把长文本保存成临时文件，然后作为 Telegram 文件发送。
    """

    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            delete=False
        ) as f:
            f.write(text)
            tmp_path = f.name

        return send_file(
            chat_id,
            tmp_path,
            caption=filename,
            telegram_filename=filename
        )

    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def send_file(
    chat_id,
    file_path,
    caption=None,
    telegram_filename=None
):
    """
    将服务器文件发送给 Telegram。
    """

    path = Path(file_path)

    if not path.exists():
        return {
            "ok": False,
            "error": f"文件不存在: {file_path}"
        }

    if not path.is_file():
        return {
            "ok": False,
            "error": f"不是普通文件: {file_path}"
        }

    if telegram_filename is None:
        telegram_filename = path.name

    url = f"{API}/sendDocument"

    try:
        with path.open("rb") as f:

            data = {
                "chat_id": str(chat_id)
            }

            if caption:
                data["caption"] = caption

            response = requests.post(
                url,
                data=data,
                files={
                    "document": (
                        telegram_filename,
                        f
                    )
                },
                timeout=UPLOAD_TIMEOUT
            )

        return response.json()

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


# ============================================================
# Command execution
# ============================================================

def run_command(command):

    print(f"[RUN] {command}")

    try:

        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT
        )

        output = ""

        if result.stdout:
            output += result.stdout

        if result.stderr:
            output += "\n[stderr]\n"
            output += result.stderr

        if not output:
            output = "(no output)"

        output = (
            f"$ {command}\n"
            f"exit code: {result.returncode}\n\n"
            f"{output}"
        )

        return output

    except subprocess.TimeoutExpired:

        return (
            f"$ {command}\n\n"
            f"ERROR: command timeout "
            f"({COMMAND_TIMEOUT}s)"
        )

    except Exception as e:

        return (
            f"$ {command}\n\n"
            f"ERROR: {e}"
        )


# ============================================================
# Telegram message handling
# ============================================================

def handle_message(message):

    chat = message.get("chat", {})
    chat_id = chat.get("id")

    text = message.get("text", "")

    if not chat_id:
        return

    if not text:
        return

    print(
        f"[MESSAGE] "
        f"chat={chat_id} "
        f"text={text}"
    )

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if text == "/start":

        send_message(
            chat_id,
            """Telegram Server Bot

可用命令：

/run <命令>
执行 Linux Shell 命令。

/getfile <文件路径>
把服务器上的文件发送给你。

例如：

/run df -h

/run systemctl status xray

/getfile /tmp/xray-cf.pcap

/getfile /var/log/v2ray/access.log
"""
        )

        return

    # --------------------------------------------------------
    # /run
    # --------------------------------------------------------

    if text.startswith("/run "):

        command = text[5:].strip()

        if not command:
            send_message(
                chat_id,
                "用法:\n/run <命令>"
            )
            return

        # 先告诉用户开始执行
        send_message(
            chat_id,
            f"执行命令:\n\n$ {command}"
        )

        output = run_command(command)

        send_message(
            chat_id,
            output
        )

        return

    # --------------------------------------------------------
    # /getfile
    # --------------------------------------------------------

    if text.startswith("/getfile "):

        file_path = text[len("/getfile "):].strip()

        if not file_path:
            send_message(
                chat_id,
                "用法:\n/getfile <文件路径>"
            )
            return

        send_message(
            chat_id,
            f"正在发送文件:\n{file_path}"
        )

        result = send_file(
            chat_id,
            file_path
        )

        if not result or not result.get("ok"):

            error = (
                result.get("error")
                if isinstance(result, dict)
                else "未知错误"
            )

            send_message(
                chat_id,
                f"发送失败:\n{error}"
            )

        return

    # --------------------------------------------------------
    # Unknown command
    # --------------------------------------------------------

    if text.startswith("/"):
        send_message(
            chat_id,
            "未知命令。\n发送 /start 查看帮助。"
        )


# ============================================================
# Long polling
# ============================================================

def main():

    print("Telegram bot starting...")

    offset = None

    while True:

        try:

            params = {
                "timeout": 30
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{API}/getUpdates",
                params=params,
                timeout=40
            )

            data = response.json()

            if not data.get("ok"):
                print(
                    "getUpdates error:",
                    data
                )

                time.sleep(5)
                continue

            updates = data.get("result", [])

            for update in updates:

                # 更新 offset，避免重复处理
                offset = update["update_id"] + 1

                message = update.get("message")

                if not message:
                    continue

                try:
                    handle_message(message)

                except Exception as e:

                    print(
                        "Message handling error:",
                        repr(e)
                    )

                    chat_id = message.get(
                        "chat",
                        {}
                    ).get("id")

                    if chat_id:
                        send_message(
                            chat_id,
                            f"Bot error:\n{e}"
                        )

        except KeyboardInterrupt:

            print("\nBot stopped.")
            break

        except Exception as e:

            print(
                "Polling error:",
                repr(e)
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
