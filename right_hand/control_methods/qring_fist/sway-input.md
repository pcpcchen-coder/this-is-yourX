# QRing Hand 0.2.0：握拳與左右側擺輸入

日期：2026-10-14（Asia/Taipei）。新增的是**操作者手掌側傾的戒指輸入**。目前的機器手跟隨仍只使用握拳程度；沒有將側擺指定為手指側擺或新增手腕馬達。

## 使用

1. 在「校正」保留「同時校正左右側擺」勾選，按開始校正。
2. 張開、回到中立，停穩後單擊戒指；保持手掌方向握拳，停穩後再單擊。
3. 手指張開、回到中立，向左側傾、停穩、單擊；再回中立、向右側傾、停穩、單擊。
4. 在預覽中試張開、握拳與左右側擺，確認兩個輸入。最後單擊儲存，或按儲存校正。
5. 「控制」同時呈現握拳程度與側擺（左／中立／右、百分比、側擺座標角度、有效取樣年齡）。資料過期或無效時，側擺顯示「—」。

「設定 → 側擺中立範圍」預設為左右各自幅度的 10%，可調 0–40%。平滑沿用原有設定。這些讀值在未連機器手、未啟動跟隨時也可觀察；完成校正不會啟用扭力。

## 能觀測的自由度

只有三軸加速度（含重力），未加入新的感測器或協定命令。單位重力方向只有兩個方向座標。本版用以下可重現的幾何座標，並不宣稱能識別真正的手腕關節角度：

- 張開 `o` 與握拳 `f` 定義平面：`u = normalize(f - dot(o,f)*o)`，`n = cross(o,u)`。
- 握拳座標：`theta = atan2(dot(g,u),dot(g,o))`，除以原握拳角度差，接原有握拳濾波。
- 側擺座標：`latitude = asin(dot(g,n))`，由左右張開取樣各自正規化；左為 -1，右為 +1，零為中立。左右幅度可不相等，方向正負由使用者的取樣名稱決定。
- 中立區外重新縮放至 -1…1，再做指數平滑；回中立即歸零。

左右取樣要求離握拳平面至少 10°、至多 65°，沿握拳方向偏差不超過 15°，左右必須在不同側。量不到、與握拳重疊、左右同側、手指未保持張開或角度過大，都會拒絕完成校正並允許重記。

有效操作範圍是左右校正端點外再留 `min(off_limit,15°)` 餘裕，側擺絕對角度不得超過 75°（極點附近握拳投影不可靠）；原握拳兩端的 35° 餘裕與 0.8–1.2 g 大小檢查仍有效。四點模式以此側擺範圍取代原來只接受握拳平面附近的判斷。無效取樣不產生新機器手目標。

**物理限制**：水平放手、平行桌面左右轉若不改變重力，不能量到。其他手腕、前臂動作也可能產生相同重力方向。本模型在定義的方向座標上可以分開兩個輸入；真實握拳與側擺組合是否交互影響，必須由操作者實測，不能由模擬測試推定。

## 相容與介面

- 保留原兩點校正：取消四姿勢勾選即可。既有 version 1 校正直接讀取，側擺顯示未校正。
- 四點校正寫入 version 2：原 `open`、`fist`、`span_deg`、`name`、`saved_at`，加 `left`、`right`、`sway_degrees`。取消或失敗不覆寫既有存檔。重記張開或握拳會清除兩側暫存取樣。
- 舊版 0.1.2 不理解 version 2；降版前需還原舊校正或重新做兩點校正。本次安裝先備份舊 App 與個人設定，私有資料不入 Git。
- `POST /api/calibration/start` 增加可選布林 `sway`，省略仍為 false，以相容既有客戶端；UI 新校正預設 true。
- `GET /api/state` 的 `follow.sway` 為只讀輸入，包含 `valid`、`calibrated`、`progress`（原始比例）、`value`（中立與平滑後 -1…1）、`angle`、`age`、`issue`。未校正、無效或取樣過期不應被消費者當成控制命令。
- `sway_neutral` 是持久化設定，合法有限數字 0–0.4；布林、字串與 NaN 等無效值拒絕。
- 既有機器手 HTTP 介面、啟動確認、停止、掉線、心跳與面板保護沿用。快照輪詢調校不變。AI 動作仍只走 hand_api 的 Safety Gateway。

## 驗證

在 M6 使用 Apple Python 3.9 與模擬戒指／面板：

```bash
python -m unittest test_hand.py
node --check web/app.js
python build_app.py
.build.noindex/'QRing Hand.app'/Contents/MacOS/QRingHand --self-test
codesign --verify --deep --strict ~/Applications/'QRing Hand.app'
```

69 項測試涵蓋原有 59 項與新增的雙座標、左右反向／不等幅度、中立與平滑、不可觀測姿勢、無效大小／極點／超範圍、四步單擊儲存與重載、取消保持舊存檔、重記清除依賴、錯誤校正恢復、設定與 API 驗證。輸入測試確認沒有新增伺服機側擺目標、沒有自動啟用扭力。

瀏覽器用獨立的模擬戒指完成四點校正，看到握拳 50% 與左側擺 56% 同時顯示，儲存後重載可讀回；沒有連接實體手掌。截圖與舊 App 備份留在本機 `.build.noindex/`。

實體左右動作、不同握拳程度下側擺的交叉影響、端到端延遲尚未實測，沒有 real-hardware validation 主張。

M6 安裝檢查：`~/Applications/QRing Hand.app` 的版本為 0.2.0、build 4；封裝自我檢查回 `BUNDLED_APP_SELF_TEST_OK`，簽章驗證通過；安裝前後個人 `calibration.json` 與 `preferences.json` 的 SHA-256 一致。完整模擬測試結果：`Ran 69 tests in 106.063s / OK`。本機執行使用 `/Users/georgechen/Documents/Codex/2026-10-09/qringr08plus/.venv/bin/python` 與 bundled Node（`/Users/georgechen/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --check web/app.js`）。
