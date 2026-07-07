from mcp.server.fastmcp import FastMCP
import subprocess

mcp = FastMCP(
    "shell",
    host="0.0.0.0",
    port=6942
)


@mcp.tool()
def run_command(command: str) -> str:
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True
    )

    return result.stdout + result.stderr


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http"
    )
