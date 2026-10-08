#!/usr/bin/env bash
# 操作者指令：hand status | pending | approve <id> | stop | clear-fault | log [n]
# 建議加別名：echo "alias hand='bash ~/this-is-yourX/right_hand/tools/hand.sh'" >> ~/.zshrc
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || { echo "還沒有 Python 環境，先執行：bash right_hand/tools/first_scan.sh"; exit 1; }
exec "$PY" -m hand_api.cli "$@"
