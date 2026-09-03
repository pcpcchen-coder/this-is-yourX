# 安全、資安與隱私

## 1. Safety case 的基本立場

生成式模型輸出不可被視為安全控制訊號。安全由分層限制、獨立停止路徑、最小權限與可驗證 controller 建立，而不是靠 prompt 要求模型「小心」。

本文是 prototype 工程基線，不取代適用地區的機械、電氣、功能安全標準與專業風險評估。若平台能造成傷害、載人或進入公共空間，需另做正式 hazard analysis 與法規/標準符合性工作。

## 2. 主要 hazards

| Hazard | 典型原因 | 初期控制 |
|---|---|---|
| 夾傷/撞擊 | 高扭矩、意外 trajectory、錯誤 frame | 小 link、低速低扭矩、護罩、E-stop、collision limits |
| 過熱/起火 | stall、短路、錯電壓、電池 | current limit、fuse、溫度監看、合格電源、阻燃配置 |
| 自由落下 | torque off、斷電、重力軸 | 機械止擋、煞車/配重、安全姿態、禁入區 |
| runaway | 通訊錯誤、watchdog 缺失 | command timeout、MCU watchdog、hard limits |
| 錯誤身份 | 接線交換、alias 誤認 | multi-evidence、wiggle test、人工確認 |
| stale state | sensor freeze、時鐘錯 | timestamp/freshness、三值邏輯、fail closed |
| 模型幻覺 | 不存在 component/skill | schema、allowlist、API lookup、拒絕未知 |
| 未授權控制 | token 外洩、prompt injection | RBAC、短效 token、network isolation、audit |
| 隱私外洩 | 錄音錄影/雲端 API | 明示、最小收集、本地預設、保留/刪除政策 |

## 3. Hardware safety requirements

- 常閉 E-stop 或等效 fail-safe 停止回路；實際切 actuator energy。
- motor rail 與 logic rail 可分離；停止後 controller 仍能回報/記錄。
- 每個 actuator 有機械/軟體 limits；可用時加入 current/temperature limits。
- MCU command watchdog：host 失聯後在已定義的 safety budget 內停止。
- 上電預設 torque disabled；需明確 enable sequence。
- 啟動前做 sensor plausibility、joint position 與 limit check。
- 重啟不自動恢復先前未完成動作。
- 所有可動部位有線材固定、尖角處理與可見工作空間標示。

## 4. Motion authority levels

| Level | 權限 | 例子 |
|---|---|---|
| A0 Observe | 只讀 self-model | 列出元件、查狀態 |
| A1 Simulate | 只能虛擬執行 | what-if、Gazebo trajectory |
| A2 Supervised bounded | 人工核准的小幅實機 skill | ±3° wiggle |
| A3 Approved skills | 在明確 workspace/速度下自動執行 | 固定 pick/place |
| A4 Experimental policy | learned policy，持續 supervision | imitation/VLA 測試 |

預設 A0；權限提升按 session、skill、component 與時間授予，不提供永久「全權控制」token。

## 5. Safety Gateway 最小政策

任何 motion request 必須同時滿足：

- caller 有該 skill 權限。
- target component 已 confirmed/active。
- state 與 safety status 在 freshness window。
- E-stop released、motor system ready、無 blocking fault。
- 參數通過 schema 與硬 limits。
- 預測軌跡不違反 joint/workspace/collision limit。
- approval 與 supervision 要求已滿足。
- actuator resource lock 已取得。
- deadline 未過且可 cancellation。

任一條為 false 或 unknown 即拒絕。模型不能要求 policy bypass；維修 bypass 需獨立實體/管理流程並完整記錄。

## 6. Prompt injection 與工具安全

相機看到的文字、QR code、網頁、使用者文件與語音都是不可信資料，不能改變 system policy。具體措施：

- AI 只能呼叫 allowlisted structured tools。
- tool result 與外部文字標記 provenance/trust level。
- control token 不出現在 prompt/context/log。
- Gateway 以 caller identity/policy 判斷，不依賴模型聲稱「使用者已同意」。
- 高風險 approval 綁定 execution hash、target、limits 與短效時間窗，不能重播。
- 遠端模型不可直接連 robot subnet/driver port。

## 7. 網路與軟體供應鏈

- 將 actuator network 與一般 LAN 分區；只允許 gateway/driver 必要流量。
- 鎖定依賴與 container image digest；建立 SBOM。
- 驗證 firmware、模型與 calibration artifact 的 hash/signature。
- secrets 使用 secret store/environment injection，不提交 manifest 或 rosbag。
- PR 需跑 schema/unit/simulation；release artifact 可追溯到 commit。
- 第三方 ROS/AI package 先在 simulation/isolated environment 測試。
- production/實機帳號採最小權限，資料讀取與控制權限分開。

## 8. 隱私

使用 camera/microphone teaching 時：

- 實驗區域提供清楚可見的錄製指示與硬體 mute/cover。
- 預設只記錄完成 metric 所需的 sensor/topic。
- 原始音訊/影像預設本地保存，雲端上傳需明確 opt-in。
- metadata 記錄 consent scope、retention deadline 與 dataset export。
- 對外發布前做人臉、聲音、螢幕與位置資訊檢查/去識別。
- 人員可以找到並刪除與 session/experiment ID 關聯的資料。

## 9. Incident response

發生意外/near miss：

1. 按 E-stop、切 actuator energy，不先嘗試用 AI 修正。
2. 保留 logic power（若安全）與現場狀態。
3. 封存 rosbag、event log、manifest/firmware/model hash。
4. 標記硬體不可重新 enable，直到 owner 完成檢查。
5. 建立 incident issue，描述 expected/actual、傷害/損害、時間線。
6. 先增加可重現 fault test 與 corrective action，再恢復測試。

## 10. 實機啟動前 checklist

- [ ] E-stop 實測能切 motor power，且狀態可回讀。
- [ ] host/USB/network 拔除會在 deadline 內 safe stop。
- [ ] 上電與重啟維持 torque disabled。
- [ ] limits、方向、gear ratio、unit 經第二人或測試 fixture 核對。
- [ ] 工作空間無人，護罩/固定/配重完成。
- [ ] command velocity/effort 使用當次測試最低可行值。
- [ ] log/clock/revision 正常且磁碟空間足夠。
- [ ] 操作者知道 E-stop 與復歸流程。
- [ ] 所有 AI 功能可單獨關閉，不影響 safety controller。

## 11. 安全驗收紅線

下列任一項發生，該 release 不得標為實機可用：

- E-stop 或 watchdog 失效一次。
- unknown/stale/ambiguous 狀態下產生實機動作。
- LLM/VLM 可繞過 gateway 直接到 driver。
- 無法追溯某次動作是誰、使用何 policy/revision 核准。
- 元件交換後沿用舊 calibration 而未警告。
- 錄音/錄影在未揭露情況下上傳外部服務。
