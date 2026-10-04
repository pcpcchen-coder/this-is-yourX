# right_hand — 這是你的右手

Amazing Hand 右手版（Seeed Studio 套件，4 指、8-DOF、8 顆 Feetech SCS0009）的組裝、bring-up 與之後的自我模型資料，都放在這個資料夾。它是 `this-is-yourX` 的第一個實體 embodiment；選型與視覺計畫見 [`docs/HISTORY.md`](../docs/HISTORY.md)。

這個資料夾只管右手。其他 X 各自開資料夾，例如雙目相機在 [`stereo_camera/`](../stereo_camera/README.md)。

## 目前狀態（2026-10-04）

硬體已到貨。8 顆伺服機都通過單軸小幅轉動測試並設好 ID 1–8。USB 接地電位差的問題尚未排除（見安全邊界）。

| 項目 | 狀態 |
|---|---|
| 到貨清點 | 五金、線材、驅動板、伺服機已拍照核對；結構件與長度治具尚未核對 |
| 變壓器 | 標示 5V 3A（符合 SCS0009 的 4.0–7.4V） |
| 驅動板供電 | VCC–GND 量得 5V（使用者回報，2026-10-04） |
| Mac 環境與序列埠 | 已建立：Python 3.12.15、rustypot 1.10.0、`/dev/cu.usbmodem5B790827031` |
| 驅動板模式與通訊 | USB 模式可用；`scan` 回報 `ID 1  SCS0009`（2026-10-04） |
| 8 顆伺服機單顆測試、設 ID | 8 / 8：ID 1–8 測試通過並完成設定（4.7–5.1 V、22–23 °C）；ID 2 斷電後仍保存，其餘未個別驗證斷電保存 |
| USB 接地 | **未解決**：USB 線頭金屬殼碰到驅動板 USB 外殼，Mac mini 螢幕就會黑一下；原因待量測 |
| 零件前處理 | 完成：舵盤、樞軸孔、毛邊、長度治具（樞軸孔手感待組裝時驗證） |
| 手指組裝 | 食指進行中：第 1 支球頭連桿已對治具 |
| 校正、手掌組裝 | 未開始 |

組裝表進度 11 / 75，逐步紀錄見 [`BUILD_LOG.md`](BUILD_LOG.md)。

**本資料夾目前沒有任何 real-hardware validation。** `servo_tool.py` 的 `scan`、`test`、`setid` 已在實體伺服機上跑過並成功，但那是 bring-up 觀察，沒有 experiment ID；`diag` 仍只在假匯流排上測過。

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
│   ├── servo_tool.py               # 人工操作的單軸 bring-up 工具
│   ├── first_scan.sh               # 第一次上電：建環境、找埠、掃描（只讀）
│   └── assign_id.sh                # 單顆：掃描 → 轉動測試 → 改 ID → 再掃描
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
```

組裝檢查表：用瀏覽器開 `docs/assembly_checklist/index.html`。離線開啟時，勾選進度只存在該瀏覽器。

測試（不需要硬體）：

```bash
pip install pytest
pytest right_hand/tests -q      # 2026-10-04：17 passed
```

## 安全邊界

- `servo_tool.py` 是給人在工作台上用的 bring-up 工具，不在 AI 控制路徑上。生成式模型不直接下馬達命令；之後的動作一律經 versioned skill 與 Safety Gateway（見根目錄 `AGENTS.md`）。
- `test` 動作前會檢查：匯流排上只有一顆、電壓在 4.0–7.4V、溫度不超過 60°C；任何一項不符就不開扭力。離開前一定關扭力，包含例外與 Ctrl-C。
- 變壓器是實體斷電路徑。目前沒有獨立的 E-stop；多軸動作前要補上。
- 插拔伺服機或線材前先斷電。
- **USB 接地電位差未排除**：變壓器供電時，USB 線頭金屬殼一碰到驅動板 USB 外殼，Mac mini 螢幕就會黑一下（2026-10-04）。兩種接線順序都發生過，目前沒有已知不黑屏的順序。量測完成前不再上電；量測項目見 `BUILD_LOG.md`。

## 接下來

1. 量測並排除 USB 外殼之間的電位差。
2. 組食指並校正，再做其餘三指。
3. 手掌組裝、全手測試、外殼。
4. 建立 8-DOF semantic component IDs 與 manifest，把 bring-up 工具收斂成 hardware adapter。
5. 用雙目相機看這隻右手：相機是 Waveshare AR0144 Stereo USB Camera (A)，資料在 [`stereo_camera/`](../stereo_camera/README.md)，決策見 [ADR-0005](../docs/adr/0005-stereo-camera-ar0144.md)。
