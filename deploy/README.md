# DGX 學伴：Docker 一鍵部署

把已經跑得起來的那台 DGX（spark-ad05）打包成一個資料夾，帶到其他 DGX Spark 執行一個指令就能部署。

## 包含哪些容器

| 容器 | 用途 | Port |
|---|---|---|
| `xiaoqing` | 主程序：網頁、門禁、分配連線（前 5 人） | 8010 |
| `xiaoqing-w1`～`w3` | worker，各 5 人（合計 20 人） | 8021–8023 |
| `xiaoqing-indextts` | 本機語音 IndexTTS-2.5 | 8007 |
| `xiaoqing-asr` | 語音辨識 Whisper | 8001 |
| `xiaoqing-tunnel` | 公開 https 網址（Cloudflare 臨時網址） | — |

另外在主機上跑一個「照片學伴小幫手」（`tools/lp_worker.sh`），用 LivePortrait 映像做待機影片；install.sh 會幫你啟動並設定開機自動執行。

## 步驟 1：在 spark-ad05 打包

```bash
cd ~/livetalking && git pull origin dgx2
bash deploy/pack.sh --dry-run      # 先檢查、看大小（約 60 GB，要確認空間夠）
bash deploy/pack.sh                # 打包到 ~/xiaoqing-bundle-<日期>
```

打包內容：

| 檔案 | 內容 |
|---|---|
| `images.tar` | 4 個映像（app、indextts、asr、liveportrait）＋ cloudflared；共用底層只存一份 |
| `code.tar.gz` | 程式碼（git 最新 commit） |
| `runtime.tar.gz` | wav2lip 權重、動作影格、聲音庫（不含使用者自己錄的聲音） |
| `models.tar` | hub、indextts25、liveportrait、torch、refs |
| `index-tts.tar.gz` | IndexTTS 原始碼 |
| `asr.env` | 語音辨識的模型名稱與語言 |
| `install.sh`、`VERSION`、`SHA256SUMS` | 安裝程式、版本、檢查碼 |

**不會打包**：金鑰（`.env`、`data/llm.json`、`data/turn.json`、`data/api/`）、使用者資料（`data/custom/` 照片學伴、`data/voices/u_*`）、log。

## 步驟 2：複製到別台 DGX

```bash
rsync -a --progress ~/xiaoqing-bundle-<日期>/ nkust@<那台IP>:~/xiaoqing-bundle-<日期>/
```

## 步驟 3：在那台安裝

```bash
bash ~/xiaoqing-bundle-<日期>/install.sh
```

會依序：檢查環境與檔案 → 載入映像 → 解開程式、模型 → 建立設定檔（會問 `IAI_API_KEY`、`GF_ACCESS_CODE`，輸入時不顯示）→ `docker compose up -d` → 啟動小幫手 → 檢查各服務、印出網址。

裝好的位置（可用 `XQ_HOME=/別的/路徑` 改）：

```
~/xiaoqing/livetalking          程式；設定檔在 deploy/.env
~/xiaoqing/models               模型
~/xiaoqing/index-tts            IndexTTS 原始碼
```

## 裝好之後

| 要做什麼 | 指令 |
|---|---|
| 看狀態 | `cd ~/xiaoqing/livetalking/deploy && docker compose ps` |
| 看公開網址 | `docker logs xiaoqing-tunnel 2>&1 \| grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' \| tail -1` |
| 看人數 | `curl -s 'http://127.0.0.1:8010/api/admin/sessions?all=1'` |
| 全部重開 | `docker compose restart` |
| 全部關掉 | `docker compose down` |
| 改了 .env 之後 | `docker compose up -d`（會重建有變動的容器） |

## 要自己填的設定

| 檔案 | 必填？ | 內容 |
|---|---|---|
| `deploy/.env` | 必填 | `IAI_API_KEY`、`GF_ACCESS_CODE`（範本：`deploy/.env.example`） |
| `data/llm.json` | 建議 | 實驗室 LLM 的 `api_key`；沒填會自動改用 iAI 的 LLM |
| `data/turn.json` | 選用 | 校外 TURN 帳密（範本：`deploy/templates/turn.json.example`）；沒有也能用 https 備用模式 |
| `data/tts.json` | 不用改 | 開始畫面列出的 iAI 聲音（預設 9D、36D） |

這些檔案權限都要是 600，不要 commit，也不要貼到聊天或文件裡。

## 注意

- **在 spark-ad05 本機**：原本的容器（`xiaoqing`、`xiaozhi-asr`…）會跟 compose 搶 port，要改用 compose 前先 `docker rm -f` 舊容器。
- **公開網址每台不同**，而且 tunnel 重開就會換；每台要各自設通關碼。
- **BreezyVoice、CosyVoice 沒有打包**：語音用 iAI 和 IndexTTS 就夠；它們只是更後面的備援。
