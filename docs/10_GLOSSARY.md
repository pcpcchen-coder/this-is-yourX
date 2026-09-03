# 名詞表

| 名詞 | 本專案定義 |
|---|---|
| Embodiment | AI 所依附、可感知或可控制的物理/虛擬機體 |
| Self-model | 描述自身元件、關係、狀態、能力、限制與預測的可查詢模型 |
| Body schema | 以 frame/link/joint 為主的身體空間與運動結構 |
| Body graph | 擴充 body schema，加入 sensor、power、software、capability、evidence 與 dependency 的圖 |
| Grounding | 將語詞/概念 X 對應到可觀測、可定位、可驗證的 component |
| Sensorimotor grounding | 以受控動作與感測變化建立因果對應 |
| Proprioception | 機器對自身位置、速度、負載等內部狀態的感知 |
| Exteroception | 相機、麥克風、LiDAR 等對外部環境的感知 |
| Affordance | 元件在指定條件下能提供的行為/用途，例如 grasp、point |
| Component | link、joint、sensor、actuator、tool、power 或 software organ 等節點 |
| Semantic ID | 穩定的功能/位置身份，如 `arm.left.hand`；不同於硬體 serial |
| Binding | semantic component 與實際 driver/device/topic 的對應 |
| Evidence | 支持一項 identity/location/state assertion 的可追溯資料 |
| Confidence | 對 assertion 正確性的估計；需經校準，不能取代 evidence |
| Freshness | observation 距目前時間是否仍在可接受 window |
| Discrepancy | 模型/命令的預期與實際觀測不一致 |
| Forward model | 從目前狀態與 action 預測下一狀態/觀測的模型 |
| Calibration | 求得 joint offset、sensor bias、camera intrinsics/extrinsics 或 tool TCP 等參數 |
| Skill | 有版本、參數 schema、前置條件、限幅、timeout/cancel 的高階動作契約 |
| Safety Gateway | 驗證權限、狀態、limits、approval 與 collision 後才執行 skill 的唯一入口 |
| Authority level | A0–A4 的動作權限級別，從 read-only 到 experimental policy |
| Safe state | timeout/fault/E-stop 後元件應進入的已定義狀態 |
| Three-valued logic | safety condition 使用 true/false/unknown；unknown 不允許動作 |
| Attachment | 暫時接在自身上的 tool/payload，確認與校正後才加入 active body graph |
| Digital twin | 與實機結構/狀態對應的數位表示；本專案 self-model 還加入語意與 evidence |
| URDF/Xacro | ROS 常用的機器人 link/joint/geometry 模型格式及其巨集化工具 |
| TF2 | ROS 的時間化座標轉換系統 |
| ros2_control | ROS 2 的 controller 與硬體介面框架 |
| rosbag2 | ROS 2 topic 資料的記錄與重播工具 |
| VLM/VLA | Vision-Language Model / Vision-Language-Action model；在本專案皆受 tool/safety contract 限制 |
