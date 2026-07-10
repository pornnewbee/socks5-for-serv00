from datetime import datetime
from starlette.responses import Response
from mcp.server.fastmcp import FastMCP
import uvicorn

# 创建 FastMCP 实例，工具只需定义一次
mcp = FastMCP("multi-transport-mcp")

@mcp.tool()
def get_current_time() -> str:
    """获取当前的日期和时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@mcp.tool()
def hello(name: str) -> str:
    """向指定的人打招呼"""
    return f"你好，{name}！👋"

# 分别获取两种传输的 ASGI 应用
sse_app = mcp.sse_app()               # 内部路由: GET /sse 和 POST /messages/
stream_app = mcp.streamable_http_app() # 内部路由: POST /mcp (及可能的 GET)

# 自定义 ASGI 分发器：根据路径前缀交给不同的子应用
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
            response = Response("Not Found", status_code=404)
            await response(scope, receive, send)
    else:
        # 非 HTTP 协议（如 WebSocket）暂不支持
        pass

if __name__ == "__main__":
    # 监听所有接口，端口改为你的 6942
    uvicorn.run(app, host="0.0.0.0", port=6942)
