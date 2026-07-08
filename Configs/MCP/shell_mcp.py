"""
MCP Shell Server - with PTY session support & structured output.
Optimized for both one-shot commands and interactive sessions.
"""

from mcp.server.fastmcp import FastMCP
import asyncio
import pty
import os
import fcntl
import time
import logging
import signal
import json
from typing import Optional

# --- Configuration ---
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 6942
DEFAULT_TIMEOUT = 30
OUTPUT_MAX_BYTES = 1024 * 1024  # 1 MB output cap
SESSION_IDLE_TIMEOUT = 1800     # 30 min idle -> auto cleanup
LOG_FILE = "/tmp/mcp_shell.log"

# --- Logging ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mcp_shell")

mcp = FastMCP("shell", host=LISTEN_HOST, port=LISTEN_PORT)


# ====================================================================
# Tool 1: run_command — one-shot command with structured result
# ====================================================================

class CommandResult:
    """Structured result for one-shot commands."""

    def __init__(self, stdout: str, stderr: str, exit_code: int, timed_out: bool):
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
async def run_command(
    command: str,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
) -> str:
    """
    Execute a shell command and return structured result.

    Args:
        command: The shell command to execute
        timeout: Max execution time in seconds (default 30, 0 = no limit)

    Returns:
        JSON string with stdout, stderr, exit_code, timed_out fields.

    Commands running longer than the timeout will be killed.
    Interactive commands (e.g. bash -i, python -i, ssh) should use
    start_session / send_input / read_output instead.
    """
    log.info(f"[run_command] executing: {command[:200]}")

    actual_timeout = timeout if (timeout is not None and timeout > 0) else None

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL),
        )

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=actual_timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            # Kill the process group to clean up children
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
            exit_code=proc.returncode or -1,
            timed_out=timed_out,
        )

        log.info(f"[run_command] exit={result.exit_code}, timed_out={result.timed_out}, "
                 f"stdout={len(result.stdout)}B, stderr={len(result.stderr)}B")

        return json.dumps(result.to_dict())

    except Exception as e:
        log.error(f"[run_command] error: {e}")
        return json.dumps({
            "stdout": "",
            "stderr": f"Error: {e}",
            "exit_code": -1,
            "timed_out": False,
        })


# ====================================================================
# PTY Session Management — for interactive commands
# ====================================================================

class PTYSession:
    """A pseudo-terminal session for interactive commands."""

    def __init__(self, session_id: str, command: str):
        self.session_id = session_id
        self.command = command
        self.master_fd: Optional[int] = None
        self.child_pid: Optional[int] = None
        self.output_buffer = ""
        self.last_active = time.time()
        self.closed = False
        self._reader_task: Optional[asyncio.Task] = None

    def is_alive(self) -> bool:
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


# Session registry
_sessions: dict[str, PTYSession] = {}
_session_lock = asyncio.Lock()


def _set_nonblocking(fd: int):
    """Set a file descriptor to non-blocking mode."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


async def _reader_loop(session: PTYSession):
    """Background reader: drains PTY output into session buffer."""
    loop = asyncio.get_event_loop()
    fd = session.master_fd

    def _read():
        try:
            data = os.read(fd, 65536)
            if data:
                decoded = data.decode(errors="replace")
                session.output_buffer += decoded
                if len(session.output_buffer) > OUTPUT_MAX_BYTES * 2:
                    session.output_buffer = session.output_buffer[-OUTPUT_MAX_BYTES:]
                session.last_active = time.time()
                return True
            else:
                # EOF
                session.closed = True
                return False
        except (BlockingIOError, OSError):
            return True  # retry later
        except Exception:
            session.closed = True
            return False

    try:
        while not session.closed and session.master_fd is not None:
            await asyncio.sleep(0.01)
            try:
                loop.add_reader(fd, lambda: None)  # cheap wakeup
            except Exception:
                pass
            # Do a blocking-ish read with timeout via the event loop
            await asyncio.sleep(0.02)
            _read()
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
    """Remove sessions that have been idle too long."""
    now = time.time()
    stale_ids = []
    for sid, s in _sessions.items():
        if not s.is_alive() or (now - s.last_active > SESSION_IDLE_TIMEOUT):
            stale_ids.append(sid)
    for sid in stale_ids:
        try:
            s = _sessions.pop(sid, None)
            if s:
                log.info(f"[cleanup] closing stale session {sid}")
                s.close()
        except Exception:
            pass


@mcp.tool()
async def start_session(
    command: str = "/bin/bash",
    session_id: Optional[str] = None,
) -> str:
    """
    Start a new PTY session for interactive command execution.

    Args:
        command: The command to start (default: /bin/bash)
        session_id: Optional custom session ID (auto-generated if omitted)

    Returns:
        JSON string with session_id and status.

    After starting a session, use send_input() to send commands
    and read_output() to read the responses.
    """
    import uuid

    _cleanup_stale_sessions()

    sid = session_id or uuid.uuid4().hex[:8]

    async with _session_lock:
        if sid in _sessions and _sessions[sid].is_alive():
            return json.dumps({
                "error": f"Session '{sid}' already exists",
            })

        session = PTYSession(session_id=sid, command=command)

        try:
            # Fork PTY
            master_fd, slave_fd = pty.openpty()
            _set_nonblocking(master_fd)

            child_pid = os.fork()
            if child_pid == 0:
                # Child process
                os.setsid()
                os.close(master_fd)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                # Set terminal size
                import struct
                packed = struct.pack("HHHH", 80, 24, 0, 0)
                fcntl.ioctl(0, 0x5410, packed)  # TIOCSWINSZ
                os.execvp(command.split()[0], command.split())

            os.close(slave_fd)
            session.master_fd = master_fd
            session.child_pid = child_pid
            session._reader_task = asyncio.create_task(_reader_loop(session))
            _sessions[sid] = session

            log.info(f"[start_session] id={sid}, command={command}, pid={child_pid}")

            return json.dumps({
                "session_id": sid,
                "pid": child_pid,
                "command": command,
                "status": "started",
            })

        except Exception as e:
            session.close()
            log.error(f"[start_session] error: {e}")
            return json.dumps({"error": str(e)})


@mcp.tool()
async def send_input(
    session_id: str,
    text: str,
) -> str:
    """
    Send input to an active PTY session.

    Args:
        session_id: The session ID from start_session
        text: Input text to send (newline appended if missing)

    Returns:
        JSON string with status.
    """
    async with _session_lock:
        session = _sessions.get(session_id)
        if not session or not session.is_alive():
            return json.dumps({"error": f"Session '{session_id}' not found or not alive"})

        try:
            # Ensure text ends with newline for typical command execution
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
async def read_output(
    session_id: str,
    timeout: float = 2.0,
) -> str:
    """
    Read accumulated output from a PTY session.

    Args:
        session_id: The session ID from start_session
        timeout: Max time in seconds to wait for new output (default: 2)

    Returns:
        JSON string with output content, has_more flag, and is_alive status.

    If the command is slow to respond, this will block up to `timeout`
    seconds waiting for at least some output.
    """
    session = _sessions.get(session_id)
    if not session:
        return json.dumps({"error": f"Session '{session_id}' not found"})

    if session.closed and not session.output_buffer:
        return json.dumps({
            "output": "",
            "has_more": False,
            "is_alive": False,
        })

    # Wait briefly for any new output
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

        # Truncate if too long
        if len(output) > OUTPUT_MAX_BYTES:
            output = output[-OUTPUT_MAX_BYTES:]

        return json.dumps({
            "output": output,
            "has_more": is_alive,
            "is_alive": is_alive,
        })


@mcp.tool()
async def list_sessions() -> str:
    """
    List all active PTY sessions.

    Returns:
        JSON string with list of active sessions.
    """
    _cleanup_stale_sessions()
    active = []
    for sid, s in list(_sessions.items()):
        alive = s.is_alive()
        if alive:
            idle_secs = int(time.time() - s.last_active)
            active.append({
                "session_id": sid,
                "command": s.command,
                "idle_seconds": idle_secs,
            })
    return json.dumps({"sessions": active, "count": len(active)})


@mcp.tool()
async def close_session(session_id: str) -> str:
    """
    Close and clean up a PTY session.

    Args:
        session_id: The session ID to close

    Returns:
        JSON string with status.
    """
    async with _session_lock:
        session = _sessions.pop(session_id, None)
        if not session:
            return json.dumps({"error": f"Session '{session_id}' not found"})

        session.close()
        log.info(f"[close_session] closed session {session_id}")
        return json.dumps({"status": "closed", "session_id": session_id})


# ====================================================================
# Server Entry Point
# ====================================================================

if __name__ == "__main__":
    log.info(f"Starting MCP Shell server on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"MCP Shell server starting on {LISTEN_HOST}:{LISTEN_PORT}")
    mcp.run(transport="streamable-http")
