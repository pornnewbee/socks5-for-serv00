#!/usr/bin/env python3
import http.server
import socketserver
import os

# 配置常量
PORT = 11450
SHARE_DIR = "/home/qbittorrent/Downloads"
# 永久缓存时间：1年 = 31,536,000秒
PERMANENT_CACHE_SECONDS = 31536000

class PermanentCacheHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # 1. 注入绝对缓存头：设置为1年，并添加 immutable 防止用户刷新时 CDN 回源校验
        self.send_header("Cache-Control", f"public, max-age={PERMANENT_CACHE_SECONDS}, immutable")
        # 2. 允许跨域（方便某些前端下载器或播放器调用）
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

def main():
    if not os.path.exists(SHARE_DIR):
        print(f"错误: 目标路径 {SHARE_DIR} 不存在！")
        return
    
    # 核心：强制切换工作目录至下载路径，使其对外公开
    os.chdir(SHARE_DIR)
    
    # 允许端口重用，避免频繁重启服务时报 Address already in use 错误
    socketserver.TCPServer.allow_reuse_address = True
    
    with socketserver.TCPServer(("", PORT), PermanentCacheHTTPRequestHandler) as httpd:
        print(f"==================================================")
        print(f" 成功启动文件分享服务！")
        print(f" 正在分享目录: {SHARE_DIR}")
        print(f" 监听端口: {PORT}")
        print(f" 缓存策略: 永久缓存 (max-age={PERMANENT_CACHE_SECONDS})")
        print(f"==================================================")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已关闭。")

if __name__ == "__main__":
    main()
