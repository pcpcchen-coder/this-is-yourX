# 協作方式與待決策項目

## 0. 已接受的 Phase 0 決策（2026-09-03）

- 第一個實機：**新製 2-DOF 桌上教具**。
- 視覺資料：**local-only camera / local inference**，v0.1 不使用雲端 VLM。
- 預算與範圍：**S1 桌上型 MVP**，目標 NT$10,000–15,000，採購前再核價。
- Runtime：Ubuntu 24.04 + ROS 2 Jazzy + Gazebo Harmonic。

詳細理由、替代方案與 revisit 條件見 [`docs/adr/`](adr/)，執行清單見 [S1 Build Plan](11_S1_BUILD_PLAN.md)。以下未勾選表格保留作後續平台擴展的決策歷程。

## 1. 我們怎麼合作

這個專案同時包含研究假說、機器人整合與 AI 軟體，建議所有工作都能回答三個問題：

1. 要證明哪一個 self-model 能力？
2. 用什麼 evidence/metric 判定成功？
3. 失敗時如何安全停止、重播與修正？

每個 PR 只推進一個清楚能力；硬體選型、API breaking change、安全政策與資料上雲另寫 ADR。

## 2. George 現階段最能協助的資訊

請在 issue 或下一輪討論提供：

### A. 第一個 embodiment

- [ ] 純模擬起步。
- [ ] 新做 2-DOF 桌上教具（目前推薦）。
- [ ] 已有機械臂：品牌/型號/DOF/SDK/URDF。
- [ ] 直接使用 InMoov 某一部位：請指定頭、右手、手臂或其他。

### B. 現有硬體盤點

- Linux/Windows/Mac 主機及 GPU/記憶體。
- MCU、servo、motor driver、電源、相機、IMU、麥克風、3D printer。
- 可否固定裝置、加護罩、獨立 motor power 與實體 E-stop。
- 可用預算範圍與希望的體積/payload。

### C. 感知與模型政策

- 相機是否允許持續錄影？是否包含家人/同事？
- 麥克風是否必要，或第一版只用文字/UI 教學？
- 是否允許雲端 LLM/VLM，哪些資料不得離開本機？
- 是否已有偏好的本地模型與 GPU 主機？

### D. Demo 優先順序

請排序：

- 指出並說出「這是你的手」。
- 動一下被指定的 joint。
- 回答健康/故障原因。
- 自動校正或發現接線錯誤。
- 安裝新工具後更新能力。
- 跨到第二台 robot 仍使用同一套 AI/self-model。

## 3. 建議立即建立的 ADR

第一批 ADR 已建立：第一個 embodiment、本地相機政策、S1 預算範圍與 ROS runtime。Actuator/bus 的候選基線記錄在 S1 Build Plan；完成台灣供貨與實際扭矩核對後，再建立最終採購 ADR。

ADR 格式：

```markdown
# ADR-XXXX: 決策標題

- Status: proposed | accepted | superseded
- Date: YYYY-MM-DD
- Owners: ...

## Context
要解決的問題與限制。

## Decision
決定採用什麼。

## Alternatives
評估過的選項與不採用原因。

## Consequences
新增的成本、風險、測試與後續工作。

## Validation
用哪些實驗/metric 驗證。
```

## 4. Issue 模板（建議）

```markdown
## Goal
使用者/系統完成什麼。

## Self-model level
L0 / L1 / L2 / L3 / L4

## Inputs and outputs
輸入、結構化輸出、topic/API。

## Evidence
什麼證據支持結果。

## Safety impact
Observe / Simulate / Supervised / Autonomous；hazards 與 stop。

## Acceptance criteria
- [ ] 可量測條件 1
- [ ] fault/rejection 條件
- [ ] docs/replay data

## Revisions
manifest / hardware / software / model / calibration。
```

## 5. Branch 與 PR

- `main` 應保持可驗證。
- feature branch：`feat/<short-name>`；文件：`docs/<short-name>`；修正：`fix/<short-name>`。
- PR 描述包含：目的、架構變更、測試命令/結果、實機與否、風險、rollback。
- 介面或 manifest schema 變更需附 before/after 範例與 migration。
- 實機測試結果附 `experiment_id`，避免只放「肉眼看起來成功」。

## 6. 貢獻者角色

| 角色 | 責任 |
|---|---|
| Product/research owner | 定義要證明的能力與 demo |
| Robot integration | 機構、電源、driver、URDF、calibration |
| Self-model | schema、graph、state/evidence/API |
| Perception/ML | vision grounding、prediction、dataset/model |
| Safety reviewer | hazard、policy、E-stop/watchdog tests |
| Experiment owner | protocol、ground truth、metrics、replay |

一人可以兼任，但實機第一次 enable、limit 變更與高能量硬體最好由另一人交叉核對。

## 7. 建議的第一個共同工作包

### George

1. 回覆 A–D 的選項與現有硬體照片/型號。
2. 確認桌面空間、電源與相機安裝位置。
3. 決定第一版採文字/UI 或加入語音。
4. 若已有 InMoov/servo，提供關節、控制板與電源資訊。

### 專案實作端

1. 建立 ROS 2 workspace 與 CI。
2. 實作 schema validator/manifest loader/body graph CLI。
3. 建 2-DOF URDF + Gazebo + fault injection。
4. 實作 read-only API 後，再加入 bounded `wiggle_joint`。

### 共同驗收

依 E00–E04 完成 v0.1 demo；完整保留失敗案例，作為後續 learned model 與 diagnosis 的資料。

## 8. 下一輪需要 George 協助的最少資訊

Phase 0 的三個方向已回答。採購與機構定稿前，請再提供：

1. 目前是否已有 Raspberry Pi 5、USB webcam、5V 電源或 3D printer 可沿用？
2. 2-DOF 機構希望自行 3D 列印，還是購買 ROBOTIS 原廠 frame？
3. 你偏好由台灣通路一次買齊，或可接受 ROBOTIS 海外採購？

這三項只影響 BOM/成本，不阻擋 simulation 與 schema/API 實作。
