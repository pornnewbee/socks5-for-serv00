from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
import subprocess
import shlex
import traceback

app = FastAPI()

# ===== 修改为你的 cloudflared 路径 =====
CF_BIN = "/usr/local/bin/cloudflared"


# ===== HTML 页面（已修复 {} 问题）=====
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Cloudflared Panel</title>
<style>
body {{ font-family: Arial; background:#111; color:#eee; padding:40px }}
input,select,button {{ font-size:16px; padding:6px; margin:5px }}
pre {{ background:#000; padding:15px; border-radius:8px; overflow:auto }}
</style>
</head>

<body>

<h2>Cloudflared 控制面板</h2>

<form method="post" action="/run">

<select name="main_cmd">
<option value="tunnel">tunnel</option>
<option value="service">service</option>
</select>

<input name="sub_cmd"
placeholder="例如: list 或 --help"
style="width:350px">

<button type="submit">执行</button>

</form>

{output}

</body>
</html>
"""


# ===== 首页 =====
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE.format(output="")


# ===== 执行命令 =====
@app.post("/run", response_class=HTMLResponse)
def run_cmd(
    main_cmd: str = Form(...),
    sub_cmd: str = Form("")
):

    try:

        # ===== 主命令校验 =====
        if main_cmd not in ["tunnel", "service"]:
            raise ValueError("Invalid main command")

        # ===== 输入长度限制 =====
        if len(sub_cmd) > 200:
            raise ValueError("Input too long")

        # ===== 安全拆分参数 =====
        sub_args = shlex.split(sub_cmd)

        # ===== 构造命令 =====
        cmd = ["sudo", CF_BIN, main_cmd] + sub_args

        print("EXEC:", cmd)

        # ===== 执行 =====
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180
        )

        output_text = (
            "=== STDOUT ===\n"
            + result.stdout +
            "\n\n=== STDERR ===\n"
            + result.stderr
        )

    except Exception:
        output_text = traceback.format_exc()

    return HTML_PAGE.format(
        output=f"<pre>{output_text}</pre>"
    )


# ===== 健康检查 =====
@app.get("/health")
def health():
    return {"status": "ok"}
