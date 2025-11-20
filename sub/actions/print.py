#!/usr/bin/env python3
"""
每秒输出 N 行（默认 1000），输出到 total 行（默认 50000）后退出。
用法: python3 burst_lines.py [--lines-per-sec N] [--total-lines M]
"""
import time
import sys
import argparse

def run(lines_per_sec: int, total_lines: int):
    chunk = lines_per_sec
    line_no = 1
    try:
        while line_no <= total_lines:
            start = time.perf_counter()
            # 计算本次要生成的行数（最后一批可能不足 chunk）
            this_batch = min(chunk, total_lines - line_no + 1)
            # 一次性生成并写出，减少系统调用开销
            lines = []
            for i in range(this_batch):
                # 自定义每行内容：行号 + 当前时间（可去掉时间以提高速度）
                lines.append(f"Line {line_no + i}")
            out = "\n".join(lines) + "\n"
            sys.stdout.write(out)
            sys.stdout.flush()

            line_no += this_batch
            elapsed = time.perf_counter() - start
            to_sleep = 1.0 - elapsed
            if to_sleep > 0:
                time.sleep(to_sleep)
            # 如果写入已经超过1秒，立即进入下一批（不做补偿）
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted by user.\n")
        sys.exit(1)

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Burst-line printer")
    p.add_argument("--lines-per-sec", "-r", type=int, default=1000,
                   help="每秒输出行数（默认 1000）")
    p.add_argument("--total-lines", "-t", type=int, default=50000,
                   help="总输出行数（默认 50000）")
    args = p.parse_args()
    run(args.lines_per_sec, args.total_lines)
