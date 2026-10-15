# daily_gesture — 每天 07:00 的自創手勢

RPi 上的 systemd timer 每天台北時間 07:00 執行 `daily_gesture.py`：手有連線就從 16 個 Claude 自創的手勢裡隨機挑一個（避開最近 4 天用過的，彎曲再加 ±5° 隨機變化），慢速擺出、停 3 秒、張開、關扭力。

## 決策（2026-10-15，George）

偏離 ADR-0006：**不逐次核准**、**手勢沒有經過工作台校正**。彌補：

- 只走網頁操作面板的公開 HTTP 介面，面板的落後／電壓／溫度／掉線／到位檢查與 fault 鎖定照舊；不清 fault。
- 只用慢速；側擺 ±20°（面板 ±40°）；|彎曲|+|側擺| ≤ 90°；手勢表啟動時自我檢查。
- 手沒連線（面板連不到、匯流排沒接上、8 顆沒全回應）、面板是模擬、有 fault、有人正在控制、電壓不在 4.0–7.4 V、溫度 > 55 °C：一律不動，只記一筆。
- 結束一律 `/api/stop`；錯過 07:00（主機關著）不補做。
- 實體開關仍是唯一的 E-stop；07:00 沒有人在旁邊。

## 安裝（RPi）

```bash
scp install_daily_gesture.sh kergptpi@192.168.1.112:
ssh kergptpi@192.168.1.112 'bash install_daily_gesture.sh'
sudo loginctl enable-linger kergptpi        # 沒開的話，登出後 timer 不會觸發
systemctl --user enable hand-panel          # 面板要在跑；07:00 連不到面板就只記 skipped
```

## 使用

| 指令 | 作用 |
|---|---|
| `python3 daily_gesture.py --list` | 列出手勢 |
| `python3 daily_gesture.py --dry-run` | 只檢查連線與挑手勢 |
| `python3 daily_gesture.py --gesture claw` | 指定手勢試跑（會動） |
| `systemctl --user list-timers daily-gesture.timer` | 下次觸發時間 |
| `tail -n 5 right_hand/logs/daily_gesture.jsonl` | 結果（outcome：done／skipped／aborted） |
| `systemctl --user disable --now daily-gesture.timer` | 停用 |

## 驗證

在開發環境的模擬面板（`hand_panel.server --adapter fake`）上：16 個手勢各跑一次全部 done；模擬面板未加 `--allow-simulated` 時 skipped；另一頁面控制中時 skipped；停留中被別人按停止時 aborted 並關扭力；不合規的手勢被拒絕。**實機上還沒跑過**，沒有 experiment ID。
