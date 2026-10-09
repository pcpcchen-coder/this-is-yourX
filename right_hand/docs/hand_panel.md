# 右手的網頁操作面板（hand_panel）

人在瀏覽器上逐指操作右手、把姿勢存下來、之後重現。決策見 [ADR-0007](../../docs/adr/0007-hand-operator-web-panel.md)（proposed）。

> **狀態（2026-10-08）**：已實作，`pytest right_hand/tests -q` → 281 passed（其中面板 133 項，全部跑在假 adapter 或假匯流排上）。
> **實機（bring-up 觀察，沒有 experiment ID）**：在 Raspberry Pi 4 上以 `--adapter scs` 執行。2026-10-08 George 從瀏覽器操作了約半小時：151 段移動，150 段正常結束（慢 67、中 40、快 43），存了 5 個姿勢並重現 34 次；另外一段在快速檔下拇指落後超過上限，面板照設計關扭力並鎖住。紀錄見 §12。
> 實機上的故障注入（移動中關頁面、拔 USB、切電源、殺面板）還沒做。

## 1. 這是什麼、不是什麼

- 是給「人」用的工作台工具，和 `tools/servo_tool.py`、`finger_cal.sh` 同一類。操作者是坐在手旁邊、手放在電源開關上的人。
- **不是 AI 的控制路徑。** AI 的動作仍然只走 `hand_api` 的 skill gateway（[ADR-0006](../../docs/adr/0006-hand-skill-api-mcp.md)）：從已校正的手勢表選名字、逐次核准。面板的 HTTP 介面接受角度，所以不得接到任何模型或 agent 上。
- 面板存下的姿勢（`config/poses.yaml`）不會被 `hand_api` 讀取，也就沒有對 AI 開放。要讓 AI 能用某個姿勢，仍然照 [`hand_api_design.md`](hand_api_design.md) §5 由人登記到 `gestures.yaml`；兩個檔的 `pose` 格式相同，可以直接抄。

## 2. 啟動與網址

```bash
bash right_hand/tools/hand_panel.sh                  # 模擬（預設），Ctrl-C 結束
bash right_hand/tools/hand_panel.sh --adapter scs    # 接實體伺服機
bash right_hand/tools/hand_panel.sh url              # 印出網址
```

Raspberry Pi 上已裝成 systemd 使用者服務（`tools/hand-panel.service`，連接埠 8765）：

```bash
systemctl --user status hand-panel       # 看狀態
systemctl --user restart hand-panel      # 改了程式或設定之後
systemctl --user stop hand-panel         # 停掉（有扭力會先關）
systemctl --user enable hand-panel       # 之後登入時自動啟動；目前沒有開，由操作者決定
journalctl --user -u hand-panel -n 50    # 看輸出
```

**預設不需要存取碼**（2026-10-08 George 決定：只在自己的內網使用）。同一個區域網路上開得了這個網址的人，都能啟用扭力、移動手指。

要加回存取碼：啟動時加 `--require-token`（服務的話改 `hand-panel.service` 的 `ExecStart`），網址用 `hand_panel.sh url --require-token` 取得。
第一次用含 `?token=…` 的網址開啟，之後這個瀏覽器 30 天內靠 cookie；沒有存取碼的請求一律 401。存取碼存在 `~/.this-is-yourx/hand/panel_token`，刪掉後重啟就換一組。

## 3. 第一次在實機上用

1. 伺服機的 5 V 變壓器接在有開關的延長線上，**一隻手放在開關上**。手的活動範圍內沒有東西。
2. 開頁面。頂列應該是「實機：8 顆都有回應，…V，最高 …°C」，四根手指的琥珀色橫線是現在讀到的位置。
3. 速度選「慢」。按「啟用扭力」，再按一次確認。這時手指應該**不動**，只是變硬（目標先設成目前位置才開扭力）。
4. 先只動一根手指、只動一點：把食指的長條往下拖 10° 左右，放開。看手指跟著走、琥珀線跟上藍色把手。
5. 逐一試其他手指與側擺，再試「全部張開」。任何不對勁：切電源。
6. 按紅色的「停止」（或 Esc 鍵），確認手指變鬆。

`中`、`快` 兩檔沿用 `hand_api.motion.SPEEDS`。2026-10-08 三檔都在實機上用過；`快` 檔出現過一次拇指落後超過上限而停下（§12）。沒走過的姿勢先用慢。

## 4. 畫面

- **停止**：關掉 8 顆扭力。任何開著的頁面都能按，鍵盤 Esc 也可以。它經過網路與面板程式，**不是 E-stop**；實體開關才是。
- **扭力**：啟用（要按兩次）、關扭力、速度。啟用後這一頁成為控制者；其他頁面只能看和按停止。狀態、閒置倒數、說明各有固定大小的位置，換字時下面的手指區不會跟著移動。故障也顯示在這一區，同一顆按鈕變成「解除鎖定」；說明太長被截斷時點一下可以展開。
- **手指**：每根手指一條直的長條（彎曲，−30° 張開到 +90° 握）和一條橫的（側擺，±40°）。藍色是下給手的目標，琥珀色是讀回來的實際位置。拖動時手指跟著走；鍵盤方向鍵一次 1°、PageUp/PageDown 一次 10°。
- **姿勢**：取名稱（小寫英數與底線）與標籤，按「存下目前姿勢」。清單裡按「重現」會四指同時走到那個姿勢。`gestures.yaml` 裡已校正的手勢（目前是 `ok`）也列在這裡，只能重現、不能改。
- **伺服機讀值與操作紀錄**：每顆的位置、電壓、溫度、扭力，以及最近的事件。

存檔內容：扭力開著時存的是**目標角度**（`source: target`），所以重現時下的是同一組目標；扭力關著時存的是**讀到的位置**（`source: measured`）。兩種都另外記下存檔當下的讀值（`measured`）供對照。

## 5. 什麼情況會自動關扭力

| 情況 | 結果 | 之後 |
|---|---|---|
| 控制者的頁面超過 3 秒沒有回報（關頁、斷網、手機鎖屏、瀏覽器把背景分頁暫停） | 關扭力 | 重新啟用即可 |
| 有扭力但 60 秒沒有新目標（`--hold-timeout` 可調，5–600 秒） | 關扭力 | 重新啟用即可 |
| 移動途中任何一顆落後超過速度檔上限（12° / 15° / 19°）、電壓低於 4.0 V、掉線、沒到位（8° 內且停下） | 關扭力，鎖住 fault | 人看過後按「解除鎖定」 |
| 停著時讀到：有顆沒回應、8 顆都沒回應（電源被切）、電壓不在 4.0–7.4 V、溫度超過 60 °C、有顆的扭力自己關了（斷過電） | 關扭力，鎖住 fault | 同上 |
| 面板收到 SIGTERM／Ctrl-C | 關扭力後結束 | — |
| 面板被強制結束（SIGKILL、當掉） | **伺服機停在最後目標並保持扭力**；systemd 會跑 `hand_panel.safe_off` 補送一次關扭力 | 整台主機當掉時只有實體開關有用 |

移動沿用 `hand_api.motion.move_together`，也就是 `servo_tool.py gesture` 在實機上跑過的那一段：路徑切成很多輪，走最遠那顆每輪前進 3° / 6° / 10°，每輪讀回位置與電壓。拖滑桿時新目標會取消進行中的那一段，從目前讀到的位置重新起步。

啟動時面板不對伺服機寫入任何東西；沒按過「啟用扭力」就結束，也不寫。

## 6. 限制

- 彎曲 −30° 到 +90°、側擺 ±40°，而且兩顆伺服機各自都在 ±90°（含中位修正 ±95°）以內；超出的值會被收進範圍並在回應裡註明。彎曲的兩端是 bring-up 工具走過的張開與閉合；**側擺在各種彎曲角度下會不會讓手指互碰，沒有驗證過**，靠落後檢查擋。
- 沒有電流或力回授。手指頂到東西時，要落後超過上限才會停，停之前已經出了力。不要用它抓握或壓東西。
- 姿勢記下存檔時的校正版本（`calibration.yaml` 的 `revision`）。改了校正之後，舊姿勢會被拒絕，直到在清單裡按「重新確認」。
- `poses.yaml` 手改壞了（格式、範圍、名稱）時，面板拒絕讀寫並在頁面上指出原因，不會覆蓋它。

## 7. 和其他工具共用匯流排

面板用和 `servo_tool.py`、`handd` 同一個檔案鎖。**有頁面開著時面板佔著匯流排**，`finger_cal.sh`、`handd --adapter scs` 會回報被占用；關掉頁面 6 秒後放開。反過來，別的工具在跑時，頁面頂列會顯示「序列埠正被另一個程式使用」。

## 8. 資安

- **預設沒有登入這一層。** 區域網路上的明文 HTTP；連得到這台主機 8765 埠的任何裝置（同一個 Wi-Fi 上的電腦、手機、訪客、被入侵的 IoT 裝置）都能操作這隻手。只適合自己的工作台網路，不要做 port forwarding 或放到對外的網路上。只想本機用：`--host 127.0.0.1`，再用 SSH tunnel。
- 沒有存取碼時仍然擋掉的，是「別的網站借區域網路裡某台電腦的瀏覽器送請求」：
  - POST 要求 `Content-Type: application/json` 與同源的 `Origin`（一般的跨站請求過不了）。
  - `Host` 要是 IP 位址，或這台機器的名稱（主機名稱、`<主機名稱>.local`、`localhost`；其他名稱用 `--allow-host` 加）。這擋的是 DNS rebinding。
  - 頁面有 `Content-Security-Policy: default-src 'self'`。
- 加了 `--require-token` 時：存取碼存在主機上（0600），cookie 是 `HttpOnly; SameSite=Strict`；同網段能側錄封包的人仍然拿得到它。
- **在這台主機上有 shell 的任何程式（包括有 shell 的 AI agent）都能直接呼叫面板**，有沒有存取碼都一樣，軟體擋不住；和 ADR-0006 的結論一樣，實體開關是權威。
- 稽核紀錄：`right_hand/logs/panel_events.jsonl`（只增不改，不進 Git），每筆有時間、來源頁面與位址、內容。

## 9. HTTP 介面（給維護的人）

（加了 `--require-token` 時，所有請求要帶 cookie 或 `Authorization: Bearer <存取碼>`。）POST 的內容都要有 `client`（頁面自己產生的 8–40 個英數字元）；多出來的欄位一律拒絕。
應用層的結果都是 HTTP 200，看 `status`：`ok`／`accepted`／`rejected`（附 `reason_code`、`detail`）。

| 方法與路徑 | 內容 | 說明 |
|---|---|---|
| `GET /api/status?client=…` | — | 狀態；同時是這一頁的心跳 |
| `POST /api/enable` | `speed?` | 啟用扭力並成為控制者 |
| `POST /api/stop` | — | 關扭力；永遠接受 |
| `POST /api/fingers` | `fingers: {index｜middle｜ring｜thumb: {flex?, side?}}` | 設目標（度） |
| `POST /api/open` | — | 全部張開 |
| `POST /api/speed` | `speed` | 換速度檔（下一段移動起生效） |
| `POST /api/clear_fault` | — | 解除鎖定 |
| `GET /api/poses` | — | 姿勢清單（含 `gestures.yaml` 的手勢） |
| `POST /api/poses/save` | `name`、`label?`、`note?`、`overwrite?` | 存下目前姿勢 |
| `POST /api/poses/play`／`delete`／`reconfirm` | `name` | 重現／刪除／重新確認 |
| `GET /api/log?n=20` | — | 最近的稽核事件 |

## 10. 檔案

| 檔案 | 職責 |
|---|---|
| `hand_panel/core.py` | 狀態機與 worker：唯一碰匯流排的地方；啟用、移動、檢查、看門狗、關扭力 |
| `hand_panel/poses.py` | `poses.yaml` 的讀寫與格式檢查（寫入是整檔替換，不會留下寫到一半的檔） |
| `hand_panel/server.py` | HTTP 伺服器（只用標準函式庫）、來源檢查、選用的存取碼、進入點 |
| `hand_panel/safe_off.py` | 面板異常結束後補送關扭力 |
| `hand_panel/static/` | 頁面（`index.html`、`app.css`、`app.js`），沒有外部資源 |
| `tools/hand_panel.sh`、`tools/hand-panel.service` | 啟動腳本、systemd 使用者服務 |
| `tests/test_hand_panel.py` | 測試 |

## 11. 還沒做

- 實機上的故障注入（移動中關頁面、拔 USB、切電源、殺面板）。控制頁面不見時的看門狗在實機上還沒有觸發紀錄。
- 固定的實體開關／E-stop（ADR-0006 的未決事項，這裡同樣適用）。
- 手勢序列、逐指的速度、相機畫面。

## 12. 實機使用紀錄

依據是面板自己的稽核紀錄 `right_hand/logs/panel_events.jsonl`（不進 Git）。這些是 bring-up 觀察：沒有 experiment ID、沒有照片或錄影；手實際擺出來的樣子只有操作者看過。

### 2026-10-08（19:27–20:01，George 從同一個網段的瀏覽器操作）

| 項目 | 紀錄 |
|---|---|
| 啟用扭力 | 8 次（慢 5、快 1、中 2） |
| 移動 | 151 段；150 段到位並停下（慢 67、中 40、快 43）；單段最長 2.0 秒、最多 50 輪 |
| 到位後的差 | 目標與實際的差平均 1.5°、最大 7.0°（1200 筆；到位容許是 8°） |
| 沒過的檢查 | 1 次 `LAG_EXCEEDED`：快速檔，拇指 ID 7 在第 3／8 輪目標 +5.2°、實際 −14.1°（落後 19.3°，快速檔上限 19°） |
| 沒過之後 | 面板關掉 8 顆扭力（全部成功）並鎖住 fault；操作者 10 秒後解除鎖定，之後照常使用 |
| 關扭力 | 閒置逾時 3 次、按停止 4 次、fault 1 次；每一次 8 顆都關成功 |
| 姿勢 | 存了 `ya`、`one`、`two`、`three`、`stone`（都是目標角度）；重現 34 次，其中 `gestures.yaml` 的 `ok` 12 次 |

看得出來、但還沒有結論的事：

- 那一次落後只超過上限 0.3°，發生在快速檔一段移動的開頭。是拇指起步比較慢、還是當時有東西擋著，紀錄分不出來。同一檔的另外 43 段都正常。
- 側擺用到了接近範圍邊緣的值（`ya` 的食指與中指側擺約 ±37°），沒有出現落後或沒到位。這不等於各種彎曲角度下的側擺都驗證過。
