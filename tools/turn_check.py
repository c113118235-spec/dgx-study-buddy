"""檢查 TURN 中繼伺服器現在能不能用（校外連不上、畫面卡在 connecting 時先跑這個）。

在 xiaoqing 容器裡跑（讀 data/turn.json，不會印出密碼）：
  docker exec xiaoqing python /app/tools/turn_check.py
會分別用 UDP 和 TCP 向 TURN 伺服器申請中繼位址，印出成功與否、花幾秒。
  兩個都成功：TURN 正常，問題在別處（瀏覽器那端的網路、或伺服器其他設定）
  都失敗／逾時：TURN 伺服器現在不能用（免費版不穩、帳號密碼錯、或被擋）
"""
import asyncio
import json
import os
import time
from urllib.parse import urlparse

import aioice.turn as turn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def servers():
    with open(os.path.join(ROOT, "data", "turn.json"), encoding="utf-8") as f:
        c = json.load(f)
    out = []
    for s in c.get("ice_servers", []):
        urls = s.get("urls") or []
        for u in [urls] if isinstance(urls, str) else urls:
            if not u.startswith("turn:"):
                continue
            hostport, _, q = u[5:].partition("?")
            host, _, port = hostport.rpartition(":")
            tr = "tcp" if "transport=tcp" in q else "udp"
            out.append((host, int(port or 3478), tr, s.get("username"), s.get("credential")))
    return out


async def check(host, port, tr, user, pw):
    t0 = time.time()
    try:
        tp, _ = await asyncio.wait_for(
            turn.create_turn_endpoint(asyncio.DatagramProtocol, (host, port), user, pw, transport=tr), timeout=10)
        relay = tp.get_extra_info("sockname")
        tp.close()
        print(f"  {tr.upper()} {host}:{port}  成功（{time.time() - t0:.2f} 秒），中繼位址 {relay[0]}:{relay[1]}")
        return True
    except asyncio.TimeoutError:
        print(f"  {tr.upper()} {host}:{port}  逾時（10 秒沒回應）")
    except Exception as e:
        print(f"  {tr.upper()} {host}:{port}  失敗：{e.__class__.__name__} {str(e)[:120]}")
    return False


async def main():
    try:
        lst = servers()
    except FileNotFoundError:
        print("找不到 data/turn.json（沒有設定 TURN）")
        return
    if not lst:
        print("data/turn.json 裡沒有 turn: 網址（如果用 Cloudflare／Metered，這個工具不適用）")
        return
    print("向 TURN 伺服器申請中繼位址：")
    ok = [await check(*s) for s in lst]
    print("結論：" + ("TURN 正常" if all(ok) else "TURN 部分可用" if any(ok) else "TURN 現在不能用"))


asyncio.run(main())
