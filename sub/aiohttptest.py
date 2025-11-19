#!/usr/bin/env python3
# coding: utf-8

import os
import sys
import json
import copy
import asyncio
import aiohttp
import contextlib
from datetime import datetime, timedelta, timezone

# ===================== 配置区（按需调整） =====================
SEGMENTS_PER_DAY = 48                  # 每天拆成几段（时间粒度）
MAX_CONCURRENT_ACCOUNTS = 1            # 同时启动多少个账户抓取（1 = 串行账户）
FOLLOWER_START_INTERVAL = 1            # 每个从线程启动间隔（秒）
FOLLOWER_RECOVERY_INTERVAL = 1         # 恢复暂停任务时每个任务的间隔（秒）
# ============================================================

# 从环境变量读取 ACCOUNTS（JSON 字符串）
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON，请在 GitHub Secrets/Variables 中设置")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("ACCOUNTS_JSON must be a JSON object mapping account_id -> service_name")
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

# ===================== 抓取实现 =====================
async def fetch_segment_follower(session, account_id, service_name, seg_id, start_ms, end_ms,
                                 shared_progress, paused_queue: asyncio.Queue, offset=None, is_main=False, main_ok_event: asyncio.Event=None):
    """
    follower / main common worker that:
      - saves progress to shared_progress[seg_id] after each page
      - if sees shared_progress[seg_id]['take_request'] -> stop and return current progress
      - if HTTP 429 and paused_queue provided and not is_main -> enqueue paused task (seg_id,start,end,offset) and exit
      - unlimited retries with linear backoff
    Returns dict: collected logs for this segment
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

    # ensure progress entry exists
    shared_progress.setdefault(seg_id, {
        "offset": offset,
        "logs": {},
        "done": False,
        "take_request": False,
        "stopped_event": asyncio.Event(),
    })

    # read initial offset if present
    cur_offset = shared_progress[seg_id].get("offset")

    while True:
        data = copy.deepcopy(base_data)
        if cur_offset:
            data["offset"] = cur_offset

        attempt = 1
        result = None
        while True:
            try:
                async with session.post(URL_TEMPLATE.format(account_id=account_id),
                                        headers=HEADERS, json=data, timeout=30) as resp:
                    status = resp.status
                    text = await resp.text()
                    if status == 200:
                        # if main was previously marked bad, mark ok now
                        if is_main and main_ok_event is not None and not main_ok_event.is_set():
                            main_ok_event.set()
                            print(f"🔔 {account_id}/{service_name} 主线程已恢复 (HTTP 200)")
                        try:
                            result = await resp.json()
                        except Exception as e:
                            print(f"❌ {account_id}/{service_name} 第{seg_id}段 JSON 解码异常: {e}")
                            result = None
                        # break if we got a JSON object (may be None -> will cause retry/continue)
                        if isinstance(result, dict):
                            break
                    else:
                        # non-200
                        print(f"⚠️ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 HTTP {status}")
                        if text:
                            print(f"   返回内容: {text[:300]}")

                        # 主线程遇到 429 -> 标记 main_ok_event clear(), 但主线程继续重试
                        if status == 429:
                            if is_main and main_ok_event is not None:
                                if main_ok_event.is_set():
                                    main_ok_event.clear()
                                    print(f"⛔ {account_id}/{service_name} 主线程检测到 429，进入退避(主线程仍持续尝试)。")
                            # 如果是 follower (not main) and paused_queue provided -> pause this follower and return
                            if (not is_main) and paused_queue is not None:
                                print(f"♻️ {account_id}/{service_name} 第{seg_id}段 从线程遇到 429，暂停并入队等待恢复 (offset={cur_offset})")
                                await paused_queue.put((seg_id, start_ms, end_ms, cur_offset))
                                # signal stopped
                                shared_progress[seg_id]["stopped_event"].set()
                                return all_logs
                        # otherwise will retry (linear backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"❌ {account_id}/{service_name} 第{seg_id}段 网络/请求异常: {e}")

            # check if take_request flagged (someone requested this follower to be taken over)
            if shared_progress[seg_id].get("take_request"):
                # mark stopped and return current progress so main can take over
                print(f"🔁 {account_id}/{service_name} 第{seg_id} 段 收到 take_request，正在停止并交出进度 (offset={cur_offset})")
                shared_progress[seg_id]["offset"] = cur_offset
                shared_progress[seg_id]["logs"] = dict(all_logs)
                shared_progress[seg_id]["stopped_event"].set()
                return all_logs

            # linear backoff, infinite retries
            delay = linear_delay(attempt)
            print(f"⏳ {account_id}/{service_name} 第{seg_id} 第{page+1}页 第{attempt}次重试，等待 {delay:.1f}s")
            await asyncio.sleep(delay)
            attempt += 1

        # result obtained
        if not result or "result" not in result or "invocations" not in result["result"]:
            print(f"❌ {account_id}/{service_name} 第{seg_id}段 收到空或异常响应结构，结束该段")
            break

        invocations = result["result"].get("invocations", {})
        if not invocations:
            # empty segment -> done
            break

        # merge into all_logs
        all_logs.update(invocations)
        page += 1
        print(f"✅ {account_id}/{service_name} 第{seg_id}段 第{page}页 获取 {len(invocations)} 条日志")

        # compute next offset
        next_offset = None
        for req_id in reversed(list(invocations.keys())):
            logs_list = invocations[req_id]
            if isinstance(logs_list, list) and logs_list:
                metadata = logs_list[-1].get("$metadata", {})
                next_offset = metadata.get("id")
                if next_offset:
                    break
        cur_offset = next_offset

        # save progress after each page (so takeover/resume can continue)
        shared_progress[seg_id]["offset"] = cur_offset
        # store a shallow copy of logs (to keep memory reasonable - it's user's decision)
        shared_progress[seg_id]["logs"] = dict(all_logs)

        # check if takeover requested
        if shared_progress[seg_id].get("take_request"):
            print(f"🔁 {account_id}/{service_name} 第{seg_id}段 检测到 take_request，停止并交出进度 (offset={cur_offset})")
            shared_progress[seg_id]["stopped_event"].set()
            return all_logs

        # continue loop; if no next_offset then segment finished
        if not cur_offset:
            break

    # mark done
    shared_progress[seg_id]["done"] = True
    shared_progress[seg_id]["stopped_event"].set()
    return all_logs

# ===================== 账户抓取主流程 =====================
async def fetch_account(account_id, service_name, dates):
    """
    主流程：
      - 主线程抓第1段（持续尝试）
      - 逐步启动 follower 抓剩余段（动态增加）
      - follower 遇 429 -> 放入 paused_queue
      - 主线程完成后接管最新 follower（带 offset & partial logs）
      - paused_queue 中的任务在主线程可用时按顺序恢复（带 offset）
    """
    async with aiohttp.ClientSession() as session:
        for date_str in dates:
            print(f"\n===== 抓取 {account_id}/{service_name} 的 {date_str} 日日志（UTC） =====")
            ranges = split_timeframes(date_str)
            all_logs = {}
            pending_segments = ranges.copy()
            paused_queue = asyncio.Queue()
            shared_progress = {}  # seg_id -> dict(progress)
            tasks = {}

            # 主线程负责第1段（编号 1）
            main_seg = pending_segments.pop(0)
            print(f"▶️ 启动主线程抓取第1段: {main_seg}")
            # main_ok_event 用于标记主线程当前是否可用（未遇 429）
            main_ok_event = asyncio.Event()
            main_ok_event.set()

            # start the main segment as a task but await it (main always keeps trying)
            main_task = asyncio.create_task(fetch_segment_follower(
                session, account_id, service_name, 1, main_seg[0], main_seg[1],
                shared_progress, paused_queue, offset=None, is_main=True, main_ok_event=main_ok_event
            ))
            # Wait until main_task yields first page or completes: we still await full completion later,
            # but we concurrently launch followers.
            # We'll not block here; start followers while main_task is running.
            await asyncio.sleep(0.1)

            # 启动 follower 抓剩余段（按间隔逐步启动）
            seg_index_base = 2
            for seg_index, (s_ms, e_ms) in enumerate(pending_segments, start=seg_index_base):
                await asyncio.sleep(FOLLOWER_START_INTERVAL)
                print(f"▶️ 启动从线程抓第{seg_index}段: {(s_ms, e_ms)}")
                # ensure progress entry exists
                shared_progress.setdefault(seg_index, {
                    "offset": None,
                    "logs": {},
                    "done": False,
                    "take_request": False,
                    "stopped_event": asyncio.Event(),
                })
                t = asyncio.create_task(fetch_segment_follower(
                    session, account_id, service_name, seg_index, s_ms, e_ms,
                    shared_progress, paused_queue, offset=None, is_main=False, main_ok_event=main_ok_event
                ))
                tasks[seg_index] = t

            # 恢复器：主线程恢复后逐个恢复 paused_queue
            async def recovery_loop():
                while True:
                    # when nothing to do, exit
                    if paused_queue.empty() and not tasks:
                        return
                    # only resume a paused follower when main_ok_event is set
                    if main_ok_event.is_set() and not paused_queue.empty():
                        seg_id, s_ms, e_ms, saved_offset = await paused_queue.get()
                        print(f"♻️ 恢复任务: {account_id}/{service_name} 第{seg_id}段 (offset={saved_offset})")
                        # ensure progress entry
                        shared_progress.setdefault(seg_id, {
                            "offset": saved_offset,
                            "logs": {},
                            "done": False,
                            "take_request": False,
                            "stopped_event": asyncio.Event(),
                        })
                        # start new follower from saved_offset
                        t = asyncio.create_task(fetch_segment_follower(
                            session, account_id, service_name, seg_id, s_ms, e_ms,
                            shared_progress, paused_queue, offset=saved_offset, is_main=False, main_ok_event=main_ok_event
                        ))
                        tasks[seg_id] = t
                        await asyncio.sleep(FOLLOWER_RECOVERY_INTERVAL)
                    else:
                        await asyncio.sleep(1)

            recovery_task = asyncio.create_task(recovery_loop())

            # 主线程接管逻辑 & 结果合并循环
            try:
                while True:
                    # If main_task completed and no follower tasks remain and no paused tasks -> done for this date
                    if main_task.done() and not tasks and paused_queue.empty():
                        # merge main result
                        try:
                            res = main_task.result()
                            if res:
                                all_logs.update(res)
                        except Exception as e:
                            print(f"❌ {account_id}/{service_name} 主线程 第1段 异常: {e}")
                        break

                    # If main_task finished its segment early (i.e. returned), but there are follower tasks ongoing,
                    # then main should take over the latest follower task (highest seg_id)
                    if main_task.done():
                        # merge main partial result
                        try:
                            res = main_task.result()
                            if res:
                                all_logs.update(res)
                        except Exception as e:
                            print(f"❌ {account_id}/{service_name} 主线程 第1段 合并异常: {e}")

                        # pick latest running follower to take over
                        running_followers = [sid for sid, tk in tasks.items() if not tk.done()]
                        if running_followers:
                            latest = max(running_followers)
                            print(f"🔁 主线程接管从线程：{account_id}/{service_name} 第{latest}段")
                            # request takeover
                            shared_progress.setdefault(latest, {}).setdefault("take_request", False)
                            shared_progress[latest]["take_request"] = True
                            # wait for follower to acknowledge stop (stopped_event)
                            await shared_progress[latest]["stopped_event"].wait()
                            # read progress
                            saved_offset = shared_progress[latest].get("offset")
                            partial_logs = shared_progress[latest].get("logs", {}) or {}
                            print(f"🔁 主线程接手第{latest}段 (offset={saved_offset})，已收 {len(partial_logs)} 条日志 (from follower)")
                            # merge follower partial logs
                            all_logs.update(partial_logs)
                            # ensure follower task removed if done
                            if latest in tasks:
                                # await the task finishing (it should finish quickly because it saw take_request)
                                with contextlib.suppress(asyncio.CancelledError):
                                    await tasks[latest]
                                tasks.pop(latest, None)
                            # now main takes over remaining part of segment starting from saved_offset
                            print(f"▶️ 主线程继续抓第{latest}段 从 offset={saved_offset}")
                            # create a new main-style fetch for that segment (is_main=True so main_ok_event is respected)
                            main_task = asyncio.create_task(fetch_segment_follower(
                                session, account_id, service_name, latest,
                                ranges[latest - 1][0], ranges[latest - 1][1],
                                shared_progress, paused_queue, offset=saved_offset, is_main=True, main_ok_event=main_ok_event
                            ))
                            # loop continues
                            await asyncio.sleep(0.1)
                            continue
                        else:
                            # no running followers -> maybe paused or none -> if paused exists recovery loop will handle
                            await asyncio.sleep(0.5)
                            continue

                    # normal loop: collect finished follower results
                    for seg_id, t in list(tasks.items()):
                        if t.done():
                            try:
                                res = t.result()
                                if res:
                                    all_logs.update(res)
                            except Exception as e:
                                print(f"❌ {account_id}/{service_name} 第{seg_id}段 异常: {e}")
                            tasks.pop(seg_id, None)
                    await asyncio.sleep(0.5)

                # wait recovery loop to finish
                await recovery_task
            finally:
                if not recovery_task.done():
                    recovery_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await recovery_task

            # 最终保存 JSON（按 account+date）
            out_file = f"{account_id}_invocations_{date_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)
            print(f"📦 {account_id} 已保存 {len(all_logs)} 条日志 -> {out_file}")

# ===================== 主程序 =====================
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
