# 可能的實現方式

## 1. 先選研究問題，再選機器人

「AI 認知自己的元件」可以用不同硬體證明。若目標是驗證 identity/grounding，複雜人形機器人反而帶來大量與核心無關的機構、安全及驅動問題。本文件提供四條可逐步相容的路線。

## 2. 方案比較

| 方案 | 內容 | 能驗證的核心能力 | 成本/風險 | 建議用途 |
|---|---|---|---|---|
| A. Simulation-first | URDF + Gazebo 虛擬 2–6 DOF | manifest、TF、API、prediction、fault injection | 最低；缺少真實誤差 | 必做，CI 基線 |
| B. Sensorized 2-DOF rig | 兩關節、末端、相機、touch | 語言/視覺/動作 grounding、失配偵測 | 低；桌上安全可控 | **第一個實機 MVP** |
| C. 低成本機械臂 | 4–6 DOF + gripper + RGB-D | body schema、技能、tool attachment、模仿學習 | 中；夾傷與校正風險 | MVP 穩定後 |
| D. Mobile/humanoid | 底盤、雙臂、頭、電池等 | ownership boundary、多器官依賴、跨 embodiment | 高；安全與整合最難 | 長期展示 |

## 3. 方案 A：純模擬

### 實作

1. 建立簡化 robot：`base → shoulder → upper_link → elbow → hand`。
2. 在 URDF/Xacro 定義幾何、joint axis 與 limits。
3. 以 manifest 加上語意別名、感測 topic、capabilities 與安全狀態。
4. Gazebo 模擬 joint encoder、camera、touch/contact 與 fault。
5. `robot_state_publisher` 產生 TF；self-model service 合併 manifest + runtime state。
6. 建立 CLI：`who-are-you`, `where-is hand`, `what-if move elbow 5deg`。

### 必做 fault injection

- joint feedback freeze。
- 左右 joint topic 對調。
- camera 時戳延遲。
- sensor noise 增大。
- component 從 manifest 移除但 driver 仍存在。

### Gate

同一批 contract tests 必須能在 fake driver 與 Gazebo driver 上執行；未通過不得接馬達。

## 4. 方案 B：2-DOF 桌上型教具（推薦）

### 機構

- `base` 固定在桌面或帶重量底座。
- `shoulder_joint` 與 `elbow_joint` 各由 smart servo 驅動。
- `hand` 可先用被動指示棒，第二階段再換夾爪。
- 各 link 貼 AprilTag/ArUco 或高對比色塊，作為初期可解釋視覺 ground truth。
- 在末端加 FSR、micro switch 或小型 load cell，提供「被觸碰」事件。

### 教學流程

1. 系統載入 manifest，知道候選元件但 alias 可尚未確認。
2. 使用者在相機畫面點選/實體觸碰元件並輸入「這是你的手」。
3. 系統取得 candidate：靠近指向點的 link、發生 touch 的 component、TF 投影區域。
4. 如仍不確定，請求 supervised wiggle。
5. Gateway 執行 ±3°、低速度動作；比對 commanded joint、encoder delta、visual motion region。
6. 顯示證據與 confidence，使用者確認後建立 alias revision。

### 為什麼需要視覺標記

初期目標是驗證整個 evidence pipeline，而不是挑戰最難的無標記零件辨識。標記提供可量測 ground truth；等系統可靠後，再逐步改為 keypoint/segmentation/VLM。

### MVP demo script

1. 問：「你有哪些部位？」系統列出 base、shoulder、elbow、hand。
2. 問：「哪個是你的手？」UI 同時 highlight URDF 及相機畫面。
3. 問：「動一下你的手。」AI 轉成 `wiggle_joint_group(hand_chain)`，Gateway 限幅執行。
4. 人手阻擋或拔掉 touch sensor，系統偵測失配並停用相關 skill。
5. 替換末端為 probe，完成 attachment handshake 後回答「我現在有一支 probe」。

## 5. 方案 C：低成本機械臂

可使用已有 ROS/LeRobot 支援或可提供 serial/CAN/TCP SDK 的平台。選型時優先順序：

1. 能回讀每個 joint 的實際位置。
2. 有可靠的 torque disable / emergency stop。
3. URDF、尺寸、joint limits 與通訊協定可取得。
4. 可讀 current/temperature/fault code 更佳。
5. 有固定可重複的 homing/calibration 方法。
6. SDK 授權允許開源 adapter。

### 實作策略

- 用 `ros2_control` 封裝硬體，不讓 self-model 直接呼叫 vendor SDK。
- 使用 MoveIt 2 計算 IK、碰撞與 trajectory；其結果仍經 Gateway 二次檢查。
- 以 wrist camera 或固定 RGB-D 觀察手臂；記錄 TF 與影像同步。
- 若使用 LeRobot，將資料收集與 policy inference 視為可插拔模組；self-model contract 仍是本專案核心。

### 第一批任務

- 指向指定自己的 link。
- 把手移到相機可見區並自我定位。
- pick-and-place 前描述將使用哪些元件。
- 安裝不同 gripper 後更新 capability。
- 故意調錯一個 joint sign，偵測 prediction/observation mismatch。

## 6. 方案 D：移動式或人形平台

此路線新增「整體自身邊界」問題：wheel、battery、network、arm、camera 都彼此依賴。建議分 domain 建模：

- `mobility`：wheel、motor、encoder、odometry、brake。
- `manipulation`：arm、hand、tool。
- `perception`：camera、mic、IMU、lidar。
- `power`：battery、BMS、rail、charger。
- `compute`：MCU、SBC、GPU、network。
- `safety`：E-stop、bumper、protective stop、watchdog。

AI 應能做 dependency-aware 回答，例如：「右臂本身正常，但 24 V actuator rail 被 safety relay 切斷，所以無法動作。」

## 7. 三種 grounding 方法

### 7.1 Configuration grounding

從 manifest、URDF、driver mapping 直接建立。快速且可重現，但只能證明「設定如此」，不能證明接線正確。

### 7.2 Interactive grounding

透過人類指向、touch、QR/AprilTag、UI selection 與語音建立語意。適合命名與消歧；要保存誰確認、何時確認及輸入資料。

### 7.3 Sensorimotor grounding

透過受限動作建立因果關聯：命令 joint A 後 encoder A、link pose 與影像區域應同步變化。它比單純視覺相似更能確認控制對應，但必須置於安全條件內。

正式確認建議至少包含 configuration + interactive 或 configuration + sensorimotor 兩類獨立證據。

## 8. AI 實現選項

| AI 功能 | 最簡版本 | 進階版本 | 安全界線 |
|---|---|---|---|
| 語言解析 | 規則/JSON form | LLM function calling | 僅產生 query/skill request |
| 視覺指認 | tag/色塊/keypoint | segmentation/VLM | 低 confidence 必須確認 |
| 狀態解釋 | template | LLM 依結構化 evidence 摘要 | 不可隱藏 fault |
| 動作預測 | URDF kinematics | physics + learned residual | uncertainty 過高不執行 |
| 技能選擇 | state machine | behavior tree/LLM planner | allowlist + policy gateway |
| 動作 policy | teleop/trajectory | imitation/RL/VLA | 輸出再限幅、可中止 |

## 9. 建議決策

除非已有可立即使用且安全的機械臂，建議採：

1. A 與 B 並行：先讓 simulation contract 跑通，再用 2-DOF rig 驗證真實 grounding。
2. 固定相機 + visual tag 起步，先建立 ground truth。
3. 第一版不需要讓 LLM 做控制；CLI + structured API 足以證明 self-model。
4. L0–L2 穩定後才加入 VLM；L3 穩定後才加入 learned policy。
5. 每升一級 autonomy，先新增 fault tests 與 stop mechanism。
