# stereo_camera — 這是你的眼睛

Waveshare AR0144 Stereo USB Camera (A)，SKU 32695。用途是讓系統看得到 [`right_hand/`](../right_hand/README.md) 那隻右手。選用理由、取捨與驗收項目見 [ADR-0005](../docs/adr/0005-stereo-camera-ar0144.md)。

## 目前狀態（2026-10-03）

已到貨並接上 Mac mini（macOS）。外盒照片在 [`photos/2026-10-03_box-label.jpg`](photos/2026-10-03_box-label.jpg)。

2026-10-03 用 Photo Booth 看過一次即時畫面，屬於初步觀察，不是正式實驗，沒有 experiment ID：

- macOS 免驅動列舉成功，裝置名稱顯示為 `CCB Camera`。
- 影像是**彩色**。
- 輸出是左右併排的單一畫面，中央有接縫。
- 室內光下曝光正常，約半公尺內的近物與數公尺外的背景都算清楚。

Photo Booth 會裁切並鏡像預覽，所以這次無法判斷實際解析度、FPS，以及哪一半是左眼。**規格表仍全部是廠商標示值，尚未量測。**

## 廠商標示規格

| 項目 | 標示值 |
|---|---|
| 感光元件 | onsemi AR0144 ×2，1/4 吋，global shutter |
| 解析度 | 單眼 1280×720，左右併排 2560×720 |
| 基線 | 52 mm |
| 鏡頭 | 2.88 mm、F2.2、定焦；74°(D) / 65°(H) / 43°(V)；畸變 < 0.2% |
| 介面 | USB 2.0 Type-C，免驅動，5 V |
| MJPG | 1280×720 @ 60 FPS；2560×720 @ 30 或 60 FPS（產品頁與 Wiki 不一致） |
| YUY2 | 2560×720 @ 5 FPS；1280×720 @ 10 FPS |
| 系統 | Windows、Linux、macOS |
| 尺寸 | 66 × 30 × 17.72 mm |

來源：[Waveshare 產品頁](https://www.waveshare.com/ar0144-stereo-usb-camera-a.htm)、[Waveshare Wiki](https://www.waveshare.com/wiki/AR0144_Stereo_USB_Camera_(A))、[onsemi AR0144CS](https://www.onsemi.com/products/sensors/image-sensors/ar0144cs)。

## 開箱後要先確認的事

- [x] 彩色還是黑白 → 彩色（2026-10-03 Photo Booth 目視）。
- [ ] 併排影像哪一半是左眼。
- [ ] macOS 與 Ubuntu 上實際列出的格式、解析度、FPS。
- [ ] 0.4–0.6 m 的對焦是否清楚。
- [ ] 能否鎖定曝光、增益、白平衡。
- [ ] 運作時的溫度；Wiki 提到發熱明顯，不要碰 PCB 背面。

完整驗證項目與 gate 在 ADR-0005 的 Validation 一節。

## 安裝條件

- 剛性支架，距離手 0.4–0.6 m，斜上方約 30–45 度，完整看到手掌與四指。
- 霧面、與手部高對比的背景；柔和、不閃爍的照明。
- 校正後不要再動相機、手座或鏡頭；動了就要重新校正。

## 邊界

- 影像只在本機處理（[ADR-0002](../docs/adr/0002-local-camera-policy.md)）。
- 原始影像與錄影不提交到 Git；repo 只放 metadata、校正結果與可重建的腳本。
- 視覺只提供觀測，不進安全控制鏈。
