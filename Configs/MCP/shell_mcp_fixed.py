from datetime import datetime
from starlette.applications import Starlette
from starlette.routing import Mount
from mcp.server.fastmcp import FastMCP

# 创建 FastMCP 实例（工具只需定义一次）
mcp = FastMCP("multi-transport-mcp")

@mcp.tool()
def get_current_time() -> str:
    """获取当前的日期和时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@mcp.tool()
def hello(name: str) -> str:
    """向指定的人打招呼"""
    return f"你好，{name}！👋"

# 构建 ASGI 应用，同时挂载两种传输
app = Starlette(
    routes=[
        Mount("/sse", app=mcp.sse_app()),                # Cline 连接用
        Mount("/mcp", app=mcp.streamable_http_app()),    # 其他 Agent（如 Cursor）连接用
    ]
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6942)
