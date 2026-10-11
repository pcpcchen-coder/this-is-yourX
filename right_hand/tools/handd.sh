#!/usr/bin/env bash
# 啟動 handd（右手的 skill gateway，ADR-0006）。保持這個終端機開著；Ctrl-C 結束（會關扭力）。
#   bash right_hand/tools/handd.sh                 模擬（預設）：AI 的動作只在模擬上執行
#   bash right_hand/tools/handd.sh --adapter scs   接實體伺服機：只讀狀態，動作一律拒絕（P2）
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || { echo "還沒有 Python 環境，先執行：bash right_hand/tools/first_scan.sh"; exit 1; }
"$PY" -c "import yaml, mcp" 2>/dev/null || { echo "缺套件，先執行：right_hand/.venv/bin/pip install -r right_hand/requirements.txt"; exit 1; }
exec "$PY" -u -m hand_api.daemon "$@"
