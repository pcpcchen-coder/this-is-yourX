# ADR-0001: 第一個實體採新製 2-DOF 桌上教具

- Status: accepted
- Date: 2026-09-03
- Owners: George / project maintainers

## Context

專案需要第一個可安全、可重複驗證「this is your X」的實體。既有 InMoov 或完整機械臂會同時帶入大量機構、拉索、舵機、校正與安全問題；純模擬又無法證明真實 sensorimotor grounding。

## Decision

第一個實體 embodiment 採新製 2-DOF 桌上教具：

```text
base -> shoulder_joint -> upper_link -> elbow_joint -> hand_pointer
```

- shoulder、elbow 使用可回讀 position/current/temperature/error 的 smart servo。
- hand 第一版是輕量指示棒，不使用夾爪、不帶 payload。
- 固定本地相機觀察整個機構；link 使用 AprilTag/高對比標記提供 ground truth。
- hand 加一個簡單 touch sensor。
- 最大 link 長度初始限制：upper 100 mm、forearm/hand 80 mm；實際尺寸需經扭矩計算再確認。
- 初期只允許低速、低電流、±3° supervised wiggle。

## Alternatives considered

### Simulation only

仍是必要的 Phase 1，但不能取代真實摩擦、背隙、接線與感測器失配。

### Existing InMoov hand/arm

保留為第二個 embodiment。常見 PWM hobby servo 無法提供可靠 position/current feedback，不適合作第一個 proprioception/self-model 基線；日後可藉此測試「感測能力不足的身體」與外加 encoder。

### Complete 4–6 DOF arm

可展示更多 task，但第一版的成本、碰撞空間、整合與 fault surface 過大。

## Consequences

- 需購買兩顆 smart servo、控制板、相機、獨立供電、E-stop 與製作機構。
- 可在低能量環境完成 E00–E07，再擴展到 tool attachment/完整手臂。
- manifest、URDF 與 API 仍保持多 embodiment；不把 2-DOF 寫死在 core。

## Validation

- E00–E04 全通過。
- driver loss、encoder freeze、超限命令、E-stop 全部 fail closed。
- 10 次 supervised wiggle 正確識別 joint，false action = 0。
- 實機連續 30 分鐘 idle/小幅動作無非預期 reboot 或通訊中斷。

## Revisit when

v0.1 gate 通過，或扭矩試算顯示指定尺寸超過安全連續工作能力。

