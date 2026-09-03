# ADR-0004: v0.1 採 Ubuntu 24.04、ROS 2 Jazzy、Gazebo Harmonic

- Status: accepted
- Date: 2026-09-03
- Owners: project maintainers

## Context

2026-09 已有較新的 ROS 2 Lyrical/Ubuntu 26.04/Gazebo Jetty 組合；但 v0.1 更重視相機、DYNAMIXEL、ros2_control、MoveIt 與 Raspberry Pi 套件的成熟度和可重現性。

## Decision

- Robot host：Ubuntu 24.04 LTS 64-bit。
- Middleware：ROS 2 Jazzy Jalisco。
- Simulation：Gazebo Harmonic。
- Python orchestration、C++/MCU 負責 timing-sensitive path。
- `lyrical` 建立相容性測試目標，但不作 v0.1 實機預設。
- USB UVC camera 避免 Ubuntu 24.04 的 Pi CSI/libcamera 限制。

## Alternatives considered

- ROS 2 Lyrical + Ubuntu 26.04 + Gazebo Jetty：新 LTS，保留為升級線。
- Raspberry Pi OS：相機支援直接，但 ROS binary 與專案基線會更分散。
- macOS robot runtime：適合開發/本地 AI，不作低階硬體與 ROS 驅動基線。

## Consequences

- 初期較容易取得成熟套件與文件。
- CSI Camera Module 3 不作預設，改用 UVC camera。
- 未來升級 Lyrical 需跑 simulation/replay/HIL，而不是直接換實機映像。

## Validation

- 乾淨 image 可執行 build、simulation、camera、controller smoke tests。
- 依賴鎖定並保存 OS/ROS/Gazebo revision。
- CI 或定期工作測試 Lyrical，列出阻擋套件。

## Revisit when

所有必要 driver、ros2_control、camera 與 simulation tests 在 Lyrical/Jetty 通過，且升級收益高於遷移成本。

