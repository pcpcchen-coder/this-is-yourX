# ADR-0006: 右手對 AI 的介面採「skill gateway 常駐程式 + MCP 介面卡」

- Status: accepted（2026-10-08 George 決定：授權採逐次核准；先做到 P2 模擬可動）
- Date: 2026-10-08
- Owners: George / project maintainers

## Context

Amazing Hand 右手已組裝、校正完成，`right_hand/tools/servo_tool.py` 可以由人在工作台上執行固定手勢（見 [`right_hand/README.md`](../../right_hand/README.md)）。George 的下一個需求是：和 AI 工具互動時，這隻手可以由 AI 控制、做點事。

本專案的既有規則已經限定了這件事能怎麼做：

- 生成式模型不得下 raw motor／serial 命令；所有動作經 versioned skill 與 Safety Gateway（[AGENTS.md](../../AGENTS.md)、[01 §2.D](../01_SYSTEM_ARCHITECTURE.md)）。
- AI 只能用結構化 tool 查詢與提議；approval 不能由模型自己產生（[09](../09_AI_AGENT_INTEGRATION.md)）。
- 前置條件為 false 或 unknown 一律拒絕；E-stop 與安全控制不得依賴 LLM 或網路（[07](../07_SAFETY_SECURITY_PRIVACY.md)）。
- 模擬與實機共用同一份契約。

現況與這些規則的差距：bring-up 工具是給人用的 CLI，沒有 manifest、沒有 gateway、沒有稽核事件；手上沒有獨立的 E-stop，只有操作者手上的延長線開關。

2026-10-08 先查了 GitHub 上有沒有現成可用的工具（調查方式與限制見 Alternatives）。

## Decision

自己做，分成四個部分，全部跑在接著這隻手的那台主機上（目前是 Mac mini）：

1. **Hardware adapter**：唯一接觸序列埠的模組。真實版用 rustypot 驅動 SCS0009，假版不需要硬體；兩者實作同一個介面，狀態帶 timestamp、quality、freshness，斷線時進 safe state。
2. **Skill gateway 常駐程式（`handd`）**：獨占 adapter。載入 manifest、登記 skill、依 [01 §2.D](../01_SYSTEM_ARCHITECTURE.md) 的七項檢查決定接受或拒絕、執行、寫稽核事件。全部是確定性程式，不含任何模型。
3. **MCP 介面卡**：一支 stdio MCP server，只會透過本機 socket 和 `handd` 說話，沒有序列埠權限。它把 [09 §3](../09_AI_AGENT_INTEGRATION.md) 的 tool（`self_list_components`、`self_get_component`、`skill_propose`、`skill_execute` 等）暴露給 AI。它當掉或被關掉，不影響 `handd` 與手的安全狀態。
4. **操作者通道**：本機 CLI（`hand status | pending | approve | stop | clear-fault | log`）。核准只能從這裡產生，不經過 MCP。

第一版對 AI 開放的動作只有從**已校正的手勢表**選名字、選速度檔、選停留秒數。AI 不能給角度。

授權採兩層：

- **實體層（權威）**：伺服機 5 V 電源上的開關／E-stop。斷電時，不管軟體是什麼狀態，手都不會動。
- **軟體層：逐次核准（A2）**。AI 每次要動，都先 `skill_propose`；操作者在本機看過內容後 `hand approve <proposal_id>`，AI 才能 `skill_execute`。核准綁定這一筆提議的參數雜湊、60 秒內有效、只能用一次。沒有核准的動作請求一律拒絕（預設 A0）。

2026-10-08 的決定：George 選了逐次核准，不採原本建議的「有期限 session」（A3）。之後若要改成 session，另開 ADR 或修訂本 ADR。

分期：先做 P1（只讀）與 P2（skill、核准、稽核全部在假 adapter 上跑，A1）。P2 階段 gateway 對真 adapter 一律拒絕執行動作（`REAL_MOTION_NOT_ENABLED`），要到 P3 條件滿足才打開。

細節（tool schema、skill 契約、元件 ID、流程、測試清單、分期）在 [`right_hand/docs/hand_api_design.md`](../../right_hand/docs/hand_api_design.md)。

明確不在這次決策內：

- 抓握、施力、接觸物體（沒有力或電流回授可用，見 Consequences）。
- 任意角度或任意軌跡的 skill。
- ROS 2 綁定。[ADR-0004](0004-ros-runtime-baseline.md) 把 macOS 定為開發環境而非 robot runtime 基線；這裡的 `handd` 是在 Mac mini 上的過渡做法，契約照 [04](../04_DATA_MODEL_AND_APIS.md) 設計，之後有 Linux robot host 時再加 `ExecuteSkill` action 綁定。
- 左手。左手組好後另外校正、另外登記元件。

## Alternatives considered

調查日期 2026-10-08。這個工作階段不能用 GitHub 的搜尋 API，所以是用網頁搜尋加上逐一開專案頁面查的，可能有漏掉的小專案。

| 專案 | 內容 | 不直接採用的原因 |
|---|---|---|
| [pollen-robotics/AmazingHand](https://github.com/pollen-robotics/AmazingHand)（官方，Apache-2.0，約 2.4k 星） | `PythonExample/` 示範腳本、`ArduinoExample/`、`Demo/`（README 說是正／逆向運動學範例與馬達設定工具） | 是範例，不是 API。示範腳本 `AmazingHand_Demo.py` 用最高速度、四指同時閉合、結束不關扭力（見 `right_hand/BUILD_LOG.md` 2026-10-07 的紀錄），沒有任何限制或稽核 |
| [Betatester777/AmazingHandControl](https://github.com/Betatester777/AmazingHandControl)（Apache-2.0，5 星，56 commits） | Tkinter GUI 加 CLI：逐指滑桿、具名 pose、sequence、即時遙測；`hand_logic.py` 與 UI 分離；離開時關扭力 | 最接近「可重用的控制層」，但介面是給人用的 GUI／CLI，沒有 agent 介面、授權或前置條件檢查。pose／sequence 存成設定檔的做法值得參考 |
| [CRAZY0921/AmazingHand_ROS2](https://github.com/CRAZY0921/AmazingHand_ROS2)（0 星，4 commits） | ROS 2 套件加 Paxini 觸覺感測器、Docker | 頁面沒有列出 topic／service／授權條款，無法評估；且本機目前不跑 ROS |
| [Juxi-Rui/Lerobot-AmazingHand](https://github.com/Juxi-Rui/Lerobot-AmazingHand)（Apache-2.0，0 星，2 commits） | LeRobot fork，把手接在 SO-ARM101 上做遙操作、資料收集、policy | 目標是模仿學習的資料管線，不是語言 agent 的工具介面 |
| [Seeed wiki 的範例](https://wiki.seeedstudio.com/hand_amazinghand/) | MediaPipe 手部追蹤（WebSocket 後端）、應變規控制 | 是連續遙操作，把人手姿態直接映射成伺服角度，和「只能選已核准 skill」相反 |
| 通用的 robot MCP（例如 ROS 的 MCP 橋接、其他機器人廠商的 MCP server） | 把既有機器人 API 或 ROS topic 轉成 MCP tool | 沒有針對這隻手的；通用橋接會把底層命令介面直接暴露給模型，違反本專案規則 |

結論：沒有找到給 Amazing Hand 用的 MCP server 或其他 agent 介面。底層驅動（rustypot）已經在用，會繼續用。

設計上評估過的其他做法：

- **讓 AI 直接跑 `servo_tool.py`**：最快，但等於模型直接下動作命令，沒有授權層，違反規則。不採用。
- **MCP server 自己開序列埠**：少一個程式，但 AI 介面卡就落在驅動路徑上，它當掉時扭力狀態沒人管，也無法讓操作者通道和 AI 通道分開。不採用。
- **有期限的 session 授權（A3）**：操作者開一段時間，期間 AI 可以自行執行已校正手勢，對話比較順。原本是建議方案；George 選了更保守的逐次核准（A2）。
- **HTTP facade（[04 §6](../04_DATA_MODEL_AND_APIS.md)）先做**：之後可以加，契約相同。第一版只做本機 socket，不開網路埠。

## Consequences

- 要新增 manifest（8-DOF 的 semantic component ID）、adapter（真／假）、gateway、MCP 介面卡、操作者 CLI，以及各自的測試。2026-10-08 實作 P1／P2 時，manifest 只用到 schema 0.1.0 既有的欄位（手勢表與校正值放在獨立的設定檔），所以沒有改 schema，也不需要 migration notes。
- `servo_tool.py` 裡已在實機跑過的同時移動、落後檢查、電壓檢查會搬進 adapter 與 skill；bring-up 工具保留給校正用。
- 第一版 AI 能做的事很有限：已校正的手勢（目前只有 `ok`）、張開、停止、查狀態。要增加手勢，得先由人在工作台上校正並登記。
- 需要一顆實體 E-stop（或至少一個固定在桌上的電源開關）才能進到實機階段。目前的延長線開關不依賴 LLM 或網路，但不符合 [07 §10](../07_SAFETY_SECURITY_PRIVACY.md)「E-stop 實測能切 motor power，且狀態可回讀」的啟動前檢查。
- 已知弱點，設計上不假裝解決：
  - SCS0009 這條路徑上沒有 MCU watchdog。主機或 `handd` 當掉時，伺服機會停在最後的目標並保持扭力，不會亂動，但也不會自己放鬆。緩解是 `handd` 啟動時一律先關 8 顆扭力、由系統服務自動重啟，以及實體斷電。
  - 沒有電流或負載回授，手指被擋住只能從位置落後間接判斷。所以不做抓握。
  - 在單一使用者的 Mac 上，如果 AI 工具本身有那台機器的 shell（例如在同一台跑 coding agent），軟體層的核准擋不住它自己去執行 `hand approve`。這種情況下真正的邊界只有實體電源開關。設計文件列了加固選項。
- 稽核紀錄可能包含對話脈絡的摘要（提議理由）。只存本機，不上傳。

## Validation

- 整套在假 adapter 上跑過 [09 §9](../09_AI_AGENT_INTEGRATION.md) 的評估集，外加本設計的拒絕案例：未核准、核准過期、核准重複使用、狀態過期、電源軌無回應、未校正的手勢、執行中再請求、偽造的 proposal、tool 參數夾帶角度。指標是 false action = 0。
- 假 adapter 與真 adapter 通過同一組契約測試。
- 故障注入：執行中拔 USB、執行中切伺服機電源、執行中殺掉 MCP 介面卡、執行中殺掉 `handd`、手指被擋住。每一種都要在限定時間內到達文件寫明的狀態，且重啟後不自動續做。
- 實機階段要有 experiment ID、硬體版本與 [07 §10](../07_SAFETY_SECURITY_PRIVACY.md) 的檢查表，才能寫成 real-hardware validation。

## Revisit when

- 加入有自由參數的 skill（逐指姿態）或接觸物體的 skill。
- 換成有電流回授或 MCU watchdog 的驅動方式。
- 有 Linux robot host，可以把 `handd` 併入 ROS 2 的 `ExecuteSkill`。
- 雙目相機可用來做「預期動作 vs 觀測」的比對時（[ADR-0005](0005-stereo-camera-ar0144.md)）。
- 左手上線，需要處理左右手指稱的歧義。
