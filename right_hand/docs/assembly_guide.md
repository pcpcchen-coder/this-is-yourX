# Amazing Hand 右手組裝指引（Seeed 套件）

整理自官方組裝手冊，加上 Seeed 套件買家與論壇回報的差異。逐步勾選請用 [組裝檢查表](assembly_checklist/index.html)；這份文件說明順序、原因和容易出錯的地方。

沒有找到完整的組裝影片。最完整的逐步資料是官方 37 頁圖文手冊。

每項差異與注意事項後面標了來源。標「買家筆記」或「論壇」的是他人回報，本專案還沒在實物上驗證；已由實物確認的列在 [到貨清點](arrival_inspection.md)。

## 順序

| 階段 | 內容 | 手冊頁 |
|---|---|---|
| 0 | 開箱清點、確認變壓器電壓、驅動板模式、電腦環境、伺服機逐顆測試並設 ID | 5、21 |
| 1 | 零件前處理：修舵盤、修樞軸孔、清毛邊、組長度治具 | 6–8、10 |
| 2 | 手指組裝 ×4：球頭連桿、舵盤、指節機構、軟殼、襯套、伺服機、合體 | 11–20 |
| 3 | 單指校正 ×4：回中位、裝舵盤、微調中位、記錄數值 | 21–24 |
| 4 | 手掌組裝：三指上 Hand plate、拇指上 Wrist interface、合體、串接、全手測試 | 29–33 |
| 5 | 外殼：軟掌殼、上蓋 | 35–36 |

兩個刻意偏離手冊的安排：

- **設 ID 提前到階段 0。** 手冊在階段 3 才設。提前做可以在伺服機裝進手指之前抓出壞件；Seeed 論壇有買家裝好第一根手指後才發現那兩顆都是壞的。
- **先把食指從組裝做到校正完，再做其餘三根。** 四根手指相同，第一根學到的調整可以直接套用。

每根手指校正完才裝上手掌，裝上去之後很難再調舵盤。

## 伺服機 ID 配置

| 手指 | ID | 俯視 Hand plate 的位置 |
|---|---|---|
| 食指 | 1、2 | 左 |
| 中指 | 3、4 | 中 |
| 無名指 | 5、6 | 右 |
| 拇指 | 7、8 | 裝在 Wrist interface |

面向出力軸、指尖朝上時，奇數 ID 在右、偶數 ID 在左（手冊第 21 頁）。

## Seeed 套件與手冊的差異

| 手冊 | Seeed 套件 | 來源 |
|---|---|---|
| 1.5mm 鑽頭鑽舵盤孔，再攻 M2 牙 | 只附一支鑽頭，買家全程用 2mm 完成 | 買家筆記；本套件確認只有一支鑽頭 |
| 自攻螺絲分 2.5x6 與 2.5x8 | 統一成約 7mm | 買家筆記 |
| 球頭有凸緣 | 沒有凸緣，球的一側凸出較多。螺絲頭放在凸出較少的那側，舵盤側才有間隙 | 買家筆記 |
| Link 用 M2 長螺桿，兩端各一顆螺帽 | 帶頭長螺絲加一顆螺帽：螺絲頭 → 球頭 → Link 凸台 → 球頭 → 螺帽，在接連桿那一步才裝 | 買家筆記；本套件食指實裝（2026-10-04） |
| 自行把螺桿與球頭螺絲裁短 | 已裁好 | 本套件確認 |

## 機構注意事項

- **球頭連桿長度是全手最關鍵的尺寸**，決定手指行程。用長度治具（兩支插銷軸距 33mm）確認，連桿套上去應不用硬壓；量好後把其中一端轉 90°。（手冊第 10–11 頁）
- 樞軸孔分兩類：零件要繞著插銷轉的孔修到滑配，壓住插銷的孔保持緊配。哪些孔屬於哪一類看手冊第 7 頁的圖。滑配孔不要鑽到鬆晃。
- 兩顆伺服機的出力軸要在遠離襯套（外展樞軸）的那一端。（手冊第 18 頁）
- Gimbal 的固定螺絲鎖到墊片碰到襯套就停，手指左右擺動要順、不晃。（手冊第 19 頁）
- 連桿上的 M2 螺帽碰到後再多鎖半圈；會鬆就點膠。（手冊第 20 頁）
- 軟殼的螺絲輕鎖。鎖太深會刮到內部螺桿，也會穿破軟殼。（手冊第 16 頁；買家筆記）
- 修舵盤需要斜口鉗和銼刀或美工刀，套件沒附。

## 電氣注意事項

- **電壓**：SCS0009 工作範圍 4.0–7.4V，Seeed 客服表示套件以 5V 變壓器驗證。驅動板本身吃 5–12V，部分通路頁面寫 12V 3A；通電前看變壓器標示，12V 不要接。（Seeed 論壇）
- **驅動板模式（用電腦 USB 控制）**：板背焊盤要橋接，正面 2-pin 要插跳線帽。Seeed 客服表示出廠預設是 USB 模式。（Seeed Wiki、Seeed 論壇）
- **設 ID**：出廠全部是 ID 1，一次只接一顆。要先把 Lock 設為 0 再改 ID，否則斷電後不會保存。（買家筆記）`tools/servo_tool.py setid` 已照這個順序寫入。
- **壞件**：伺服機亂轉、讀值跳動時，先單顆測試排除壞件，再懷疑板子。（Seeed 論壇）
- **配線**：每根手指的兩顆先進一個分線板，再兩兩合併，方便單指測試。（買家筆記）
- 插拔伺服機或線材前先斷電。（Seeed Wiki）

## 校正流程

官方腳本在上游 repo 的 `PythonExample/`，需要 `rustypot` 與 `numpy`。

1. `AmazingHand_Hand_FingerMiddlePos.py`：改 `serial_port`、`ID_1`、`ID_2`，執行後兩顆回到中位。
2. 伺服機保持在中位，裝上兩個舵盤，位置照手冊第 22 頁，鎖 M2x4。
3. `AmazingHand_FingerTest.py`：同樣改埠與 ID。手指閉合時停下，看舵盤是否對齊伺服機中線，沒對齊就調 `MiddlePos`（單位是度）再試。左右兩顆鏡像安裝，加減方向不直觀，一次改 2–3 度。
4. 記下兩個中位值。四根手指共 8 個值，最後填進 `AmazingHand_Demo.py` 的 `MiddlePos`；右手 `Side = 1`。

已知問題：`AmazingHand_FingerTest.py` 的扭力啟用寫死成 `write_torque_enable(1, 1)`，只開 ID 1。校正其他手指時要改成對 `ID_1`、`ID_2` 各開一次。（讀上游原始碼確認，2026-10-02）

腳本預設 `serial_port="COM11"`、鮑率 1000000。macOS 的埠名是 `/dev/cu.usbmodem…` 這類。

## 來源

- [官方組裝手冊 PDF](https://github.com/pollen-robotics/AmazingHand/blob/main/docs/AmazingHand_Assembly.pdf)（Pollen Robotics）
- [官方 repo 與 PythonExample](https://github.com/pollen-robotics/AmazingHand)
- [Seeed Wiki：AmazingHand Quick Start](https://wiki.seeedstudio.com/hand_amazinghand/)
- [Seeed Wiki：Bus Servo Driver Board](https://wiki.seeedstudio.com/bus_servo_driver_board/)
- [Seeed 論壇：Issues with Bus Servo Driver board & SCS0009](https://forum.seeedstudio.com/t/issues-with-bus-servo-driver-board-scs0009/295301)
- [Seeed 論壇：Bus Servo Driver Board and Pico 2 over UART](https://forum.seeedstudio.com/t/issue-with-bus-servo-driver-board-and-pico-2-over-uart/295104)
- [Seeed wiki 討論串 #4160](https://github.com/Seeed-Studio/wiki-documents/discussions/4160)
- [Amazing Hand notes（Seeed 套件買家筆記）](https://docs.google.com/document/d/1WSPk8Eg3CIKFiu_UKCOuWbivSlJtWLOXeQaqer0MOaw/edit?usp=sharing)
- [買家改寫的校正腳本](https://github.com/thandal/AmazingHand)
