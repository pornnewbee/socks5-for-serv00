#!/usr/bin/env python3
# coding: utf-8
"""
异步日志抓取脚本（主线程 + 从线程恢复机制）
要求：Python 3.8+，aiohttp
环境变量：
  - ACCOUNTS_JSON: JSON 字符串，形如 {"acctid1": "service1", ...}
  - CF_COOKIE: Cloudflare cookie 字符串
用法：
  python sub/fetcher.py 7          # 查询最近7天（UTC）
  python sub/fetcher.py 20251101   # 指定某天 YYYYMMDD
  python sub/fetcher.py -68dc013... 7  # 也支持 -<account_id> 选择特定账户
"""

import os
import sys
import json
import copy
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone

# ===================== 配置区（按需调整） =====================
SEGMENTS_PER_DAY = 48                  # 每天拆成几段（时间粒度）
MAX_CONCURRENT_ACCOUNTS = 1            # 同时启动多少个账户抓取（1 = 串行账户）
FOLLOWER_START_INTERVAL = 1            # 每个从线程启动间隔（秒）
FOLLOWER_RECOVERY_INTERVAL = 1         # 恢复暂停任务时的间隔（秒）
# ============================================================

# 从环境变量读取 ACCOUNTS（JSON 字符串）
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON，请在 GitHub Secrets/Variables 中设置")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("ACCOUNTS_JSON must be a JSON object")
except Exception as e:
    print("❌ ACCOUNTS_JSON 内容无效：", e)
    sys.exit(1)

URL_TEMPLATE = "https://dash.cloudflare.com/api/v4/accounts/{account_id}/workers/observability/telemetry/query"
LOCAL_COOKIE = os.getenv("CF_COOKIE") or ""
if not LOCAL_COOKIE or len(LOCAL_COOKIE) < 20:
    print("❌ 未检测到有效 CF_COOKIE，请在环境变量 CF_COOKIE 中设置")
    sys.exit(1)

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://dash.cloudflare.com",
    "referer": "https://dash.cloudflare.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "workers-observability-origin": "workers-logs",
    "x-cross-site-security": "dash",
    "cookie": LOCAL_COOKIE,
}

# ===================== 工具函数 =====================
def get_date_list(arg: str):
    """返回要查询的日期列表（UTC，格式 YYYYMMDD）"""
    if arg and arg.isdigit() and len(arg) == 8:
        return [arg]
    try:
        n = int(arg) if arg and arg.isdigit() else 7  # 默认 7 天（包含今天 UTC）
    except Exception:
        n = 7
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]

def split_timeframes(date_str, segments=SEGMENTS_PER_DAY):
    """将一天分割为若干时间段（返回 list of (start_ms,end_ms)）"""
    dt = datetime.strptime(date_str, "%Y%m%d")
    start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step = (end_ms - start_ms) // segments
    arr = []
    for i in range(segments):
        s = start_ms + i * step
        e = s + step
        if i == segments - 1:
            e = end_ms
        arr.append((s, e))
    return arr

def linear_delay(attempt: int):
    """线性退避：第一次 0.5s，第二次 1s，... 上限 10s"""
    return min(0.5 * attempt, 10.0)

# ===================== 异步抓取函数 =====================
async def fetch_segment(session, account_id, service_name, seg_id, start_ms, end_ms, paused_queue=None, offset=None, is_main=False, main_ok_event: asyncio.Event = None):
    """
    抓取单段日志（分页 + 无限重试 + 线性退避 + 支持 offset 恢复）
    参数:
      - paused_queue: asyncio.Queue，用于从线程遇到 429 时存放暂停任务 (seg_id,start_ms,end_ms,offset)
      - offset: 用于恢复分页
      - is_main: 如果 True 表示主线程（永远持续尝试且会设置/清除 main_ok_event）
      - main_ok_event: asyncio.Event，主线程成功时 set(); 遇 429 时 clear()
    返回:
      dict 所有抓到的 invocations（按原始 API 的结构）
    """
    all_logs = {}
    page = 0
    base_data = {
        "view": "invocations",
        "queryId": "workers-logs-invocations",
        "limit": 100,
        "parameters": {
            "datasets": ["cloudflare-workers"],
            "filters": [
                {"key": "$metadata.service", "type": "string", "value": service_name, "operation": "eq"}
            ],
            "calculations": [], "groupBys": [], "havings": []
        },
        "timeframe": {"from": start_ms, "to": end_ms}
    }

    # offset 用于分页恢复
    while True:
        data = copy.deepcopy(base_data)
        if offset:
            data["offset"] = offset

        attempt = 1
        while True:
            try:
                async with session.post(URL_TEMPLATE.format(account_id=account_id), headers=HEADERS, json=data, timeout=30) as resp:
                    status = resp.status
                    text = await resp.text()
                    # 成功
                    if status == 200:
                        # 如果主线程之前被标记为不可用（main_ok_event cleared），现在成功则 set()
                        if is_main and main_ok_event is not None and not main_ok_event.is_set():
                            main_ok_event.set()
                            # 主恢复：log
                            print(f"🔔 {account_id}/{service_name} 主线程已恢复（HTTP 200）")
                        # 解析 JSON
                        try:
                            result = await resp.json()
                        except Exception as e:
                            print(f"❌ {account_id}/{service_name} 第{seg_id}段 JSON 解码异常: {e}")
                            # 解析异常按重试处理
                            pass
                        else:
                            break  # 成功拿到 result，跳出重试循环
                    else:
                        # 遇到非 200
                        print(f"⚠️ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 HTTP {status}")
                        if text:
                            print(f"   返回内容: {text[:300]}")
                        # 主线程遇到 429 -> 标记不可恢复状态（clear），但持续重试
                        if status == 429:
                            if is_main and main_ok_event is not None:
                                if main_ok_event.is_set():
                                    main_ok_event.clear()
                                    print(f"⛔ {account_id}/{service_name} 主线程检测到 429，切换到退避模式（主线程不停止）")
                            # 若是 follower（paused_queue 提供），把任务挂起并返回
                            if paused_queue is not None and not is_main:
                                # 把当前 offset 一并保存（可能为 None）
                                print(f"♻️ {account_id}/{service_name} 第{seg_id}段 从线程遇到 429，暂停并入队等待恢复 (offset={offset})")
                                await paused_queue.put((seg_id, start_ms, end_ms, offset))
                                return all_logs
                            # 否则（主线程）继续下面的重试逻辑
                        # 对于 5xx/4xx 其它码，主/从都将在下面等待后重试（主线程不会停止）
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"❌ {account_id}/{service_name} 第{seg_id}段 网络/请求异常: {e}")

            # 线性退避，永远重试（主线程 & 从线程的非429情形也采用此策略）
            delay = linear_delay(attempt)
            print(f"⏳ {account_id}/{service_name} 第{seg_id} 第{page+1}页 第{attempt}次重试，等待 {delay:.1f}s")
            await asyncio.sleep(delay)
            attempt += 1
            # 注意：无限重试，不再以次数为上限

        # 到这里拿到 result（或跳出）
        if not result or "result" not in result or "invocations" not in result["result"]:
            # 非正常结构，结束该段（避免死循环）；但对于主线程我们仍然继续尝试下一次（这里选择 break 是为了安全）
            print(f"❌ {account_id}/{service_name} 第{seg_id}段 收到空或异常响应结构，终止该段")
            break

        invocations = result["result"].get("invocations", {})
        if not invocations:
            # 本段没有日志，结束该段
            break

        # 合并日志
        all_logs.update(invocations)
        page += 1
        print(f"✅ {account_id}/{service_name} 第{seg_id}段 第{page}页 获取 {len(invocations)} 条日志")

        # 计算下一页 offset（基于最后一个 request id 的最后条目的 $metadata.id）
        offset = None
        for req_id in reversed(list(invocations.keys())):
            logs_list = invocations[req_id]
            if isinstance(logs_list, list) and logs_list:
                metadata = logs_list[-1].get("$metadata", {})
                offset = metadata.get("id")
                if offset:
                    break

        # 如果没有 offset，说明已经读完本段
        if not offset:
            break

        # 否则继续循环去抓下一页（offset 已经设置）
    return all_logs

# ===================== 账户抓取主流程 =====================
async def fetch_account(account_id, service_name, dates):
    """
    每个账户：主线程抓第一段并维持 main_ok_event，
    从线程负责其余段，遇到 429 放入 paused_queue，主线程恢复后逐个恢复 paused_queue 中的任务（带 offset）
    """
    async with aiohttp.ClientSession() as session:
        for date_str in dates:
            print(f"\n===== 抓取 {account_id}/{service_name} 的 {date_str} 日日志（UTC） =====")
            ranges = split_timeframes(date_str)
            all_logs = {}
            pending_segments = ranges.copy()
            paused_queue = asyncio.Queue()
            main_ok_event = asyncio.Event()
            main_ok_event.set()  # 初始为可恢复状态

            # 主线程负责第 1 段（编号 1）
            main_seg = pending_segments.pop(0)
            print(f"▶️ 启动主线程抓取第1段: {main_seg}")
            main_logs = await fetch_segment(
                session, account_id, service_name, 1, main_seg[0], main_seg[1],
                paused_queue=paused_queue, offset=None, is_main=True, main_ok_event=main_ok_event
            )
            all_logs.update(main_logs)

            # 启动从线程抓剩余段（并不会阻塞主线程的继续重试——主线程已完成第一段的持续尝试）
            tasks = {}
            for seg_index, (s_ms, e_ms) in enumerate(pending_segments, start=2):
                await asyncio.sleep(FOLLOWER_START_INTERVAL)
                print(f"▶️ 启动从线程抓第{seg_index}段: {(s_ms, e_ms)}")
                t = asyncio.create_task(fetch_segment(
                    session, account_id, service_name, seg_index, s_ms, e_ms,
                    paused_queue=paused_queue, offset=None, is_main=False, main_ok_event=main_ok_event
                ))
                tasks[seg_index] = t

            # 恢复器：当 paused_queue 有任务并且主线程处于可恢复状态 (main_ok_event.is_set()) 时
            # 逐个恢复 paused_queue 中的任务（带 offset），恢复间隔 FOLLOWER_RECOVERY_INTERVAL
            async def recovery_loop():
                while True:
                    # 如果没有 paused、没有正在跑的 tasks，则退出
                    if paused_queue.empty() and not tasks:
                        return
                    # 只在主线程可用时恢复一个 paused 任务
                    if main_ok_event.is_set() and not paused_queue.empty():
                        seg_id, s_ms, e_ms, saved_offset = await paused_queue.get()
                        print(f"♻️ 恢复任务: {account_id}/{service_name} 第{seg_id}段 (offset={saved_offset})")
                        # 启动一个新任务从 saved_offset 继续抓
                        t = asyncio.create_task(fetch_segment(
                            session, account_id, service_name, seg_id, s_ms, e_ms,
                            paused_queue=paused_queue, offset=saved_offset, is_main=False, main_ok_event=main_ok_event
                        ))
                        tasks[seg_id] = t
                        await asyncio.sleep(FOLLOWER_RECOVERY_INTERVAL)
                    else:
                        # 如果主线程不可用 或 paused_queue 空，等一会儿再检查
                        await asyncio.sleep(1)

            # 等待 tasks 完成或加入恢复循环处理 paused_queue
            # 同时周期性合并已经完成的从线程结果
            recovery_task = asyncio.create_task(recovery_loop())
            try:
                while tasks or not paused_queue.empty():
                    # 检查已完成任务并合并结果
                    for seg_id, t in list(tasks.items()):
                        if t.done():
                            try:
                                res = t.result()
                                if res:
                                    all_logs.update(res)
                            except Exception as e:
                                print(f"❌ {account_id}/{service_name} 第{seg_id}段 异常: {e}")
                            tasks.pop(seg_id)
                    await asyncio.sleep(0.5)
                # 等待恢复任务结束（resume loop 退出）
                await recovery_task
            finally:
                # make sure recovery_task cancelled if still running
                if not recovery_task.done():
                    recovery_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await recovery_task

            # 保存 JSON（按 account+date）
            out_file = f"{account_id}_invocations_{date_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)
            print(f"📦 {account_id} 已保存 {len(all_logs)} 条日志 -> {out_file}")

# ===================== 主程序 =====================
import contextlib

async def main_async():
    args = sys.argv[1:]
    # 支持 -<account_id> 选择账户，也支持数字参数表示天数或 YYYYMMDD
    selected_days = None
    selected_accounts = []
    for a in args:
        if a.startswith("-"):
            selected_accounts.append(a[1:])
        elif a.isdigit():
            if len(a) == 8:
                selected_days = a  # single date
            else:
                # numeric -> treat as number of days
                try:
                    int(a)
                    selected_days = a
                except:
                    pass

    if selected_days is None:
        selected_days = "7"  # 默认最近7天

    # Build accounts map to operate on
    if selected_accounts:
        accounts = {k: v for k, v in ACCOUNTS.items() if k in selected_accounts}
        if not accounts:
            print("❌ 没有匹配的账户ID，退出")
            return
    else:
        accounts = ACCOUNTS

    # date list
    if len(selected_days) == 8 and selected_days.isdigit():
        dates = [selected_days]
    else:
        dates = get_date_list(selected_days)

    print(f"📅 查询天数: {len(dates)} -> {dates}")
    print(f"👥 目标账户: {', '.join(accounts.keys())}")

    account_list = list(accounts.items())
    # 控制同时查询账户数（batch）
    for i in range(0, len(account_list), MAX_CONCURRENT_ACCOUNTS):
        batch = account_list[i:i + MAX_CONCURRENT_ACCOUNTS]
        tasks = [fetch_account(acc_id, svc_name, dates) for acc_id, svc_name in batch]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main_async())
