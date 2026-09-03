# ADR-0002: 相機與視覺推論採 local-only

- Status: accepted
- Date: 2026-09-03
- Owners: George / project maintainers

## Context

相機用於 link 定位、指向/觸碰教學、wiggle correlation 與實驗重播。畫面可能包含住家、工作環境、家人或同事，因此資料路徑必須在導入 VLM 前確定。

## Decision

- 相機串流、快照、calibration、tag detection、grounding 與資料集處理保持在 robot host 或同一受控本地網路。
- v0.1 不呼叫任何雲端 VLM/vision API。
- Raspberry Pi 上先執行 AprilTag/ArUco/OpenCV 類 deterministic vision；大型本地 VLM 若需要，可由本地工作站提供服務。
- 本地推論服務不得取得 Safety Gateway approval 或直接 driver 權限。
- 預設不持續保存完整影像；只有明確開始的 experiment session 才記錄。
- 對外發布 rosbag、影片或 dataset 前必須人工檢查並去識別。

## Camera baseline

v0.1 優先使用 USB UVC 1080p camera，以維持 Ubuntu 24.04 + ROS 2 Jazzy 基線。Raspberry Pi Camera Module 3 保留為替代選項，但 Canonical 文件指出 Ubuntu 25.04 以前的 libcamera stack 不可用，因此在 Ubuntu 24.04 上會引入額外風險。

推薦等級：Logitech C920e/C920s 類 1080p30 USB camera，具 autofocus、約 78° FoV、腳架固定與 privacy cover；固定後鎖定曝光/對焦（如 driver 支援），完成 camera calibration。

## Alternatives considered

- 雲端 VLM：初期不採用，因 tag-based grounding 不需要，且增加隱私、延遲、費用與供應商相依。
- Raspberry Pi CSI camera：成本低且整合漂亮，但與 Ubuntu 24.04 camera stack 的版本選擇衝突。
- RGB-D/OAK-D/RealSense：保留 Phase 5/6；v0.1 的 2D marker + TF 已能驗證核心假說。

## Consequences

- 外網斷線不影響視覺 grounding。
- Pi 只執行低成本 CV；本地 VLM 可晚一階段加入。
- 必須固定相機、保存 intrinsics/extrinsics revision，並管理 local storage。

## Validation

- 網路出口封鎖時，E03/E04 仍完整運作。
- packet capture/設定審查顯示 image topic 未傳到外網。
- camera stale/遮擋時回報 unknown，不觸發 motion。
- experiment recording 有可見狀態與可停止/刪除機制。

## Revisit when

本地 VLM 明確無法達成新任務，且另有資料分類、同意、脫敏與雲端安全 ADR。

