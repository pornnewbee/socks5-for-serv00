from mcp.server.fastmcp import FastMCP
import asyncio

mcp = FastMCP(
    "shell",
    host="0.0.0.0",
    port=6942
)


@mcp.tool()
async def run_command(command: str) -> str:
    """
    Execute shell command.
    Commands running longer than 30 seconds will be terminated.
    """

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=30
            )

        except asyncio.TimeoutError:
            proc.kill()

            return (
                f"Command timeout after 30s\n"
                f"PID: {proc.pid}"
            )

        return (
            stdout.decode(errors="ignore")
            +
            stderr.decode(errors="ignore")
        )

    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http"
    )
