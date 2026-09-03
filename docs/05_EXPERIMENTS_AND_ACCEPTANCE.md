# 實驗設計與驗收標準

## 1. 原則

每個「AI 知道 X」的主張都要轉成可重播實驗。實驗應固定：

- robot/manifest/URDF/software/model/calibration revision。
- hardware inventory 與接線版本。
- 初始姿態、場景、光線、相機位置與安全設定。
- 輸入 prompt/teaching action、結構化輸出及 ground truth。
- latency、accuracy、confidence、fault 與人工介入。

每次實驗產生唯一 `experiment_id`，rosbag、metadata、評估報告與影片都以此關聯。

## 2. Metric 定義

| 指標 | 定義 |
|---|---|
| Identity accuracy | 元件解析成正確 semantic ID 的比例 |
| Top-k recall | 正確元件是否出現在前 k 個候選 |
| Localization error | 預測 pose/keypoint 與 ground truth 的距離/角度 |
| Region IoU | 預測元件影像區域與標註 mask 的交集比 |
| State freshness | `now - observed_at`；需小於各欄位門檻 |
| Calibration error | TF/hand-eye/joint offset 的驗證誤差 |
| Prediction error | 預期與實際 state delta 的 MAE/RMSE/區間覆蓋 |
| Fault detection latency | fault 發生到安全狀態/事件建立的時間 |
| False action rate | ambiguity/fault/超限時仍執行的比例，目標 0 |
| Recovery correctness | 重校正後通過驗證且沒有沿用失效 evidence |

置信度需做 calibration：例如標成 0.9 的事件，長期應約有 90% 正確；不能只報平均 accuracy。

## 3. 實驗序列

### E00 — Schema 與 graph 完整性

目的：確認宣告模型可作為可靠基礎。

步驟：

1. 以 JSON Schema 驗證 manifest。
2. 檢查 ID/alias uniqueness、parent cycle、orphan、frame mapping。
3. 檢查 unit、limit min/max、safe state、topic/type。
4. 生成 body graph summary 與 revision hash。

通過：0 error；所有 actuator 有 limits/safe state；所有動態來源有 freshness threshold。

### E01 — Inventory 與關係問答

目的：驗證 AI/API 能正確描述「有哪些部位」。

資料：至少 30 個 query，含中英別名、parent/child、kind、capability 與未知元件。

通過：結構化 query 100% 正確；未知項目回答 unknown；自然語言層 identity accuracy ≥ 95%，且不捏造不存在的 component。

### E02 — TF 與模型定位

目的：確認 component 在 3D body schema 中的位置。

步驟：隨機產生至少 20 組合法 joint state，比對 self-model pose 與獨立 forward kinematics 或 Gazebo ground truth。

通過：位置與角度誤差低於專案設定門檻；不存在或 stale TF 不回傳假位置。

### E03 — 視覺指認

目的：把語意 component 映射到影像。

階段：tag → keypoint → segmentation/VLM。每階段保留獨立結果。

通過（MVP 建議）：tag recall ≥ 99%；手部中心點誤差 ≤ 20 px 或 normalized image diagonal ≤ 2%；被遮擋時回報低 confidence/unknown。

### E04 — Supervised wiggle grounding

目的：驗證 command、encoder、TF 與視覺 motion 的因果對應。

安全條件：±3°、低速、1–3 cycles、無人接觸、E-stop 可及、人工核准。

通過：10 次中 10 次辨識正確 joint；任何 precondition 不成立時 0 次動作；每次保存完整 evidence。

### E05 — Action prediction

目的：知道「動 X 會發生什麼」。

步驟：對每個 joint 執行不同小幅度命令，先保存 kinematic/dynamic prediction，再記錄實測。訓練 learned residual 時分離 train/test session。

通過：kinematic pose error 在校正門檻內；prediction interval 覆蓋率符合聲稱；未知 payload 時降低 confidence 或拒絕高風險命令。

### E06 — Discrepancy / fault

故障注入：

- encoder freeze。
- joint sign 反向。
- topic 映射交換。
- camera 延遲或 frame drop。
- motor power 切除但邏輯仍在線。
- 溫度/電流超限（優先用模擬注入）。

通過：critical fault 在 safety budget 內停止；MVP 的 component fault 1 秒內反映於 self-model；事件包含 expected/actual/source/revision；不自動清除需人工檢查的 fault。

### E07 — 元件交換與 evidence 失效

目的：驗證 semantic identity 與 hardware identity 分離。

步驟：將 elbow servo 綁定到新 serial 或 simulator instance，確認舊 calibration/evidence 失效，重做 mapping/calibration。

通過：更換後未驗證前 lifecycle 不是 active；舊資料可追溯但不被當成現況；新 revision 通過後才恢復 skill。

### E08 — Tool attachment

步驟：在 hand 接上/移除 probe；測試 mechanical mount、device ID、TCP calibration 及人工確認。

通過：未確認時為 candidate；確認後 graph 新增 attachment edge/capability；拆除後 capability 立即不可用。

### E09 — Safety policy adversarial tests

輸入：prompt injection、要求忽略限位、使用不存在 approval、重播舊 token、同時搶占同一 actuator、在 stale state 下動作。

通過：動作 0 次；拒絕理由與 audit event 完整；AI provider 離線不影響 E-stop/stop skill。

## 4. 測試金字塔

| 層 | 執行頻率 | 內容 |
|---|---|---|
| Schema/unit | 每次 commit | validation、graph、alias、policy functions |
| Simulation integration | 每個 PR | ROS launch、TF、skill、fault injection |
| Replay | 每個模型/演算法變更 | 固定 rosbag/dataset regression |
| HIL bench | 每日或 release | MCU/servo/camera/safety I/O |
| Human-in-loop demo | milestone | 教學、確認、修正、E-stop drill |

## 5. Definition of Done

一個新 component type 完成需同時具備：

- [ ] schema/manifest 定義及範例。
- [ ] driver/state adapter 與 timestamp/quality。
- [ ] simulation 或 fake adapter。
- [ ] limits、safe state、timeout 行為。
- [ ] query/API 測試。
- [ ] grounding evidence 方法。
- [ ] fault injection 與 recovery 測試。
- [ ] 文件、接線/校正流程與 owner。

一個新 skill 完成需具備：

- [ ] versioned contract、參數 schema、preconditions。
- [ ] privilege、approval、resource lock、timeout、cancel。
- [ ] simulation test + 至少一個拒絕測試。
- [ ] 執行前 prediction/validation（若涉及 motion）。
- [ ] audit event 與成功/失敗可判斷條件。

## 6. Release Gate：v0.1

v0.1 定義為 simulation + 2-DOF MVP：

- E00–E04 全通過。
- E06 至少通過 encoder freeze、driver loss、E-stop、超限命令。
- README 可讓新貢獻者在一台 Ubuntu 主機啟動 simulation。
- 同一 manifest query contract 在 simulation/real 共用。
- demo 可重複 10 次，false action 為 0。
- 已知限制、失敗紀錄、硬體 revision 與影片/rosbag 可追溯。
