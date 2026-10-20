#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HTTP Remote Linux Manager
Python 3.10+, standard library only.

Server:
    python3 remote_http.py server --host 127.0.0.1 --port 8765

Agent:
    python3 remote_http.py agent \
      --url http://your-control.example.com:8765 \
      --name linux-01

Control API:
  POST /api/exec
  GET  /api/tasks/<task_id>
  GET  /api/health

Agent API:
  GET  /api/agent/poll
  POST /api/agent/result
"""

import argparse
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen


MAX_BODY = 1024 * 1024
MAX_OUTPUT = 256 * 1024
TASK_TTL = 3600
tasks = {}
task_lock = threading.Lock()
task_event = threading.Condition(task_lock)


def now():
    return time.time()


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def clean_tasks():
    cutoff = now() - TASK_TTL
    for tid in list(tasks):
        if tasks[tid].get("created_at", now()) < cutoff:
            del tasks[tid]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "RemoteHTTP/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.client_address[0], fmt % args))

    def send_json(self, status, obj):
        data = json_bytes(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length")

        if length <= 0 or length > MAX_BODY:
            raise ValueError("Body must be 1..1048576 bytes")

        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid JSON")

        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/health":
            return self.send_json(200, {"ok": True})

        if path == "/api/agent/poll":
            # Long-poll for up to 25 seconds.
            deadline = now() + 25
            with task_event:
                while True:
                    clean_tasks()
                    task = next(
                        (t for t in tasks.values()
                         if t["status"] == "queued"),
                        None,
                    )
                    if task:
                        task["status"] = "running"
                        task["agent"] = self.headers.get(
                            "X-Agent-Name", "unknown"
                        )[:128]
                        task["started_at"] = now()
                        return self.send_json(200, {
                            "task_id": task["task_id"],
                            "command": task["command"],
                            "timeout": task["timeout"],
                        })

                    remaining = deadline - now()
                    if remaining <= 0:
                        return self.send_json(200, {"task": None})
                    task_event.wait(timeout=remaining)

        if path.startswith("/api/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            with task_lock:
                task = tasks.get(tid)
                if task is None:
                    return self.send_json(404, {"error": "task not found"})
                return self.send_json(200, task.copy())

        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/exec":
            try:
                data = self.read_json()
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})

            command = data.get("command")
            if not isinstance(command, str) or not command.strip():
                return self.send_json(400, {
                    "error": "command must be a non-empty string"
                })

            try:
                timeout = int(data.get("timeout", 60))
            except (ValueError, TypeError):
                return self.send_json(400, {"error": "invalid timeout"})

            if not 1 <= timeout <= 3600:
                return self.send_json(400, {
                    "error": "timeout must be 1..3600 seconds"
                })

            task = {
                "task_id": uuid.uuid4().hex,
                "command": command,
                "timeout": timeout,
                "status": "queued",
                "created_at": now(),
            }
            with task_event:
                clean_tasks()
                tasks[task["task_id"]] = task
                task_event.notify_all()

            return self.send_json(202, {
                "task_id": task["task_id"],
                "status": "queued",
            })

        if path == "/api/agent/result":
            try:
                data = self.read_json()
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})

            tid = data.get("task_id")
            if not isinstance(tid, str):
                return self.send_json(400, {"error": "invalid task_id"})

            with task_lock:
                task = tasks.get(tid)
                if task is None:
                    return self.send_json(404, {"error": "task not found"})
                if task["status"] != "running":
                    return self.send_json(409, {
                        "error": "task is not running"
                    })

                task.update({
                    "status": "completed",
                    "returncode": data.get("returncode"),
                    "stdout": str(data.get("stdout", ""))[:MAX_OUTPUT],
                    "stderr": str(data.get("stderr", ""))[:MAX_OUTPUT],
                    "elapsed": data.get("elapsed"),
                    "finished_at": now(),
                })

            return self.send_json(200, {"ok": True})

        return self.send_json(404, {"error": "not found"})


def run_server(args):
    httpd = Server((args.host, args.port), Handler)
    print(f"Listening on {args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def http_json(url, method="GET", payload=None, headers=None, timeout=40):
    h = {}
    if headers:
        h.update(headers)
    body = None
    if payload is not None:
        body = json_bytes(payload)
        h["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=h, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise RuntimeError("Response too large")
            return json.loads(raw)
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Connection error: {exc}") from exc


def execute_command(command, timeout):
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", command],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        def decode(value):
            if value is None:
                return ""
            if isinstance(value, bytes):
                return value.decode("utf-8", "replace")
            return value

        stdout = decode(exc.stdout)
        stderr = decode(exc.stderr) + "\nCommand timed out."
        code = 124

    elapsed = round(time.monotonic() - started, 3)
    return {
        "returncode": code,
        "stdout": stdout[-MAX_OUTPUT:],
        "stderr": stderr[-MAX_OUTPUT:],
        "elapsed": elapsed,
    }


def run_agent(args):
    base = args.url.rstrip("/")
    print(f"Agent {args.name} connecting to {base}")
    while True:
        try:
            task = http_json(
                base + "/api/agent/poll",
                headers={"X-Agent-Name": args.name},
                timeout=35,
            )
            if not task.get("task_id"):
                continue

            tid = task["task_id"]
            print(f"Running task {tid}")
            result = execute_command(
                task["command"], int(task.get("timeout", 60))
            )
            payload = {"task_id": tid, **result}

            http_json(
                base + "/api/agent/result",
                method="POST",
                payload=payload,
                timeout=40,
            )
            print(f"Task {tid} finished: exit={result['returncode']}")

        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"Agent error: {exc}")
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="HTTP remote Linux manager")
    sub = parser.add_subparsers(dest="mode", required=True)

    srv = sub.add_parser("server")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8765)

    ag = sub.add_parser("agent")
    ag.add_argument("--url", required=True)
    ag.add_argument("--name", default="linux-agent")

    args = parser.parse_args()
    if args.mode == "server":
        run_server(args)
    else:
        run_agent(args)


if __name__ == "__main__":
    main()
