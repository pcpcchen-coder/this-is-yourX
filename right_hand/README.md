# right_hand — 這是你的右手

Amazing Hand 右手版（Seeed Studio 套件，4 指、8-DOF、8 顆 Feetech SCS0009）的組裝、bring-up 與之後的自我模型資料，都放在這個資料夾。它是 `this-is-yourX` 的第一個實體 embodiment；選型與視覺計畫見 [`docs/HISTORY.md`](../docs/HISTORY.md)。

這個資料夾只管右手。之後加入的其他 X（例如雙目視覺鏡頭）各自開資料夾。

## 目前狀態（2026-10-07）

硬體已到貨。8 顆伺服機都通過單軸小幅轉動測試並設好 ID 1–8。四指已校正並裝上手掌，全手可通電輪流開合，組裝表全部完成。USB 黑屏問題以中間加 USB hub 緩解，成因尚未量測（見安全邊界）。

| 項目 | 狀態 |
|---|---|
| 到貨清點 | 五金、線材、驅動板、伺服機已拍照核對；結構件與長度治具尚未核對 |
| 變壓器 | 標示 5V 3A（符合 SCS0009 的 4.0–7.4V） |
| 驅動板供電 | VCC–GND 量得 5V（使用者回報，2026-10-04） |
| Mac 環境與序列埠 | 已建立：Python 3.12.15、rustypot 1.10.0、`/dev/cu.usbmodem5B790827031` |
| 驅動板模式與通訊 | USB 模式可用；`scan` 回報 `ID 1  SCS0009`（2026-10-04） |
| 8 顆伺服機單顆測試、設 ID | 8 / 8：ID 1–8 測試通過並完成設定（4.7–5.1 V、22–23 °C）；ID 2 斷電後仍保存，其餘未個別驗證斷電保存 |
| USB 接地 | **已緩解，成因未量測**：驅動板改經 USB hub 接 Mac mini 後，螢幕不再受影響（使用者回報，2026-10-07） |
| 零件前處理 | 完成：舵盤、樞軸孔、毛邊、長度治具（樞軸孔手感待組裝時驗證） |
| 手指組裝 | 四指機構完成；合照核對奇數 ID 都在右側（偶數標籤未拍到），舵盤待校正時裝上 |
| 校正 | 四指完成，8 個中位修正都是 0（目視判定） |
| 手掌組裝 | 完成：8 顆已串接，全手輪流開合一次，四指都到閉合 90°（靜止讀值比目標少 0.4–2.1°） |
| 外殼 | 記為完成（使用者口頭回報，無照片） |

組裝表進度 75 / 75，逐步紀錄見 [`BUILD_LOG.md`](BUILD_LOG.md)。

**本資料夾目前沒有任何 real-hardware validation。** `servo_tool.py` 的 `scan`、`test`、`setid`、`center`、`finger`、`hand` 已在實體伺服機上跑過並完成動作，但那是 bring-up 觀察，沒有 experiment ID；`diag` 仍只在假匯流排上測過。

## 目錄

```text
right_hand/
├── README.md
├── BUILD_LOG.md                    # 日期化建置紀錄
├── requirements.txt
├── THIRD_PARTY_NOTICES.md          # 官方手冊圖片的來源與授權
├── docs/
│   ├── assembly_guide.md           # 組裝順序、Seeed 套件差異、注意事項、來源
│   ├── arrival_inspection.md       # 到貨清點結果
│   └── assembly_checklist/
│       ├── index.html              # 75 項可勾選檢查表，瀏覽器直接開
│       ├── progress.json           # 進度快照
│       └── img/                    # 官方組裝手冊對應頁
├── photos/2026-10-02_arrival/      # 到貨照片（已縮圖、已移除 EXIF）
├── tools/
│   ├── servo_tool.py               # 人工操作的 bring-up 工具（單顆測試、設 ID、單指校正）
│   ├── first_scan.sh               # 第一次上電：建環境、找埠、掃描（只讀）
│   ├── assign_id.sh                # 單顆：掃描 → 轉動測試 → 改 ID → 再掃描
│   └── finger_cal.sh               # 單指校正（center、finger）與全手測試（hand）
└── tests/
    ├── fake_scs_bus.py             # 假的 SCS 匯流排
    └── test_servo_tool.py
```

## 快速開始

需要 Python 3.10 以上（macOS 內建的 3.9 只裝得到舊版 rustypot）。

第一次上電最快的做法是 `bash tools/first_scan.sh`：它會找 Python 3.10 以上，找不到就用 [uv](https://docs.astral.sh/uv/) 下載 3.12，然後建環境、找序列埠、做一次只讀掃描。

```bash
cd right_hand
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python tools/servo_tool.py ports              # 找驅動板的序列埠
python tools/servo_tool.py scan  <PORT>       # 只讀，列出回應的 ID
python tools/servo_tool.py diag  <PORT>       # 掃不到時用：送 PING 並印出原始位元組
python tools/servo_tool.py test  <PORT> <ID>  # 單顆、無負載、±20° 低速擺動
python tools/servo_tool.py setid <PORT> 1 3   # 匯流排上只接一顆時改 ID
python tools/servo_tool.py center <PORT> 1 2  # 一根手指的兩顆回中位並保持扭力（裝舵盤用）
python tools/servo_tool.py finger <PORT> 1 2 [中位A 中位B]   # 分段開合一次（微調中位用）
python tools/servo_tool.py hand <PORT> [中位1 … 中位8]      # 8 顆全接，四根手指輪流開合一次
```

組裝檢查表：用瀏覽器開 `docs/assembly_checklist/index.html`。離線開啟時，勾選進度只存在該瀏覽器。

測試（不需要硬體）：

```bash
pip install pytest
pytest right_hand/tests -q      # 2026-10-07：49 passed
```

## 安全邊界

- `servo_tool.py` 是給人在工作台上用的 bring-up 工具，不在 AI 控制路徑上。生成式模型不直接下馬達命令；之後的動作一律經 versioned skill 與 Safety Gateway（見根目錄 `AGENTS.md`）。
- `test` 動作前會檢查：匯流排上只有一顆、電壓在 4.0–7.4V、溫度不超過 60°C；任何一項不符就不開扭力。離開前一定關扭力，包含例外與 Ctrl-C。
- `center`、`finger` 只接受同一根手指的一對 ID（1 2、3 4、5 6、7 8），匯流排上必須恰好是這兩顆，電壓、溫度條件同上；中位修正限 ±30°。`finger` 以較低速度分段開合，任一步 3 秒內沒到位（誤差 8° 以上）就停止並關扭力。
- `hand` 要求匯流排上恰好是 ID 1–8，一次只動一根手指；任一步沒到位或電壓低於 4.0V 就停止並關全部扭力。
- 斷電路徑是變壓器。全手測試時變壓器接在有開關、伸手可及的延長線上，由操作者按著。這是人工斷電，不是獨立的 E-stop 電路；交給 skill 與 Safety Gateway 控制之前要補上。
- 插拔伺服機或線材前先斷電。
- **USB 黑屏已用 hub 緩解，成因未量測**：變壓器供電時，USB 線頭金屬殼一碰到驅動板 USB 外殼，Mac mini 螢幕就會黑一下（2026-10-04），兩種接線順序都發生過。2026-10-07 使用者改成驅動板經 USB hub 接 Mac mini，螢幕不再受影響。兩個外殼之間的電位差沒有量過，所以變壓器的漏電大小與絕緣狀況仍未知；驅動板一律經 hub 連接，不要直插 Mac mini。

## 接下來

1. 補拍裝殼後的照片並留一份裝殼後的 `hand` 輸出。
2. （選做）量測 USB 外殼之間的電位差，確認變壓器漏電在正常範圍。
3. 補上獨立的 E-stop。
4. 建立 8-DOF semantic component IDs 與 manifest，把 bring-up 工具收斂成 hardware adapter。
5. 雙目視覺鏡頭已採購（感光元件 AR0144），用來讓系統看得到這隻右手。模組型號等細節確認後另開資料夾並補 ADR，見 [`docs/HISTORY.md`](../docs/HISTORY.md)。
