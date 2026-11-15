"""測實驗室專用平台的語音模型：哪個模型、哪些聲音（9D、36D…）能用，一句要幾秒，同時多句會不會排隊。

在 xiaoqing 容器裡跑（網址和金鑰從 data/llm.json 讀，不會印出金鑰）：
  docker exec xiaoqing python -u /app/tools/tts_probe.py
  docker exec xiaoqing python -u /app/tools/tts_probe.py --models fish-speech,Speaker --voices 9D,36D,bjmkpo90caqop
  docker exec xiaoqing python -u /app/tools/tts_probe.py --models Speaker --voices 9D --concurrent 1,5,10
聽聽看：生成的語音存在 data/tts_probe/（模型_聲音.wav），可以下載回來聽是不是同一個人的聲音。
能用的話，在 data/tts.json 的 "iai" 裡加 "lab": {"model": "<模型>"}，小晴就會先用實驗室的語音，失敗才用學校 iAI。
"""
import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT = "嗨，今天過得好嗎"


def secs(data):
    try:
        import soundfile as sf
        a, sr = sf.read(BytesIO(data))
        return len(a) / sr
    except Exception:
        return None


def call(url, key, model, voice, text=TEXT):
    t0 = time.time()
    try:
        r = requests.post(url, timeout=60, json={"model": model, "input": text, "voice": voice, "response_format": "wav"},
                          headers={"Authorization": f"Bearer {key}", "x-litellm-api-key": f"Bearer {key}"})
        dt = time.time() - t0
        if r.status_code != 200 or len(r.content) < 200:
            return dt, None, f"{r.status_code} {r.text[:160]}"
        return dt, r.content, ""
    except Exception as e:
        return time.time() - t0, None, repr(e)[:160]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="Speaker,fish-speech")
    ap.add_argument("--voices", default="9D,36D,bjmkpo90caqop")
    ap.add_argument("--concurrent", default="1,5,10", help="對第一個能用的組合測同時幾句")
    a = ap.parse_args()
    try:
        with open(os.path.join(ROOT, "data", "llm.json"), encoding="utf-8") as f:
            c = json.load(f)
    except OSError:
        sys.exit("找不到 data/llm.json（實驗室平台的網址和金鑰）")
    key = c.get("api_key") or os.getenv(str(c.get("key_env") or ""), "")
    url = c.get("base_url", "").rstrip("/") + "/audio/speech"
    if not key:
        sys.exit("data/llm.json 沒有金鑰")
    out = os.path.join(ROOT, "data", "tts_probe")
    os.makedirs(out, exist_ok=True)
    print(f"網址：{url}")
    works = []
    for m in [x.strip() for x in a.models.split(",") if x.strip()]:
        for v in [x.strip() for x in a.voices.split(",") if x.strip()]:
            res = [call(url, key, m, v) for _ in range(2)]          # 兩次：看速度、也可以聽聲音有沒有固定
            oks = [(dt, d) for dt, d, _ in res if d]
            if not oks:
                print(f"  {m:14s} {v:16s} 不能用：{res[0][2]}")
                continue
            for i, (_, d) in enumerate(oks):
                with open(os.path.join(out, f"{m}_{v}_{i + 1}.wav"), "wb") as f:
                    f.write(d)
            s = secs(oks[0][1])
            print(f"  {m:14s} {v:16s} 可以：{' / '.join(f'{dt:.2f}s' for dt, _ in oks)}"
                  + (f"（語音長 {s:.1f} 秒）" if s else ""), flush=True)
            works.append((m, v))
    if not works:
        print("\n沒有能用的組合。把上面的錯誤訊息貼給我。")
        return
    m, v = works[0]
    print(f"\n同時多句（{m} / {v}）：")
    base = None
    for n in [int(x) for x in a.concurrent.split(",") if x.strip()]:
        t0 = time.time()
        with ThreadPoolExecutor(n) as ex:
            res = list(ex.map(lambda _: call(url, key, m, v), range(n)))
        ts = [dt for dt, d, _ in res if d]
        if not ts:
            print(f"  同時 {n:2d} 句：全部失敗 {res[0][2]}")
            continue
        med = statistics.median(ts)
        base = base or med
        print(f"  同時 {n:2d} 句：中位數 {med:.2f}s、最慢 {max(ts):.2f}s、全部做完 {time.time() - t0:.2f}s（{med / base:.1f} 倍）"
              + (f"｜失敗 {n - len(ts)}" if len(ts) < n else ""), flush=True)
        time.sleep(1)
    print(f"\n語音檔在 data/tts_probe/。要讓小晴用：data/tts.json 的 \"iai\" 加上 \"lab\": {{\"model\": \"{m}\"}}")


if __name__ == "__main__":
    main()
