#!/usr/bin/env python3
"""量小晴在 DGX 上跑的時候「最大瓦數」和「記憶體用量」。

在 DGX 主機上跑（不是容器裡；要用到 nvidia-smi 和 docker），只用 Python 內建套件：
  # 1. 待機量 60 秒
  python3 tools/power_mem_monitor.py --seconds 60 --label 待機
  # 2. 一邊跑壓力測試一邊量（測試跑完就自動停止）
  python3 tools/power_mem_monitor.py --label 20人6人說話 --during \
    "docker exec xiaoqing python -u /app/tools/load_test.py --n 20 --speak 6 --voices iai_9D,iai_36D,female --text 嗨，今天過得好嗎"
  # 不加 --seconds 也不加 --during：一直量到按 Ctrl+C

量什麼（每秒一次）：
  GPU：耗電（W）、使用率、溫度、時脈（nvidia-smi）
  整台主機的記憶體：已用 = MemTotal − MemAvailable（DGX Spark 的 GPU 和 CPU 共用同一塊記憶體，所以這就是全部）
  各小晴容器的記憶體與 CPU（docker stats，每 5 秒一次）
輸出：
  畫面上的總結（最大值、平均值），以及 data/monitor/<時間>_<標籤>.csv（每秒一筆，可以畫圖）
注意：
  nvidia-smi 的 power.draw 是 GPU 這顆晶片的耗電，不是整台機器插座的瓦數；整機要用插座型電表量。
"""
import argparse
import csv
import datetime as dt
import os
import shlex
import signal
import statistics
import subprocess
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPU_FIELDS = ["power.draw", "power.limit", "utilization.gpu", "temperature.gpu", "clocks.sm", "memory.used", "memory.total"]
CONTAINER_PREFIX = "xiaoqing"


def num(s):
    try:
        return float(str(s).strip().split()[0])
    except (ValueError, IndexError):
        return None                                   # [N/A]、Not Supported


def gpu_sample():
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if not out:
        return {}
    vals = [v.strip() for v in out[0].split(",")]     # 只有一顆 GPU（DGX Spark）
    return {k: num(v) for k, v in zip(GPU_FIELDS, vals)}


def mem_sample():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = float(v.split()[0]) / 1024 / 1024       # kB → GiB
    except OSError:
        return {}
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    if total is None or avail is None:
        return {}
    return {"mem_total_gib": total, "mem_used_gib": total - avail, "swap_used_gib": info.get("SwapTotal", 0) - info.get("SwapFree", 0)}


def to_gib(s):
    s = s.strip()
    for unit, mul in (("TiB", 1024), ("GiB", 1), ("MiB", 1 / 1024), ("KiB", 1 / 1024 / 1024), ("TB", 931.3 / 1000), ("GB", 0.9313),
                      ("MB", 0.9313 / 1000), ("kB", 0.9313 / 1e6), ("B", 1 / 1024 ** 3)):
        if s.endswith(unit):
            try:
                return float(s[: -len(unit)]) * mul
            except ValueError:
                return None
    return None


class DockerStats(threading.Thread):
    """docker stats 一次要 1～2 秒，放在背景每 5 秒抓一次，主迴圈拿最新的一筆"""

    def __init__(self):
        super().__init__(daemon=True)
        self.latest, self.peak, self.stop = {}, {}, threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}"],
                                     capture_output=True, text=True, timeout=20).stdout
                snap = {}
                for line in out.splitlines():
                    name, mem, cpu = (line.split("|") + ["", ""])[:3]
                    if not name.startswith(CONTAINER_PREFIX):
                        continue
                    g = to_gib(mem.split("/")[0])
                    snap[name] = {"mem_gib": g, "cpu_pct": num(cpu.replace("%", ""))}
                    if g is not None:
                        self.peak[name] = max(self.peak.get(name, 0), g)
                self.latest = snap
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            self.stop.wait(5)


def stats(xs):
    xs = [x for x in xs if x is not None]
    return (max(xs), statistics.mean(xs)) if xs else (None, None)


def fmt(v, unit, nd=1):
    return "—（不支援）" if v is None else f"{v:.{nd}f} {unit}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="量幾秒（0 = 量到 --during 的指令結束，或按 Ctrl+C）")
    ap.add_argument("--during", default="", help="同時執行的指令（例如壓力測試），指令結束就停止")
    ap.add_argument("--label", default="測試", help="這次量測的名稱，會放進檔名和總結")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()

    out_dir = os.path.join(ROOT, "data", "monitor")
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in a.label)[:40]
    path = os.path.join(out_dir, f"{stamp}_{safe}.csv")

    ds = DockerStats()
    ds.start()
    proc = None
    if a.during:
        proc = subprocess.Popen(shlex.split(a.during))
        print(f"同時執行：{a.during}")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f"開始量測「{a.label}」，每 {a.interval:g} 秒一筆，存到 {os.path.relpath(path, ROOT)}（Ctrl+C 停止）")

    rows, t0 = [], time.time()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["秒", "GPU 耗電(W)", "GPU 使用率(%)", "GPU 溫度(C)", "GPU 時脈(MHz)", "主機記憶體已用(GiB)", "swap 已用(GiB)",
                    "小晴容器記憶體合計(GiB)", "小晴容器 CPU 合計(%)"])
        while not stop.is_set():
            t = time.time() - t0
            g, m = gpu_sample(), mem_sample()
            snap = ds.latest
            cmem = sum(v["mem_gib"] or 0 for v in snap.values()) if snap else None
            ccpu = sum(v["cpu_pct"] or 0 for v in snap.values()) if snap else None
            r = {"t": t, "power": g.get("power.draw"), "util": g.get("utilization.gpu"), "temp": g.get("temperature.gpu"),
                 "clk": g.get("clocks.sm"), "mem": m.get("mem_used_gib"), "swap": m.get("swap_used_gib"), "cmem": cmem, "ccpu": ccpu}
            rows.append(r)
            w.writerow([f"{t:.0f}"] + ["" if r[k] is None else f"{r[k]:.2f}" for k in ("power", "util", "temp", "clk", "mem", "swap", "cmem", "ccpu")])
            f.flush()
            if len(rows) % 10 == 1:
                print(f"  {t:5.0f}s  GPU {fmt(r['power'], 'W')}、使用率 {fmt(r['util'], '%', 0)}｜主機記憶體 {fmt(r['mem'], 'GiB')}", flush=True)
            if a.seconds and t >= a.seconds:
                break
            if proc is not None and proc.poll() is not None:
                break
            stop.wait(a.interval)
    ds.stop.set()
    if proc is not None and proc.poll() is None:
        proc.terminate()

    p_max, p_avg = stats(r["power"] for r in rows)
    u_max, u_avg = stats(r["util"] for r in rows)
    t_max, _ = stats(r["temp"] for r in rows)
    m_max, m_avg = stats(r["mem"] for r in rows)
    c_max, _ = stats(r["cmem"] for r in rows)
    m0 = next((r["mem"] for r in rows if r["mem"] is not None), None)
    g = gpu_sample()
    total = mem_sample().get("mem_total_gib")
    dur = rows[-1]["t"] if rows else 0
    print(f"\n===== 總結：{a.label}（{dur:.0f} 秒，{len(rows)} 筆）=====")
    print(f"GPU 耗電    最大 {fmt(p_max, 'W')}、平均 {fmt(p_avg, 'W')}" + (f"（上限 {g['power.limit']:.0f} W）" if g.get("power.limit") else ""))
    print(f"GPU 使用率  最大 {fmt(u_max, '%', 0)}、平均 {fmt(u_avg, '%', 0)}｜最高溫度 {fmt(t_max, '°C', 0)}")
    print(f"主機記憶體  最大 {fmt(m_max, 'GiB')}、平均 {fmt(m_avg, 'GiB')}" + (f"（共 {total:.0f} GiB，開始時 {m0:.1f} GiB）" if total and m0 else ""))
    print(f"小晴容器記憶體合計  最大 {fmt(c_max, 'GiB')}")
    for name in sorted(ds.peak):
        print(f"  {name:22s} 最大 {ds.peak[name]:.2f} GiB")
    print(f"每秒紀錄：{os.path.relpath(path, ROOT)}")
    print("註：GPU 耗電是 GPU 晶片的數字，不是整台機器插座的瓦數；DGX Spark 的 GPU 和 CPU 共用主機記憶體。")


if __name__ == "__main__":
    main()
