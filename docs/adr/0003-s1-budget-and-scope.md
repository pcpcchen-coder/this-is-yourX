# ADR-0003: 採 S1 桌上型 MVP 預算與範圍

- Status: accepted
- Date: 2026-09-03
- Owners: George / project maintainers

## Context

S0 純模擬無法驗證真實 grounding；S2 機械臂與 edge GPU 在 self-model contract 尚未穩定前容易造成過早投資。

## Decision

採 S1：simulation + 2-DOF 實機 + 本地 RGB camera + touch + 硬體 E-stop。

目標採購區間為 **NT$10,000–15,000**，不含既有開發電腦、3D printer 與工具。此數字是設計預留，不是報價；採購前按台灣供貨、運費與稅重新確認。

預算優先順序：

1. 具有完整回授與 watchdog 的 actuator/controller。
2. 獨立電源、保險絲、E-stop 與固定機構。
3. 可固定、可校正的本地 camera。
4. Robot host、儲存與散熱。
5. 外觀、RGB-D、edge GPU、夾爪等延後。

## Out of scope for S1

- 高負載抓取、手指/夾爪、移動底盤、動態平衡。
- 雲端 vision/LLM。
- Jetson/獨立 GPU 採購。
- 無標記 open-world component recognition。
- 自主 learned policy 實機控制。

## Consequences

- 可在可控成本內建立完整 evidence/safety/diagnostic loop。
- 所有機構設計以低重量、短 link、無 payload 為前提。
- Pi 算力不足的大模型可在本地工作站離線或 LAN 內推論，但不影響 v0.1 gate。

## Validation

- 採購清單總價維持預算帶；超過 15% 需更新 ADR。
- S1 交付必須通過 v0.1 release gate，而不是只完成組裝。
- 不購買 edge GPU 也能完成 E00–E04、主要 E06 fault tests。

## Revisit when

v0.1 通過，並有 E05/E08 需要 RGB-D、更多 DOF 或更高算力的量測證據。

