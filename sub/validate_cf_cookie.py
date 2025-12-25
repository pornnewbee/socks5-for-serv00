#!/usr/bin/env python3
# coding: utf-8

import os, sys, asyncio, aiohttp

CF_COOKIE = os.getenv("CF_COOKIE") or ""
if not CF_COOKIE:
    print("❌ 未检测到 CF_COOKIE 环境变量")
    sys.exit(1)

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://dash.cloudflare.com",
    "referer": "https://dash.cloudflare.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "cookie": CF_COOKIE,
}

async def validate_cookie():
    url = "https://dash.cloudflare.com/api/v4/persistence/user"
    timeout = aiohttp.ClientTimeout(total=10)
    
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    email = data.get("result", {}).get("email", "未知")
                    print(f"✅ CF_COOKIE 验证通过，用户: {email}")
                    return True
                else:
                    text = await resp.text()
                    print(f"❌ CF_COOKIE 验证失败，HTTP {resp.status}: {text[:120]}")
                    return False
        except Exception as e:
            print(f"❌ CF_COOKIE 验证异常: {e}")
            return False

if __name__ == "__main__":
    success = asyncio.run(validate_cookie())
    if not success:
        sys.exit(1)
