# Contributing

感謝參與 `this-is-yourX`。本專案把「AI 是否理解自身元件」當成可測試的工程問題。

## Before opening a PR

1. 先讀 [專案願景](docs/00_PROJECT_VISION.md) 與 [安全基線](docs/07_SAFETY_SECURITY_PRIVACY.md)。
2. 說明變更對應 L0–L4 哪個 self-model level。
3. 新增/修改 manifest 時執行 schema validation。
4. 新 component/skill 依 [Definition of Done](docs/05_EXPERIMENTS_AND_ACCEPTANCE.md) 補測試。
5. 涉及實機時，寫明 hardware revision、experiment ID、limits 與 E-stop/watchdog 結果。
6. 不提交 credentials、個人錄音/影像、未去識別資料或大型 rosbag/model artifact。

## Commit and PR scope

- 一個 PR 聚焦一個能力或決策。
- breaking schema/API change 需提供 migration。
- 生成式模型輸出不能新增繞過 Safety Gateway 的控制路徑。
- 文件、範例與 contract 應同步更新。

## PR checklist

- [ ] Goal/acceptance criteria 清楚。
- [ ] Schema、unit、simulation/replay tests 通過。
- [ ] Safety/privacy impact 已說明。
- [ ] 失敗與 unknown 情境有測試。
- [ ] Revision、evidence、event 可追溯。
- [ ] 有 rollback 或 disable 方法。
