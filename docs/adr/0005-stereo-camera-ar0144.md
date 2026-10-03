# ADR-0005: S1 視覺來源採 AR0144 雙目 USB 相機

- Status: proposed
- Date: 2026-10-03
- Owners: George / project maintainers

## Context

S1 的視覺原本定為「既有本地 USB Webcam，先完成 RGB」，並明寫「S1 暫不採購深度相機」，需要近距離 3D 時再評估 RealSense D405（見 [HISTORY](../HISTORY.md) 2026-09-03、[ADR-0002](0002-local-camera-policy.md) 的 Camera baseline）。既有 Webcam 的型號一直沒有記錄。

George 已購入並收到 **Waveshare AR0144 Stereo USB Camera (A)**（SKU 32695），用途是讓系統看得到 Amazing Hand 右手。這改變了視覺來源，也讓 S1 提前具備深度，所以需要一份 ADR。

廠商標示的規格（[產品頁](https://www.waveshare.com/ar0144-stereo-usb-camera-a.htm)、[Wiki](https://www.waveshare.com/wiki/AR0144_Stereo_USB_Camera_(A))），本專案尚未實測：

| 項目 | 標示值 |
|---|---|
| 感光元件 | onsemi AR0144 ×2，1/4 吋，global shutter |
| 解析度 | 單眼 1280×720，左右併排輸出 2560×720 |
| 基線 | 52 mm |
| 鏡頭 | 焦距 2.88 mm、F2.2、定焦；視角 74°(D) / 65°(H) / 43°(V)；畸變 < 0.2%；650 nm 濾光片 |
| 介面 | USB 2.0 Type-C，免驅動；5 V ± 5% |
| 同步 | 左右影像同一 frame 輸出 |
| MJPG | 1280×720 @ 60 FPS；2560×720 產品頁寫 30 FPS、Wiki 寫 60 FPS |
| YUY2 | 2560×720 @ 5 FPS；1280×720 @ 10 FPS |
| 系統 | Windows、Linux、macOS；Wiki 註明 Mac 上部分 OpenCV 參數無法設定 |
| 尺寸 | 66 × 30 × 17.72 mm |

廠商資料沒有寫的：彩色或黑白、併排影像的左右順序、對焦距離與景深、同步精度、是否有出廠校正。

## Decision

- S1 的主要視覺來源改為這台雙目相機。既有 USB Webcam 降為 fallback。
- 相機自成一個 X，資料放在 [`stereo_camera/`](../../stereo_camera/README.md)，不與 `right_hand/` 混放。
- 以 **2560×720 MJPG 併排串流**擷取，切成左右兩張 1280×720 後各自處理。YUY2 在全解析度只有 5 FPS，不用於即時管線。
- 自行做雙目校正（ChArUco），保存 intrinsics、extrinsics、重投影誤差與 calibration revision。不假設有出廠校正。
- 3D 優先用「左右影像各自偵測 keypoint 再三角化」取得。全畫面 dense stereo matching 不列為 S1 必要項，因為素色手指外殼缺乏紋理，稠密匹配不可靠。
- 相機固定在距離手 **0.4–0.6 m**。以標示視角估算，手高約 0.2 m 時占畫面高度約 40–60%，符合 HISTORY 的 Webcam 條件。
- ADR-0002 的 local-only 政策不變：串流、校正、推論都留在本機。
- 視覺仍只提供觀測，不進安全控制鏈；stop / torque-off 只由 deterministic safety controller 執行。

不在這次決策內：是否改買 RGB-D、相機支架的機構設計、手眼標定方法、把相機寫進 body manifest 的 schema 變更。

### 關於「1080p30 或可說明的等效設定」

S1 驗收表的 Camera capture gate 允許等效設定。本 ADR 主張以 **單眼 1280×720、≥ 30 FPS、global shutter** 作為等效設定，理由與代價如下：

- 以標示的 65° 水平視角估算，焦距約 1005 px。在 0.5 m 處每像素約 0.50 mm。
- 作為對照，ADR-0002 推薦的 C920 類 1080p 相機（對角約 78°）在同距離約 0.37 mm/px。也就是說這台的空間取樣粗約 1.4 倍；把相機移近到 0.4 m 時是約 0.40 mm/px。
- Keypoint gate 是影像對角線的 1%。在 1280×720 上是約 14.7 px，0.5 m 處約 7 mm，仍在這個解析度可量測的範圍內。
- 換來的是 global shutter（手指運動時沒有 rolling-shutter 變形）、同 frame 的雙視角，以及最高 60 FPS。

### 預期的深度解析度

由 Z = f·B / d，取 f ≈ 1005 px、B = 52 mm：

| 距離 | 視差 | 視差每差 1 px 的深度變化 | 畫面涵蓋（寬 × 高） |
|---:|---:|---:|---:|
| 0.4 m | 131 px | 3.1 mm | 0.51 × 0.32 m |
| 0.5 m | 105 px | 4.8 mm | 0.64 × 0.39 m |
| 0.6 m | 87 px | 6.9 mm | 0.76 × 0.47 m |

這些是由標示規格推算的理論值，實際要以校正後的焦距與量測結果為準。

## Alternatives considered

### 維持既有 USB Webcam（原決策）

不用新增硬體，管線最簡單。但型號未知、多半是 rolling shutter，單眼無法提供絕對深度；HISTORY 已把「手指重疊造成單眼觀測不達標」列為升級條件。保留為 fallback，用來驗證單眼 keypoint 管線與比較。

### RealSense D405

HISTORY 原先指定優先評估。有成熟 SDK 與出廠校正，理想工作距離約 7–50 cm。未採用的原因：雙目相機已經到手，而 D405 在目標 macOS / Linux / ROS 環境的 SDK 支援仍待驗證。若本 ADR 的驗證不過，D405 仍是下一個候選。

### 兩台獨立 USB Webcam 自組雙目

沒有硬體同步，手指運動時左右影像時間差會直接變成深度誤差。不採用。

## Consequences

- 需要自己維護雙目校正；相機或手座被碰到後，calibration evidence 失效，要重做。
- 深度由主機計算，相機不輸出深度圖。
- USB 2.0 頻寬下全解析度只能用 MJPG，壓縮失真會影響次像素精度。
- macOS 上經 OpenCV 可能無法鎖定曝光、增益、白平衡。HISTORY 要求校正後鎖定這些設定；做不到就必須記錄限制，或改在 Linux 擷取。
- Wiki 提到模組運作時發熱明顯。熱漂移可能影響校正，需要量測暖機前後的差異。
- 若實物是黑白版，HISTORY 提到的「初期貼彩色點產生 ground truth」不可用，要改用 ArUco / AprilTag 類 marker。
- Dataset v0 的每筆紀錄要能指到左右兩張影像與 stereo calibration revision。
- 後續工作：相機納入 body manifest、fake camera adapter、stale / disconnect 測試、ADR-0002 的 Camera baseline 一節加註由本 ADR 取代。

## Validation

到貨後依序驗證，結果記在 `stereo_camera/` 並附 experiment ID。下列數值是初始 gate，首次實測後若要調整必須留下 revision。

1. **列舉與模式**：在 macOS 與 Ubuntu 24.04 上都能以 UVC 開啟；列出實際支援的格式、解析度、FPS，釐清 2560×720 MJPG 是 30 還是 60 FPS。
2. **基本事實**：彩色或黑白；併排影像哪一半是左眼。
3. **擷取**：2560×720 MJPG 連續 10 分鐘，實測 FPS ≥ 30，記錄掉幀數與 frame timestamp jitter。
4. **同步**：拍攝快速移動或閃爍的目標，左右影像看到同一瞬間；量不到同步誤差的方法要寫明。
5. **對焦**：0.4–0.6 m 處 ChArUco 角點可穩定偵測。
6. **曝光鎖定**：各 OS 上能否固定曝光、增益、白平衡；不能的記為限制。
7. **雙目校正**：ChArUco，stereo RMS 重投影誤差 < 0.5 px，rectify 後對應點的垂直誤差平均 < 1 px。
8. **深度**：把校正板放在 0.4、0.5、0.6 m 的已量測距離，三角化距離誤差 < 2%。
9. **熱漂移**：冷機與運作 30 分鐘後各做一次第 8 項，比較差異。
10. **故障行為**：拔掉 USB 後 1 秒內回報 degraded / fault，不沿用舊影像，不影響硬體停止路徑。

通過後才進入 HISTORY 的 Phase B–D。Phase A（伺服機 bring-up）不依賴相機，可以先做。

## Revisit when

- 第 3、4、7、8 項任一項不過，且無法以設定或安裝方式解決。
- 需要 0.3 m 以內的近距離操作，定焦鏡頭或視差範圍不夠用。
- 需要稠密點雲、6D 物體姿態或 3D 碰撞檢查，而 keypoint 三角化不足。
- macOS 上無法鎖定曝光導致資料品質不達標，且不打算把擷取移到 Linux。
