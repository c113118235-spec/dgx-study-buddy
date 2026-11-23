#!/usr/bin/env bash
# 在「已經跑得起來的那台 DGX」（spark-ad05）上打包，產生一個資料夾，帶到別台 DGX 跑 install.sh 就能部署。
#   bash ~/livetalking/deploy/pack.sh                # 打包到 ~/xiaoqing-bundle-<日期>
#   bash ~/livetalking/deploy/pack.sh --dry-run      # 只檢查、估大小，不打包
#   SKIP_IMAGES=1 bash ~/livetalking/deploy/pack.sh  # 不存映像（映像改用 docker save | ssh 直接傳到另一台，省 30 多 GB 硬碟和時間）
# 不會打包：金鑰（.env、data/llm.json、data/turn.json、data/api/）、使用者資料（照片學伴、自己錄的聲音）、log
# 可用環境變數改來源：MODELS、IDX、OUT、VER、MODEL_DIRS
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
LT=$(cd "$(dirname "$0")/.." && pwd)
VER=${VER:-$(date +%Y%m%d)}
MODELS=${MODELS:-$HOME/xiaozhi-speech/models}
IDX=${IDX:-$HOME/index-tts}
OUT=${OUT:-$HOME/xiaoqing-bundle-$VER}
# 只帶執行時用得到的模型：hub（Whisper／IndexTTS 用的 HF 快取）、indextts25、liveportrait、torch、refs
MODEL_DIRS=${MODEL_DIRS:-"hub indextts25 liveportrait torch refs"}
# 來源映像 → 部署用的名稱
SRC_IMAGES=(livetalking-spark xiaoqing-indextts xiaozhi-asr xiaozhi-liveportrait)
DST_NAMES=(app indextts asr liveportrait)
ASR_CONTAINER=${ASR_CONTAINER:-xiaozhi-asr}

say() { echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

say "版本 $VER；程式 $LT；模型 $MODELS；IndexTTS $IDX；輸出 $OUT"

# 1. 檢查來源
for i in "${!SRC_IMAGES[@]}"; do
  docker image inspect "${SRC_IMAGES[$i]}" >/dev/null 2>&1 || die "找不到映像 ${SRC_IMAGES[$i]}"
done
docker image inspect cloudflare/cloudflared:latest >/dev/null 2>&1 || docker pull cloudflare/cloudflared:latest
for d in $MODEL_DIRS; do [ -d "$MODELS/$d" ] || die "找不到模型資料夾 $MODELS/$d"; done
[ -d "$IDX/indextts" ] || die "找不到 IndexTTS 原始碼 $IDX"
[ -f "$LT/models/wav2lip.pth" ] || die "找不到 $LT/models/wav2lip.pth"
if [ -n "$(git -C "$LT" status --porcelain --untracked-files=no)" ]; then
  echo "!! 注意：程式有沒 commit 的修改，打包的是 git 最新 commit（HEAD），修改不會帶走："
  git -C "$LT" status --short --untracked-files=no | sed 's/^/     /'
fi

# 2. 估大小
say "模型大小："
(cd "$MODELS" && du -sh $MODEL_DIRS)
du -sh "$LT/models" "$IDX" 2>/dev/null
say "映像（彼此共用底層，實際打包會比加總小很多）："
for s in "${SRC_IMAGES[@]}"; do docker image inspect "$s" --format "   $s {{.Size}}" | awk '{printf "   %s %.1f GB\n", $1, $2/1e9}'; done
say "輸出位置剩餘空間：$(df -h "$(dirname "$OUT")" | awk 'NR==2{print $4}')（大約需要 60 GB）"
[ "$DRY" = 1 ] && { say "--dry-run：只檢查，不打包"; exit 0; }

mkdir -p "$OUT"

# 3. 映像：改成部署用名稱後一次存（共用的底層只存一份）
TAGS=()
for i in "${!SRC_IMAGES[@]}"; do
  t="xiaoqing/${DST_NAMES[$i]}:$VER"; docker tag "${SRC_IMAGES[$i]}" "$t"; TAGS+=("$t")
done
TAGS+=(cloudflare/cloudflared:latest)
if [ "${SKIP_IMAGES:-0}" = 1 ]; then
  rm -f "$OUT/images.tar"
  say "SKIP_IMAGES=1：不存映像。直接傳到另一台："
  echo "   docker save ${TAGS[*]} | ssh nkust@<那台IP> docker load"
else
  say "存映像（幾十 GB，要等一陣子）：${TAGS[*]}"
  docker save -o "$OUT/images.tar" "${TAGS[@]}"
fi
echo "${TAGS[*]}" > "$OUT/IMAGES"

# 4. 程式（git 最新 commit）
say "打包程式碼"
git -C "$LT" archive --format=tar.gz -o "$OUT/code.tar.gz" HEAD

# 5. 不在 git 裡、但執行要用的檔案（不含金鑰與使用者資料）
say "打包執行用資料（wav2lip 權重、動作影格、聲音）"
RUNTIME=(models)
for p in data/actions data/lp_out data/voices data/tts.json data/av.json; do [ -e "$LT/$p" ] && RUNTIME+=("$p"); done
tar -C "$LT" -czf "$OUT/runtime.tar.gz" \
  --exclude='data/voices/u_*' --exclude='*.log' --exclude='__pycache__' "${RUNTIME[@]}"

# 6. 模型與 IndexTTS 原始碼
say "打包模型：$MODEL_DIRS"
tar -C "$MODELS" -cf "$OUT/models.tar" $MODEL_DIRS
tar -C "$(dirname "$IDX")" -czf "$OUT/index-tts.tar.gz" --exclude='.git' "$(basename "$IDX")"

# 7. 語音辨識的設定（只帶模型名稱和語言，不帶金鑰）
: > "$OUT/asr.env"
if docker inspect "$ASR_CONTAINER" >/dev/null 2>&1; then
  docker inspect "$ASR_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep -E '^(ASR_MODEL|ASR_LANGUAGE)=' > "$OUT/asr.env" || true
fi
echo "   語音辨識設定：$(tr '\n' ' ' < "$OUT/asr.env")"

# 8. 安裝程式、版本、檢查碼
cp "$LT/deploy/install.sh" "$OUT/install.sh"; chmod +x "$OUT/install.sh"
echo "$VER" > "$OUT/VERSION"
git -C "$LT" rev-parse --short HEAD > "$OUT/COMMIT"
(cd "$OUT" && sha256sum $(ls images.tar code.tar.gz runtime.tar.gz models.tar index-tts.tar.gz 2>/dev/null) > SHA256SUMS)

say "完成：$OUT（$(du -sh "$OUT" | cut -f1)）"
echo "   複製到別台 DGX：rsync -a --progress $OUT/ nkust@<那台IP>:~/$(basename "$OUT")/"
echo "   在那台執行：   bash ~/$(basename "$OUT")/install.sh"
