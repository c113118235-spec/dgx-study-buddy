"""備用播放模式：WebRTC 打不通時（嚴格的學校／公司網路、VPN、TURN 掛掉），影像和聲音改走 WebSocket。

為什麼可以：WebSocket 走的是網頁本身的 https 連線（公開網址 = Cloudflare Tunnel），
能打開網頁就收得到，不需要 UDP、STUN、TURN。代價是延遲多一點（瀏覽器端約 0.3 秒緩衝）、流量比 H.264 大。

  GET /ws_media?voice=...&avatar=...   （WebSocket，參數跟 /offer 一樣）
  伺服器 → 瀏覽器：
    文字  {"type":"hello","sessionid":"...","audio":{"rate":16000,"format":"s16le"},"video":"jpeg"}
          {"type":"error","msg":"..."}
    二進位 第 1 個 byte 是種類：1 = 一格 JPEG 影像；2 = 20 ms 聲音（16 kHz、16-bit、單聲道 PCM）
  連線關掉 = 這位使用者離開，名額馬上釋放。

畫質可以在 data/av.json 調（改了下一條連線生效）：
  {"ws_max_side": 1280, "ws_jpeg_quality": 82, "ws_fps": 25, "ws_auto": true}
  ws_max_side／ws_jpeg_quality 是「最好」的畫質（1280 = 原始解析度，不縮）。
  ws_auto（預設開）：網路跟不上、影像開始被丟時自動降一級（1080→960→720），順了再慢慢升回來。
"""
from __future__ import annotations

import asyncio
import json
import os

from aiohttp import WSMsgType, web

from utils.logger import logger

AV_CONF = os.path.join("data", "av.json")
VIDEO, AUDIO = b"\x01", b"\x02"


def _conf():
    c = {"ws_max_side": 1280, "ws_jpeg_quality": 82, "ws_fps": 25, "ws_auto": True}
    try:
        with open(AV_CONF, encoding="utf-8") as f:
            d = json.load(f)
        for k in c:
            if k == "ws_auto":
                if isinstance(d.get(k), bool):
                    c[k] = d[k]
            elif isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool) and d[k] > 0:
                c[k] = d[k]
    except Exception:
        pass
    return c


def _encode(img, max_side, quality):
    import cv2
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s) // 2 * 2, int(h * s) // 2 * 2), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


async def ws_media(request):
    from server.session_manager import MaxSessionError, session_manager
    from server.webrtc import HumanPlayer

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20, compress=False)
    await ws.prepare(request)
    params = {k: v for k, v in request.query.items() if k in ("voice", "avatar")}
    try:
        sid = await session_manager.create_session(params)
    except MaxSessionError:
        await ws.send_json({"type": "error", "msg": "Maximum session limit reached"})
        await ws.close()
        return ws
    except Exception as e:
        logger.exception("[gf-ws] 建立連線失敗")
        await ws.send_json({"type": "error", "msg": f"建立連線失敗：{e}"})
        await ws.close()
        return ws

    c = _conf()
    player = HumanPlayer(session_manager.get_session(sid))
    vt, at = player.video, player.audio
    loop = asyncio.get_running_loop()
    latest = {"jpg": None, "seq": 0, "sent": 0}
    fresh = asyncio.Event()
    # 畫質等級：第 0 級是設定的最好畫質，網路跟不上時往後降
    top = (int(c["ws_max_side"]), int(c["ws_jpeg_quality"]))
    levels = [top] + [(m, q) for m, q in ((1080, 75), (960, 70), (720, 62)) if m < top[0]]
    lv = {"i": 0, "made": 0, "dropped": 0, "good": 0}
    logger.info(f"[gf-ws] 備用播放模式 {sid}（最高 {top[0]} px、品質 {top[1]}、{c['ws_fps']} fps，自動調整 {'開' if c['ws_auto'] else '關'}）")
    await ws.send_json({"type": "hello", "sessionid": sid, "audio": {"rate": 16000, "format": "s16le"}, "video": "jpeg"})

    async def video_in():                       # 照 25 fps 取畫面（跟 WebRTC 同一套節奏），壓成 JPEG
        skip = max(1, round(25 / c["ws_fps"]))
        n = 0
        while not ws.closed:
            f = await vt.recv()
            n += 1
            if n % skip:
                continue
            m, q = levels[lv["i"]]
            jpg = await loop.run_in_executor(None, _encode, f.to_ndarray(format="bgr24"), m, q)
            if jpg:
                if latest["seq"] != latest["sent"]:
                    lv["dropped"] += 1          # 上一格還沒送出去就被新的蓋掉 = 網路跟不上
                latest["jpg"], latest["seq"] = jpg, latest["seq"] + 1
                fresh.set()
                lv["made"] += 1
                if c["ws_auto"] and lv["made"] >= 50:     # 每 2 秒檢查一次
                    ratio = lv["dropped"] / lv["made"]
                    if ratio > 0.2 and lv["i"] < len(levels) - 1:
                        lv["i"] += 1; lv["good"] = 0
                        logger.info(f"[gf-ws] {sid} 網路跟不上（丟 {ratio:.0%}），畫質降到 {levels[lv['i']][0]} px")
                    elif ratio < 0.02:
                        lv["good"] += 1
                        if lv["good"] >= 5 and lv["i"] > 0:      # 連續 10 秒都順，升一級
                            lv["i"] -= 1; lv["good"] = 0
                            logger.info(f"[gf-ws] {sid} 網路順了，畫質升到 {levels[lv['i']][0]} px")
                    else:
                        lv["good"] = 0
                    lv["made"] = lv["dropped"] = 0

    async def video_out():                      # 網路慢時只送最新的一格（舊的直接丟掉，不會越積越慢）
        sent = 0
        while not ws.closed:
            await fresh.wait()
            fresh.clear()
            if latest["seq"] != sent and latest["jpg"]:
                sent = latest["sent"] = latest["seq"]     # 已經拿去送（之後才來的新格若又被蓋掉，才算丟）
                await ws.send_bytes(VIDEO + latest["jpg"])

    async def audio_out():                      # 聲音一片都不丟：20 ms 一片 PCM
        while not ws.closed:
            f = await at.recv()
            await ws.send_bytes(AUDIO + bytes(f.planes[0])[: f.samples * 2])

    async def client_in():                      # 瀏覽器關掉就結束
        async for m in ws:
            if m.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    tasks = [asyncio.ensure_future(t()) for t in (video_in, video_out, audio_out, client_in)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception() and not ws.closed:
                logger.warning(f"[gf-ws] {sid} 中斷：{t.exception()!r}")
    finally:
        for t in tasks:
            t.cancel()
        for tr in (vt, at):
            try:
                tr.stop()
            except Exception:
                pass
        session_manager.remove_session(sid)
        if not ws.closed:
            await ws.close()
        logger.info(f"[gf-ws] 釋放 {sid}")
    return ws


def setup(app):
    app.router.add_get("/ws_media", ws_media)
