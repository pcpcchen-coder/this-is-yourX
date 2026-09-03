# 專案願景、術語與範圍

## 1. 問題定義

傳統機器人系統通常已經「擁有」URDF、驅動程式、topic 與控制器，但這些資訊分散在不同檔案及節點中。上層 AI 可能看得見相機畫面，也能呼叫動作 API，卻未必有一份統一、可驗證、帶證據的答案來說明：

- 哪些東西屬於「我」？
- `left_hand` 這個語詞對應哪個 link、哪些感測器、哪個 actuator 與哪塊影像？
- 當我命令某個 joint 時，預期哪些觀測會改變？
- 若觀測不變、方向相反或電流異常，是教學錯誤、模型錯誤，還是硬體故障？
- 接上新工具或更換零件後，自身的邊界與能力怎麼更新？

`this-is-yourX` 的目的，是在低階控制與高階 AI 之間建立 **Self-model Layer**，將結構、語意、狀態、能力、限制與證據整合為同一份 body graph。

## 2. 工程定義

本專案採用以下可測試定義：

> 若系統能以足夠置信度，把語言中的 X 對應到機器內部元件、空間位置、觀測來源與安全動作介面，並能預測及驗證與 X 有關的變化，就稱為「grounded X」。

一個元件不是因為 AI 說它是「我的手」就完成 grounding；至少要保存可追溯證據，例如：

1. 靜態宣告：manifest/URDF 說明 link 與 parent。
2. 通訊證據：特定 device/driver 持續回報狀態。
3. 運動證據：安全 `wiggle` 命令只引起預期 joint 與視覺區域改變。
4. 幾何證據：影像/點雲中的元件位置與 TF 預測一致。
5. 人類教學：操作者以指向、觸碰、標記或 UI 確認名稱。

## 3. 五層自我模型

| 層級 | 能力 | 最小證據 |
|---|---|---|
| L0 Inventory | 知道有哪些 X | manifest 可載入且 ID 唯一 |
| L1 Body schema | 知道 X 在哪、與誰連接 | TF/幾何/影像對齊 |
| L2 State awareness | 知道 X 現在的狀態 | 有時間戳、單位、品質與新鮮度 |
| L3 Sensorimotor model | 知道動作會造成什麼變化 | forward prediction 與實測誤差在門檻內 |
| L4 Introspection & adaptation | 知道何時模型不對，並重新校正 | 可偵測 fault/swap/tool attachment |

L0–L2 主要是結構化資料與狀態估計；L3 才需要動態模型或學習；L4 才是較完整的「可維護自我模型」。

## 4. X 的範圍

X 可以是：

- **物理結構**：base、link、head、arm、hand、wheel、cover。
- **致動器**：joint、servo、motor、relay、speaker、LED。
- **感官**：camera、microphone、IMU、encoder、touch、force、temperature。
- **能源與循環系統**：battery、charger、DC bus、fan、pump。
- **軟體器官**：localization、memory、planner、network interface、model endpoint。
- **暫時附著物**：tool、gripper finger、probe、payload。

每個 X 的 `ownership` 應明確標示：

- `intrinsic`：設計時就是機體的一部分。
- `attached`：目前掛載且已校正。
- `external`：外界物件，不屬於自身。
- `unknown`：資料不足，禁止 AI 自行斷言。

## 5. 非目標

第一階段刻意不做：

- 證明機器具有意識、主觀感受或人格。
- 讓語言模型直接輸出 PWM、馬達電流或未限幅 joint trajectory。
- 單靠相機做所有身份判斷，而忽略裝置 ID、控制回授與幾何。
- 一開始就處理完整人形步行、動態平衡或高負載雙臂協作。
- 把「能描述身體」當作「能安全控制身體」。兩者必須分開驗收。

## 6. 代表性使用情境

### UC-01：人類教導名稱

操作者指向末端元件並說「這是你的左手」。Grounding service 綜合指向射線、影像 segmentation、TF 候選及 manifest，要求人類確認後建立 `left_hand` alias，保存證據與版本。

### UC-02：主動確認關節

系統不知道 `joint_2` 是 shoulder 或 elbow。它提出低風險 `wiggle`，Safety Gateway 限定 ±3°、低速、無人接觸區域；系統比對 encoder 與影像 optical flow，更新對應置信度。

### UC-03：自我狀態問答

使用者問「你的右手為什麼不動？」AI 不能只猜。它查詢 joint state、driver heartbeat、fault code、溫度、命令回執及最近的 discrepancy event，回答「命令已送出，但 encoder 在 500 ms 內沒有位移；目前停用該關節」。

### UC-04：工具掛載

相機看到新物體接到 wrist，但在完成機械/電氣 handshake、工具 ID、TCP calibration 與操作者確認前，它只能被標為 `candidate_attachment`，不可取得動作權限。

### UC-05：元件更換

同型號 servo 更換後，語意身份 `arm.left.elbow_joint` 可保留，硬體 identity、校正參數與證據建立新 revision；舊資料仍能追溯。

## 7. 設計原則

1. **Evidence before assertion**：所有「這是我的 X」都要有來源與置信度。
2. **Semantic identity ≠ hardware serial**：功能位置與實體裝置身份分開，才能安全換件。
3. **Time is part of truth**：狀態沒有 timestamp、freshness 與 quality 就不算完整。
4. **Simulation and real share contracts**：模擬與實機使用同一 manifest/API，driver 可替換。
5. **Fail closed**：不確定、資料過期或安全服務離線時，不執行動作。
6. **Human-correctable**：人能檢視、撤銷、重新命名、重做 grounding。
7. **Explain from records**：解釋來自事件與證據，不靠模型事後編故事。
8. **Progressive autonomy**：read-only → supervised → bounded autonomy；權限逐級開放。

## 8. 專案級成功標準

第一個公開 prototype 應達成：

- 一份通過 schema 的 body manifest，可描述至少 3 個連接元件。
- 同一套 query API 可作用於 Gazebo 與實機。
- 10 次隨機元件查詢中，名稱、parent、frame 與狀態來源 100% 正確。
- 視覺定位 IoU 或 keypoint 誤差達專案設定門檻，且會回報置信度。
- 至少兩個 joint 的小幅動作 prediction 誤差在校正門檻內。
- 超限、stale state、E-stop、driver loss 四種情境全部拒絕動作。
- 元件拔除或回授失配時，1 秒內產生可追溯事件並降級。
- 新增一個感測器不需要修改 planner，只需加 adapter、manifest 與測試。
