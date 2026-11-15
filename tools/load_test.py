"""壓力測試：模擬 N 個人同時連線、其中 K 個人同時讓小晴說話，量出「開口要等多久」和「畫面有沒有變卡」。
不用準備 N 台裝置。

在 xiaoqing 容器裡跑（用 127.0.0.1 直連，不經過公開網址、不用 TURN）：
  docker exec xiaoqing python /app/tools/load_test.py --n 15 --speak 5
  docker exec xiaoqing python /app/tools/load_test.py --n 15 --speak 15 --rounds 2
參數：
  --n       同時連線幾人（要 ≤ config.yaml 的 max_session）
  --speak   每一輪同時說話幾人
  --rounds  幾輪
  --voice   用哪個聲音（female／male／iai_9D…）
  --voices  聲音混用：逗號分隔，依序輪流分給每條連線（例：iai_9D,iai_36D,female → 1/3 本機語音），結果會依聲音分開統計
  --avatar  人物（預設內建小晴；可填 custom_xxx）
  --procs   測試程式拆成幾個程序（預設 4）；人多時一個程序解不完 20 條畫面，fps 會被低估
  --text    固定說這句（預設三句 24 字左右的長句；聊天時第一小段通常只有 8～12 字）
量什麼：
  開口延遲：送出文字 → 伺服器開始說話（含 TTS 排隊、對嘴）的秒數
  影像流量：每條連線每秒收到多少 KB（掉很多 = 伺服器送不出畫面）
  伺服器每秒格數另外看：docker logs xiaoqing --since 3m 2>&1 | grep "actual avg final fps"
"""
import argparse
import asyncio
import multiprocessing as mp
import statistics
import time

import aiohttp

SENTENCES = ["大家好，我是小晴，很高興認識你，今天一起加油吧。",
             "這題可以先把大問題拆成三個小步驟，我們一步一步來。",
             "讀了這麼久，要不要先起來喝口水、伸個懶腰再繼續？"]


class Client:
    def __init__(self, base, idx, voice, avatar):
        self.base, self.idx, self.voice, self.avatar = base, idx, voice, avatar
        self.pc = None
        self.sid = None
        self.track_tasks = []
        self.frames = 0                          # 收到（解出）的影像格數 → 算每條連線的 fps

    async def connect(self, http):
        from aiortc import RTCPeerConnection, RTCSessionDescription
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver("video", direction="recvonly")
        self.pc.addTransceiver("audio", direction="recvonly")

        @self.pc.on("track")
        def on_track(track):                     # 一定要把封包收走，不然伺服器那邊會塞住；不解碼，省 CPU
            async def drain():
                try:
                    while True:
                        await track.recv()
                        if track.kind == "video":
                            self.frames += 1
                except Exception:
                    pass
            self.track_tasks.append(asyncio.ensure_future(drain()))

        await self.pc.setLocalDescription(await self.pc.createOffer())
        body = {"sdp": self.pc.localDescription.sdp, "type": "offer", "voice": self.voice}
        if self.avatar:
            body["avatar"] = self.avatar
        r = await http.post(self.base + "/offer", json=body)
        a = await r.json(content_type=None)
        if "sdp" not in a:
            raise RuntimeError(a.get("msg") or repr(a))
        self.sid = a["sessionid"]
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=a["sdp"], type=a["type"]))

    async def bytes_video(self):
        return self.frames                       # （名字沿用）現在回傳收到的影像格數

    async def speaking(self, http):
        r = await http.post(self.base + "/is_speaking", json={"sessionid": self.sid})
        return bool((await r.json(content_type=None)).get("data"))

    async def say(self, http, text):
        """送出 → 等到開始說話，回傳秒數（逾時回傳 None）"""
        t0 = time.time()
        await http.post(self.base + "/human", json={"sessionid": self.sid, "type": "echo", "text": text, "interrupt": True})
        while time.time() - t0 < 40:
            if await self.speaking(http):
                return time.time() - t0
            await asyncio.sleep(0.1)
        return None

    async def wait_quiet(self, http, limit=40):
        t0 = time.time()
        while time.time() - t0 < limit and await self.speaking(http):
            await asyncio.sleep(0.3)

    async def close(self, http):
        try:
            if self.sid:
                await http.post(self.base + "/bye", json={"sessionid": self.sid})
            await self.pc.close()
        except Exception:
            pass
        for t in self.track_tasks:
            t.cancel()


def _args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8010")
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--speak", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--voice", default="female")
    ap.add_argument("--voices", default="", help="聲音混用，例：iai_9D,female")
    ap.add_argument("--avatar", default="")
    ap.add_argument("--text", default="", help="每輪都說這句（例：--text 嗨，今天過得好嗎 模擬聊天時第一小段）")
    ap.add_argument("--procs", type=int, default=4, help="測試程式拆成幾個程序（解畫面分攤到多個 CPU）")
    a = ap.parse_args()
    a.speak = min(a.speak, a.n)
    a.procs = max(1, min(a.procs, a.n))
    a.voices = [v.strip() for v in a.voices.split(",") if v.strip()] or [a.voice]
    return a


async def part_main(a, part, barrier, q):
    """一個子程序：負責編號 i % procs == part 的連線；每一步跟其他子程序對齊時間"""
    loop = asyncio.get_running_loop()

    async def sync():
        await loop.run_in_executor(None, barrier.wait)

    mine = [Client(a.url.rstrip("/"), i, a.voices[i % len(a.voices)], a.avatar) for i in range(a.n) if i % a.procs == part]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        await asyncio.sleep(part * 0.3)                # 錯開，不要同一瞬間一起連
        ok = []
        for c in mine:
            for attempt in range(3):                 # 上一輪測試的連線可能還在釋放，等一下再試
                try:
                    await c.connect(http)
                    ok.append(c)
                    break
                except Exception as e:
                    err = repr(e)
                    try:
                        await c.pc.close()
                    except Exception:
                        pass
                    await asyncio.sleep(5)
            else:
                q.put(("err", f"第 {c.idx + 1} 條連不上：{err}"))
        q.put(("ok", len(ok)))
        await sync()
        await asyncio.sleep(5)
        b0 = [c.frames for c in ok]
        await asyncio.sleep(5)
        q.put(("idle", [(c.frames - x) / 5 for c, x in zip(ok, b0)]))
        for rd in range(a.rounds):
            await sync()
            want = {(rd * a.speak + k) % a.n for k in range(a.speak)}
            group = [c for c in ok if c.idx in want]
            text = a.text or SENTENCES[rd % len(SENTENCES)]
            b0 = [c.frames for c in ok]
            t0 = time.time()
            lats = await asyncio.gather(*[c.say(http, text) for c in group])
            await asyncio.gather(*[c.wait_quiet(http) for c in group])
            await sync()                              # 等大家都講完，才算這段時間的 fps
            dt = time.time() - t0
            q.put(("round", rd, [(c.voice, x) for c, x in zip(group, lats)], [(c.frames - x) / dt for c, x in zip(ok, b0)]))
            await asyncio.sleep(2)
        await asyncio.gather(*[c.close(http) for c in ok])


def _run_part(a, part, barrier, q):
    try:
        asyncio.run(part_main(a, part, barrier, q))
    except Exception as e:
        q.put(("err", f"子程序 {part} 出錯：{e!r}"))
        barrier.abort()
    finally:
        q.put(("done", part))


def main():
    a = _args()
    ctx = mp.get_context("fork")
    barrier, q = ctx.Barrier(a.procs), ctx.Queue()
    print(f"1. 建立 {a.n} 條連線（測試程式分 {a.procs} 個程序）…", flush=True)
    ps = [ctx.Process(target=_run_part, args=(a, k, barrier, q)) for k in range(a.procs)]
    for p in ps:
        p.start()
    done, okn, idle, rounds = 0, 0, [], {}
    lat_all, by_voice = [], {v: [] for v in a.voices}
    while done < a.procs:
        m = q.get()
        kind = m[0]
        if kind == "done":
            done += 1
        elif kind == "err":
            print("   " + m[1], flush=True)
        elif kind == "ok":
            okn += m[1]
        elif kind == "idle":
            idle += m[1]
            if len(idle) == okn:
                print(f"   成功 {okn} 條")
                print(f"2. 待機時每條連線收到：平均 {statistics.mean(idle):.1f} fps（最低 {min(idle):.1f}）", flush=True)
        elif kind == "round":
            rd, res, fps = m[1], m[2], m[3]
            r = rounds.setdefault(rd, {"res": [], "fps": [], "parts": 0})
            r["res"] += res
            r["fps"] += fps
            r["parts"] += 1
            if r["parts"] == a.procs:
                got = [x for _, x in r["res"] if x is not None]
                lat_all += got
                for v, x in r["res"]:
                    if x is not None:
                        by_voice[v].append(x)
                miss = len(r["res"]) - len(got)
                lat = f"開口延遲 中位數 {statistics.median(got):.1f}s、最慢 {max(got):.1f}s" if got else "沒人開口"
                print(f"3.{rd + 1} 同時 {len(r['res'])} 人說話：{lat}" + (f"、{miss} 人 40 秒內沒開口" if miss else "")
                      + f"｜這段時間每條連線收到 平均 {statistics.mean(r['fps']):.1f} fps（最低 {min(r['fps']):.1f}）", flush=True)
    for p in ps:
        p.join()
    if lat_all:
        print(f"\n總結：{okn} 人在線、每次 {a.speak} 人同時說話 → 開口延遲中位數 {statistics.median(lat_all):.1f}s、最慢 {max(lat_all):.1f}s")
        if len(a.voices) > 1:
            for v, xs in by_voice.items():
                if xs:
                    print(f"  {v:10s}：{len(xs)} 次，中位數 {statistics.median(xs):.1f}s、最慢 {max(xs):.1f}s")
    print("伺服器每秒格數：for c in xiaoqing xiaoqing-w1 xiaoqing-w2 xiaoqing-w3; do docker logs $c --since 5m 2>&1 | grep 'actual avg final fps' | tail -3; done")


if __name__ == "__main__":
    main()
