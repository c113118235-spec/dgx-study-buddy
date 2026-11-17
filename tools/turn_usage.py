"""看 TURN 中繼（ExpressTURN）這個月大約用了多少流量。ExpressTURN 免費版後台看不到用量，所以小晴自己記。

在 DGX 主機上跑（或容器裡）：
  python3 tools/turn_usage.py            # 這個月
  python3 tools/turn_usage.py --all      # 全部紀錄
資料來源：每條連線結束時寫進 data/turn_usage.jsonl（伺服器更新到有這功能之後才開始記）。
估算方式：走中繼的連線，把它收送的資料量算進去；兩端都走中繼的算兩次。
這是估計值：被瀏覽器直接關掉、沒走正常釋放流程的連線不會記到。
"""
import argparse
import datetime as dt
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "turn_usage.jsonl")
QUOTA_GB = 1000

ap = argparse.ArgumentParser()
ap.add_argument("--all", action="store_true")
ap.add_argument("--quota", type=float, default=QUOTA_GB)
a = ap.parse_args()

now = dt.datetime.now()
start = 0 if a.all else dt.datetime(now.year, now.month, 1).timestamp()
total = relay_n = 0
used_mb = 0.0
try:
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t", 0) < start:
                continue
            total += 1
            if r.get("relay"):
                relay_n += 1
                used_mb += r.get("mb", 0) * r["relay"]
except FileNotFoundError:
    print("還沒有紀錄（伺服器要更新到有這功能、而且有人用完離開後才會開始記）")
    raise SystemExit

gb = used_mb / 1000
span = "全部紀錄" if a.all else f"{now.year}/{now.month:02d}"
print(f"{span}：共 {total} 條連線，其中 {relay_n} 條走 TURN 中繼")
print(f"中繼流量約 {gb:.2f} GB／{a.quota:.0f} GB（剩約 {max(0, a.quota - gb):.1f} GB，用了 {gb / a.quota * 100:.2f}%）")
print("（估計值；ExpressTURN 每月重新計算）")
