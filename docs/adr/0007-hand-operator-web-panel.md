# ADR-0007: 右手加一個給操作者用的網頁面板（逐指操作、存下與重現姿勢）

- Status: proposed（2026-10-08 依 George 的要求實作；是否接受、是否開機自動啟動由 George 決定）
- Date: 2026-10-08
- Owners: George / project maintainers

## Context

右手已從 Mac mini 移到一台 Raspberry Pi 4（Debian 13，驅動板在 `/dev/ttyACM0`）。George 要在這台 RPi 上有一個網頁，可以逐指控制這隻手，並把姿勢存下來供之後重現。

現有的兩條路都不合用：

- `tools/servo_tool.py`／`finger_cal.sh` 是 CLI，姿勢寫死在程式裡；要調一個新姿勢得改檔、重跑（`gesture ok` 當初就調了四輪）。
- `hand_api`（[ADR-0006](0006-hand-skill-api-mcp.md)）是給 AI 用的，刻意不接受角度，而且 P2 階段對實機一律拒絕動作。

限制沿用既有規則：生成式模型不得下 raw motor 命令；前置條件為 false 或 unknown 一律拒絕；停止與安全狀態不得依賴 LLM 或網路；模擬與實機共用同一份契約（[AGENTS.md](../../AGENTS.md)、[07](../07_SAFETY_SECURITY_PRIVACY.md)）。手上仍然沒有獨立的 E-stop，只有延長線開關。

## Decision

加一個**給人用**的網頁面板 `right_hand/hand_panel/`，定位和 `servo_tool.py` 相同：工作台上的人工操作工具，不是 skill，也不是 AI 的控制路徑。

- 一條 worker 執行緒獨占 adapter；HTTP 那一側只改「想要的狀態」。沿用 `hand_api.adapter`（真／假 adapter、跨程式的匯流排鎖）與 `hand_api.motion.move_together`（分輪前進、落後與電壓檢查、到位確認）。
- 上電與啟動維持扭力關；要人按「啟用扭力」（兩次）才開，而且先把目標設成目前位置。
- 一次只有一個頁面能控制；任何開著的頁面都能停止。
- 看門狗：控制者的頁面 3 秒沒有回報就關扭力；閒置 60 秒關扭力；任何檢查沒過就關扭力並鎖住 fault，要人解除。
- 沒有頁面開著時不佔匯流排，bring-up 工具與 `handd` 照常可用。
- 姿勢存成 `right_hand/config/poses.yaml`，格式和 `gestures.yaml` 的 `pose` 相同，記下校正版本；校正改了就要人重新確認。
- 存取控制：區域網路上的明文 HTTP，**預設沒有登入**。最初的實作有一組存取碼；2026-10-08 George 決定拿掉，理由是只在自己的內網使用。存取碼保留成選項（`--require-token`）。不論有沒有存取碼，POST 都要同源的 `Origin` 與 JSON，`Host` 要是 IP 位址或這台機器的名稱（擋跨站請求與 DNS rebinding）。

細節在 [`right_hand/docs/hand_panel.md`](../../right_hand/docs/hand_panel.md)。

明確不在這次決策內：

- **不改變 ADR-0006。** AI 仍然只能經 `handd` 提議已校正的手勢並逐次核准；`poses.yaml` 不被 `hand_api` 讀取。面板的 HTTP 介面接受角度，不得接到任何模型或 agent 上。
- 不把面板的「停止」當成 E-stop。
- 不做抓握、接觸物體、手勢序列、相機畫面、左手。
- 不把 `handd`／MCP 搬到 RPi 上；那是另一件事（ADR-0006 的 Revisit 條件之一「有 Linux robot host」現在成立了，值得另外檢討）。

## Alternatives considered

- **用現成的 GUI**（[Betatester777/AmazingHandControl](https://github.com/Betatester777/AmazingHandControl)，Tkinter，逐指滑桿與具名 pose，ADR-0006 調查時看過）：功能最接近，但它是桌面視窗程式，RPi 目前開機到文字模式、沒有接螢幕；也沒有本 repo 的匯流排鎖、落後檢查與 fault 鎖定。沒有實際裝起來試。不採用。
- **面板經過 `handd` 的 gateway**：只有一條控制路徑比較乾淨，但 gateway 的契約是「不接受角度、每個動作逐次核准、實機動作尚未開放」，要讓滑桿可用就得替操作者開一組接受角度的方法，等於在 AI 用的 gateway 裡加一條旁路。分成兩支程式、靠同一個匯流排鎖互斥，界線比較清楚。不採用。
- **只聽 127.0.0.1，靠 SSH tunnel**：少一個網路面，但 George 要的是直接開網頁；手機上也不方便。保留成選項（`--host 127.0.0.1`）。
- **用網頁框架（Starlette、Flask）**：`mcp` 的相依套件裡已經有 Starlette，但那是別人的間接相依；標準函式庫的 `http.server` 對這個量級足夠，不增加相依。

## Consequences

- 多了一條**接受任意角度**（在限制內）的實機動作路徑，而且是在區域網路上可達的。風險與緩解：
  - 同網段上連得到這個連接埠的任何裝置都能動這隻手，不需要任何憑證（George 接受的風險）。緩解：只用在自己的工作台網路；啟用扭力要按兩次；看門狗與閒置逾時；實體開關。[07 §2](../07_SAFETY_SECURITY_PRIVACY.md) 把「未授權控制」列為 hazard、[07 §7](../07_SAFETY_SECURITY_PRIVACY.md) 建議把 actuator network 和一般 LAN 分開；這個決定在這一點上比基線寬鬆，是有意的。
  - 在這台主機上有 shell 的程式（含 AI agent）能直接呼叫面板，軟體擋不住。和 ADR-0006 記錄的弱點相同，邊界仍是實體電源。
  - 側擺在各種彎曲角度下會不會讓手指互碰沒有驗證；靠落後檢查間接擋，停之前已經出了力。
- 面板和 `handd` 是兩支程式、兩份稽核紀錄（`panel_events.jsonl`、`handd_events.jsonl`）。同一時間只有一支拿得到匯流排。
- 面板被強制結束時伺服機保持扭力（沒有 MCU watchdog，和 ADR-0006 相同）。緩解：systemd 的 `ExecStopPost` 補送關扭力；整台主機當掉時只有實體開關。
- `poses.yaml` 是執行時由面板寫入、又放在 Git 追蹤的 `config/` 下的檔案；要不要提交由操作者決定。
- 新增 133 項測試；原有 148 項不變。

## Validation

- 已做（2026-10-08，RPi 上）：`pytest right_hand/tests -q` → 281 passed。面板的測試全部在假 adapter 或假匯流排上：拒絕案例不寫出任何目標或扭力；卡住、電壓掉落、掉線、扭力自己關掉、過溫、頁面不見、閒置、停止、結束；姿勢檔的格式、覆蓋、校正版本、壞檔不覆蓋；HTTP 的跨來源、別的主機名稱、多餘欄位，以及有、沒有存取碼兩種模式。
- 已做（bring-up 觀察，沒有 experiment ID）：實機上只讀——頁面開著時讀到 8 顆、扭力皆關、沒有寫入；頁面關掉後 `servo_tool.py scan` 可用。模擬上用瀏覽器走過一次啟用、拖動、存檔。
- 已做（bring-up 觀察，沒有 experiment ID；依據是面板的稽核紀錄）：2026-10-08 George 在實機上操作約半小時，151 段移動中 150 段到位（三個速度檔都用過），存了 5 個姿勢並重現 34 次。一次快速檔下拇指落後超過上限（19.3° 對 19°），面板關扭力並鎖住，8 顆都關成功。閒置逾時在實機上觸發過 3 次。細節在 [`hand_panel.md`](../../right_hand/docs/hand_panel.md) §12。
- **還沒做**：實機上的故障注入（移動中關頁面、拔 USB、切電源、殺面板）。做完並有 experiment ID、硬體版本與 [07 §10](../07_SAFETY_SECURITY_PRIVACY.md) 的檢查表之前，不能寫成 real-hardware validation。

## Revisit when

- 裝了固定的實體開關或 E-stop（可以把「停止」接到能回讀的硬體上）。
- 要讓面板在工作台網路以外可達（那時至少要 TLS 與真正的登入）。
- 這個網路上開始有不完全信任的裝置或人（訪客 Wi-Fi、共用辦公室）：那時把 `--require-token` 開回來，或把 RPi 移到獨立的網段。
- 要把面板存的姿勢自動變成 AI 可用的手勢（目前刻意要人登記）。
- `handd` 搬到 RPi、或加入 ROS 2 runtime 時，重新檢討兩支程式要不要合併成一個 gateway 的兩種身分。
