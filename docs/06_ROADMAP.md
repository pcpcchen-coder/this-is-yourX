# 開發路線圖

本路線圖以 gate 驅動，不以日期硬推。每階段只有在驗收通過後才擴大硬體、自主性或模型複雜度。

## Phase 0 — 規格與決策基線

目標：把抽象願景轉成版本化 contract。

交付物：

- project vision、architecture、safety policy。
- component JSON Schema 與 2-DOF manifest。
- ADR：OS/ROS、第一個 embodiment、actuator、camera、資料政策。
- 初始 threat/hazard list。

Gate：E00 通過；待決策項目有 owner；不含未標示的 assumed hardware。

## Phase 1 — Simulation skeleton

目標：在不接硬體前跑通 self-model read path。

交付物：

- ROS 2 workspace、interfaces、registry、query CLI。
- 2-DOF URDF/Xacro、Gazebo world、fake/real clock tests。
- TF/location query、graph revision、structured events。
- CI：schema、lint、unit、simulation smoke test。

Gate：E00–E02 通過；重啟後 revision 與 query 結果可重現。

## Phase 2 — Safe skill gateway

目標：建立唯一動作入口。

交付物：

- `ExecuteSkill` action。
- `stop`、`wiggle_joint` 兩個 skill。
- limits、precondition、三值邏輯、resource lock、timeout/cancel。
- audit trace 與 adversarial tests。

Gate：超限、unknown/stale、E-stop、未授權全部拒絕；simulation E09 通過。

## Phase 3 — 2-DOF 實機 bring-up

目標：同一 contract 接到實機。

交付物：

- 採購/組裝/接線/安全檢查表。
- ros2_control hardware adapter 或 vendor bridge。
- E-stop、motor enable、watchdog、state feedback。
- HIL test fixture、hardware inventory、calibration record。

Gate：連續 30 分鐘 idle/小動作無通訊 fault；拔線、斷 host、按 E-stop 均進 safe state；沒有 AI 也能完成 bring-up。

## Phase 4 — 多模態 grounding

目標：證明「這是你的 X」可由多個 evidence 支撐。

交付物：

- camera calibration、tag/keypoint detector。
- touch event adapter。
- grounding session UI/CLI、candidate/confirm/revoke workflow。
- supervised wiggle correlation 與 evidence store。

Gate：E03、E04、E06 通過；人工修正可重播；遮擋或歧義不自動確認。

## Phase 5 — Prediction 與 self-diagnosis

目標：從「知道狀態」提升到「知道動作後果與模型何時錯」。

交付物：

- kinematic forward prediction。
- predicted/observed comparison、threshold versioning。
- learned residual（可選，先離線）。
- fault lifecycle、degraded mode、revalidation。

Gate：E05–E07 通過；模型 uncertainty 能實際阻止不安全動作；新模型可 rollback。

## Phase 6 — Tool 與 capability adaptation

目標：自身邊界可因 attachment 改變。

交付物：

- mount/attachment contract。
- tool identity handshake、TCP calibration、capability activation。
- attach/remove event 與 evidence invalidation。

Gate：E08 通過；未確認工具沒有 actuator permission；拆除後 skill 即時失效。

## Phase 7 — AI planner / VLM / learned policy

目標：讓模型使用 self-model，而非把 self-model 藏在 prompt 裡。

交付物：

- provider-neutral AI adapter。
- structured query/skill tool definitions。
- prompt-injection/adversarial suite。
- 可選 LeRobot dataset/export、imitation policy。

Gate：模型更換不影響 safety contract；AI runtime 被 kill 時 robot 仍可安全停止；false action rate 仍為 0。

## Phase 8 — 擴展 embodiment

候選：4–6 DOF 機械臂、InMoov 單臂、移動底盤、頭部/聲音器官、電池與 power graph。

擴展規則：

- 先重用 interface contract，再增加 component type。
- 新平台先 simulation/replay，再上實機。
- 新 actuator energy 等級重新做 hazard analysis。
- 不以 demo 成功取代 regression/fault tests。

## 建議 milestone 標記

| Milestone | 可對外展示的能力 |
|---|---|
| M0: I have parts | 列出、查詢、定位模擬元件 |
| M1: This is my hand | 人類教學 + 多證據 grounding |
| M2: I know what will move | 小動作 prediction + 安全執行 |
| M3: Something is wrong | sensor/actuator 失配與降級 |
| M4: I gained a tool | attachment 與 capability 更新 |
| M5: Same mind, new body | 相同 self-model API 移植第二平台 |

## 建議第一個 4 個 iteration

### Iteration 1

- 建 repo skeleton、schema validation、manifest loader。
- 產生 graph、`list/get/resolve` CLI。

### Iteration 2

- 加 2-DOF URDF、TF、Gazebo。
- `where-is`、graph revision、rosbag metadata。

### Iteration 3

- skill gateway、`wiggle_joint`、policy/timeout/cancel。
- fault injection tests。

### Iteration 4

- 硬體 bring-up、tag camera、touch。
- grounding session + evidence store + demo。

每個 iteration 應小到一個 PR 能審查，且附測試命令、結果與尚未解決風險。
