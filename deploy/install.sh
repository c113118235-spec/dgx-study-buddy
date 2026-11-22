#!/usr/bin/env bash
# 在新的 DGX Spark 上部署 DGX 學伴。跟 pack.sh 產生的檔案放在同一個資料夾執行：
#   bash ~/xiaoqing-bundle-<日期>/install.sh
# 會裝到 ~/xiaoqing（可用 XQ_HOME 改）：
#   ~/xiaoqing/livetalking   程式＋執行資料（deploy/.env 是設定檔）
#   ~/xiaoqing/models        模型
#   ~/xiaoqing/index-tts     IndexTTS 原始碼
# 重跑是安全的：已經載入的映像、已經解開的模型會跳過；.env、data/llm.json 不會被覆蓋。
set -euo pipefail

B=$(cd "$(dirname "$0")" && pwd)
VER=$(cat "$B/VERSION")
XQ=${XQ_HOME:-$HOME/xiaoqing}
LT=$XQ/livetalking; MODELS=$XQ/models; IDXDIR=$XQ/index-tts
ENVF=$LT/deploy/.env

say() { echo; echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

say "[0/7] 檢查環境（版本 $VER，裝到 $XQ）"
command -v docker >/dev/null || die "沒有 docker"
docker compose version >/dev/null 2>&1 || die "沒有 docker compose（v2）"
docker info 2>/dev/null | grep -qi nvidia || echo "!! docker 好像沒有 NVIDIA runtime，GPU 可能用不了"
nvidia-smi -L >/dev/null 2>&1 || echo "!! nvidia-smi 執行失敗，請確認顯卡驅動"
for c in xiaoqing xiaoqing-indextts xiaozhi-asr; do
  if docker ps --format '{{.Names}}' | grep -qx "$c"; then
    echo "!! 這台已經有容器 $c 在跑；會跟新的搶 port（8010/8007/8001），請先 docker rm -f $c"
  fi
done
if [ "${SKIP_CHECKSUM:-0}" != 1 ]; then
  say "[1/7] 檢查檔案完整（大檔要幾分鐘；SKIP_CHECKSUM=1 可跳過）"
  (cd "$B" && sha256sum -c SHA256SUMS) || die "檔案損壞或沒複製完整，請重新複製"
fi
need=$(du -sk "$B" | cut -f1); avail=$(df -k "$HOME" | awk 'NR==2{print $4}')
[ "$avail" -gt $((need * 2)) ] || echo "!! 家目錄剩餘空間可能不夠（需要約 $((need * 2 / 1048576)) GB）"

say "[2/7] 載入映像"
if docker image inspect "xiaoqing/app:$VER" >/dev/null 2>&1 && docker image inspect "xiaoqing/asr:$VER" >/dev/null 2>&1; then
  echo "   已經載入過，跳過"
else
  docker load -i "$B/images.tar"
fi

say "[3/7] 解開程式與執行資料 → $LT"
mkdir -p "$LT"
tar -xzf "$B/code.tar.gz" -C "$LT"
tar -xzf "$B/runtime.tar.gz" -C "$LT"
mkdir -p "$LT/data/custom" "$LT/data/voices"

say "[4/7] 解開模型 → $MODELS"
mkdir -p "$MODELS"
if [ -f "$MODELS/.done-$VER" ]; then echo "   已經解開過，跳過"
else tar -xf "$B/models.tar" -C "$MODELS" && touch "$MODELS/.done-$VER"; fi
tmp=$(mktemp -d); tar -xzf "$B/index-tts.tar.gz" -C "$tmp"
rm -rf "$IDXDIR"; mv "$tmp"/* "$IDXDIR"; rmdir "$tmp"

say "[5/7] 設定檔"
setkv() {  # setkv 檔案 名稱 值：有就改、沒有就加（用 python 寫，值裡有特殊符號也不會壞）
  python3 - "$1" "$2" "$3" <<'PY'
import sys
f, k, v = sys.argv[1:4]
lines = open(f, encoding="utf-8").read().splitlines()
for i, l in enumerate(lines):
    if l.startswith(k + "="):
        lines[i] = f"{k}={v}"; break
else:
    lines.append(f"{k}={v}")
open(f, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
}
[ -f "$ENVF" ] || cp "$LT/deploy/.env.example" "$ENVF"
chmod 600 "$ENVF"
setkv "$ENVF" XQ_VERSION "$VER"
setkv "$ENVF" XQ_MODELS "$MODELS"
setkv "$ENVF" XQ_INDEXTTS_SRC "$IDXDIR"
cp "$B/asr.env" "$LT/deploy/asr.env"
for k in IAI_API_KEY GF_ACCESS_CODE; do
  if ! grep -qE "^$k=.+" "$ENVF"; then
    if [ -t 0 ]; then
      read -rsp "   請輸入 $k（不會顯示在畫面上，直接 Enter 可之後再填）：" v; echo
      [ -n "$v" ] && setkv "$ENVF" "$k" "$v"
    fi
    grep -qE "^$k=.+" "$ENVF" || echo "!! $k 還沒填：之後請編輯 $ENVF"
  fi
done
for f in tts.json llm.json; do
  [ -f "$LT/data/$f" ] || cp "$LT/deploy/templates/$f.example" "$LT/data/$f"
done
chmod 600 "$LT/data/llm.json"
grep -q '"api_key": ""' "$LT/data/llm.json" && echo "!! data/llm.json 的實驗室 LLM 金鑰（api_key）還沒填；沒填會自動改用 iAI 的 LLM"
[ -f "$LT/data/turn.json" ] || echo "   （選用）校外 TURN：把 deploy/templates/turn.json.example 複製成 data/turn.json 並填帳密"
# 主程序 5 人＋3 個 worker 各 5 人
cat > "$LT/data/workers.json" <<'EOF'
{"local_max": 5, "workers": [{"url": "http://127.0.0.1:8021", "max": 5}, {"url": "http://127.0.0.1:8022", "max": 5}, {"url": "http://127.0.0.1:8023", "max": 5}]}
EOF

say "[6/7] 啟動容器"
(cd "$LT/deploy" && docker compose up -d)

# 照片學伴的小幫手（在主機上跑，用 LivePortrait 映像做待機影片）
LPENV="LIVETALKING_DIR=$LT LP_MODELS=$MODELS LP_IMAGE=xiaoqing/liveportrait:$VER"
pkill -f "tools/lp_worker.sh" 2>/dev/null || true
env $LPENV nohup "$LT/tools/lp_worker.sh" > "$LT/data/custom/lp_worker.log" 2>&1 &
CRON="@reboot sleep 60 && env $LPENV nohup $LT/tools/lp_worker.sh > $LT/data/custom/lp_worker.log 2>&1 &"
(crontab -l 2>/dev/null | grep -v 'tools/lp_worker.sh'; echo "$CRON") | crontab -
echo "   照片學伴小幫手已啟動，並設定開機自動執行"

say "[7/7] 等服務起來（第一次要 1～3 分鐘）"
ok() { curl -s -o /dev/null -m 3 -w '%{http_code}' "$1" | grep -qE '^[1-4]'; }
for name_url in "主程序 http://127.0.0.1:8010/xiaoqing.html" "worker1 http://127.0.0.1:8021/xiaoqing.html" \
                "語音辨識 http://127.0.0.1:8001/" "本機語音 http://127.0.0.1:8007/"; do
  n=${name_url%% *}; u=${name_url#* }
  for _ in $(seq 1 60); do ok "$u" && break; sleep 3; done
  if ok "$u"; then echo "   ✓ $n"; else echo "   ✗ $n 還沒回應：docker logs 看看（${u}）"; fi
done
URL=$(docker logs xiaoqing-tunnel 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true)
IP=$(hostname -I | awk '{print $1}')

echo
echo "完成！"
echo "  區網：  http://$IP:8010/xiaoqing.html?mode=chat"
echo "  公開：  ${URL:-（還沒拿到，稍後 docker logs xiaoqing-tunnel | grep trycloudflare）}"
echo "  狀態：  cd $LT/deploy && docker compose ps"
echo "  人數：  curl -s 'http://127.0.0.1:8010/api/admin/sessions?all=1'"
