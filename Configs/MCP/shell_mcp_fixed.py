"""
MCP Shell Server (FIXED) - with PTY session support & structured output.
Now supports both SSE (Cline) and Streamable HTTP (Cursor etc.) on the same port.
"""

from mcp.server.fastmcp import FastMCP
import asyncio, pty, os, fcntl, time, logging, signal, json, uuid
from typing import Optional
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 6942
DEFAULT_TIMEOUT = 30
OUTPUT_MAX_BYTES = 1024 * 1024
SESSION_IDLE_TIMEOUT = 1800
LOG_FILE = "/tmp/mcp_shell_fixed.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mcp_shell_fixed")

mcp = FastMCP("shell")

class CommandResult:
    def __init__(self, stdout, stderr, exit_code, timed_out):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    def to_dict(self):
        return {
            "stdout": self.stdout[-OUTPUT_MAX_BYTES:],
            "stderr": self.stderr[-OUTPUT_MAX_BYTES:],
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }

@mcp.tool()
async def run_command(command, timeout=DEFAULT_TIMEOUT):
    log.info(f"[run_command] executing: {command[:200]}")
    try:
        t = int(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (ValueError, TypeError):
        t = DEFAULT_TIMEOUT
    actual_timeout = t if t > 0 else DEFAULT_TIMEOUT
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: (os.setpgrp(), signal.signal(signal.SIGTERM, signal.SIG_DFL)),
        )
        timed_out = False
        stdout = b""
        stderr = b""
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=actual_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                proc.kill()
            await proc.wait()
        stdout_decoded = stdout.decode(errors="replace") if stdout else ""
        stderr_decoded = stderr.decode(errors="replace") if stderr else ""
        result = CommandResult(
            stdout=stdout_decoded[-OUTPUT_MAX_BYTES:],
            stderr=stderr_decoded[-OUTPUT_MAX_BYTES:],
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
        )
        log.info(f"[run_command] exit={result.exit_code}, timed_out={result.timed_out}")
        return json.dumps(result.to_dict())
    except Exception as e:
        log.error(f"[run_command] error: {e}")
        return json.dumps({"stdout": "", "stderr": f"Error: {e}", "exit_code": -1, "timed_out": False})

class PTYSession:
    def __init__(self, session_id, command):
        self.session_id = session_id
        self.command = command
        self.master_fd = None
        self.child_pid = None
        self.output_buffer = ""
        self.last_active = time.time()
        self.closed = False
        self._reader_task = None

    def is_alive(self):
        if self.closed or self.child_pid is None:
            return False
        try:
            pid, _ = os.waitpid(self.child_pid, os.WNOHANG)
            if pid == self.child_pid:
                self.closed = True
                return False
            return True
        except ChildProcessError:
            self.closed = True
            return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self.child_pid:
            try:
                pgid = os.getpgid(self.child_pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(self.child_pid, signal.SIGKILL)
                except Exception:
                    pass
            try:
                os.waitpid(self.child_pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
        self.master_fd = None
        self.child_pid = None

sessions = {}
_session_lock = asyncio.Lock()

def _set_nonblocking(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

async def _reader_loop(session):
    loop = asyncio.get_event_loop()
    fd = session.master_fd

    def _on_readable():
        try:
            data = os.read(fd, 65536)
        except (BlockingIOError, OSError):
            return
        if not data:
            session.closed = True
            return
        async def _consume():
            async with _session_lock:
                decoded = data.decode(errors="replace")
                session.output_buffer += decoded
                if len(session.output_buffer) > OUTPUT_MAX_BYTES * 2:
                    session.output_buffer = session.output_buffer[-OUTPUT_MAX_BYTES:]
                session.last_active = time.time()
        asyncio.ensure_future(_consume())

    try:
        loop.add_reader(fd, _on_readable)
        while not session.closed and session.master_fd is not None:
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error(f"[reader_loop] {session.session_id}: {e}")
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass

def _cleanup_stale_sessions():
    now = time.time()
    stale_ids = []
    for sid, s in sessions.items():
        if not s.is_alive() or (now - s.last_active > SESSION_IDLE_TIMEOUT):
            stale_ids.append(sid)
    for sid in stale_ids:
        try:
            s = sessions.pop(sid, None)
            if s:
                log.info(f"[cleanup] closing stale session {sid}")
                s.close()
        except Exception:
            pass

@mcp.tool()
async def start_session(command="/bin/bash", session_id=None):
    _cleanup_stale_sessions()
    sid = session_id or uuid.uuid4().hex[:8]
    async with _session_lock:
        if sid in sessions and sessions[sid].is_alive():
            return json.dumps({"error": f"Session '{sid}' already exists"})
        session = PTYSession(session_id=sid, command=command)
        try:
            master_fd, slave_fd = pty.openpty()
            _set_nonblocking(master_fd)
            child_pid = os.fork()
            if child_pid == 0:
                os.setsid()
                os.close(master_fd)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                import struct
                packed = struct.pack("HHHH", 80, 24, 0, 0)
                fcntl.ioctl(0, 0x5410, packed)
                os.execvp(command.split()[0], command.split())
            os.close(slave_fd)
            session.master_fd = master_fd
            session.child_pid = child_pid
            session._reader_task = asyncio.create_task(_reader_loop(session))
            sessions[sid] = session
            log.info(f"[start_session] id={sid}, command={command}, pid={child_pid}")
            return json.dumps({"session_id": sid, "pid": child_pid, "command": command, "status": "started"})
        except Exception as e:
            session.close()
            log.error(f"[start_session] error: {e}")
            return json.dumps({"error": str(e)})

@mcp.tool()
async def send_input(session_id, text):
    async with _session_lock:
        session = sessions.get(session_id)
        if not session or not session.is_alive():
            return json.dumps({"error": f"Session '{session_id}' not found or not alive"})
        try:
            if not text.endswith("\n"):
                text += "\n"
            data = text.encode()
            os.write(session.master_fd, data)
            session.last_active = time.time()
            log.info(f"[send_input] session={session_id}, bytes={len(data)}")
            return json.dumps({"status": "sent", "bytes": len(data)})
        except Exception as e:
            log.error(f"[send_input] error: {e}")
            return json.dumps({"error": str(e)})

@mcp.tool()
async def read_output(session_id, timeout=2.0):
    session = sessions.get(session_id)
    if not session:
        return json.dumps({"error": f"Session '{session_id}' not found"})
    if session.closed and not session.output_buffer:
        return json.dumps({"output": "", "has_more": False, "is_alive": False})
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session.output_buffer:
            break
        if session.closed:
            break
        await asyncio.sleep(0.05)
    async with _session_lock:
        output = session.output_buffer
        session.output_buffer = ""
        is_alive = session.is_alive()
        if len(output) > OUTPUT_MAX_BYTES:
            output = output[-OUTPUT_MAX_BYTES:]
        return json.dumps({"output": output, "has_more": is_alive, "is_alive": is_alive})

@mcp.tool()
async def list_sessions():
    _cleanup_stale_sessions()
    active = []
    for sid, s in list(sessions.items()):
        alive = s.is_alive()
        if alive:
            idle_secs = int(time.time() - s.last_active)
            active.append({"session_id": sid, "command": s.command, "idle_seconds": idle_secs})
    return json.dumps({"sessions": active, "count": len(active)})

@mcp.tool()
async def close_session(session_id):
    async with _session_lock:
        session = sessions.pop(session_id, None)
        if not session:
            return json.dumps({"error": f"Session '{session_id}' not found"})
        session.close()
        log.info(f"[close_session] closed session {session_id}")
        return json.dumps({"status": "closed", "session_id": session_id})

@mcp.tool()
async def start_background(command):
    """Start a long-running/interactive command in background (non-blocking).
    Returns session_id immediately; use send_input/read_output/close_session
    to interact. Ideal for ssh -R, reverse shells, python -i, etc."""
    _cleanup_stale_sessions()
    sid = uuid.uuid4().hex[:8]
    async with _session_lock:
        session = PTYSession(session_id=sid, command=command)
        try:
            master_fd, slave_fd = pty.openpty()
            _set_nonblocking(master_fd)
            child_pid = os.fork()
            if child_pid == 0:
                os.setsid()
                os.close(master_fd)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                import struct
                packed = struct.pack("HHHH", 80, 24, 0, 0)
                fcntl.ioctl(0, 0x5410, packed)
                os.execvp(command.split()[0], command.split())
            os.close(slave_fd)
            session.master_fd = master_fd
            session.child_pid = child_pid
            session._reader_task = asyncio.create_task(_reader_loop(session))
            sessions[sid] = session
            log.info(f"[start_background] id={sid}, command={command}, pid={child_pid}")
            return json.dumps({"session_id": sid, "pid": child_pid, "command": command, "status": "started"})
        except Exception as e:
            session.close()
            log.error(f"[start_background] error: {e}")
            return json.dumps({"error": str(e)})

# ====================== 启动双传输服务器 ======================
if __name__ == "__main__":
    log.info(f"Starting MCP Shell server (dual transport) on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"MCP Shell server (dual transport) starting on {LISTEN_HOST}:{LISTEN_PORT}")

    sse_app = mcp.sse_app()
    stream_app = mcp.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "/")
            # SSE 及其配套消息端点
            if path.startswith("/sse") or path.startswith("/messages"):
                await sse_app(scope, receive, send)
            # Streamable HTTP 端点
            elif path.startswith("/mcp"):
                await stream_app(scope, receive, send)
            else:
                # 对于未知路径返回 404
                from starlette.responses import Response
                response = Response("Not Found", status_code=404)
                await response(scope, receive, send)

    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
