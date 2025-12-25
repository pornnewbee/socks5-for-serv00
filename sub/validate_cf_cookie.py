#!/usr/bin/env python3
# coding: utf-8

import os, sys, asyncio, aiohttp

CF_COOKIE = os.getenv("CF_COOKIE") or ""
if not CF_COOKIE:
    print("❌ 未检测到 CF_COOKIE")
    sys.exit(1)

HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://dash.cloudflare.com",
    "referer": "https://dash.cloudflare.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "cookie": CF_COOKIE,
}

URL = "https://dash.cloudflare.com/api/v4/accounts?per_page=25"

async def main():
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(URL, headers=HEADERS) as resp:
                text = await resp.text()
                if resp.status == 200:
                    print("✅ CF_COOKIE 验证成功")
                    sys.exit(0)
                else:
                    print(f"❌ CF_COOKIE 验证失败，HTTP {resp.status}: {text[:200]}")
                    sys.exit(1)
        except aiohttp.ClientError as e:
            print(f"❌ 网络异常: {e}")
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
