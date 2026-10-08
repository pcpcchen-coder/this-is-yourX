#!/usr/bin/env bash
# 右手的網頁操作面板（人工操作；逐指滑桿、存下與重現姿勢）。說明：right_hand/docs/hand_panel.md
#   bash right_hand/tools/hand_panel.sh                 模擬（預設），Ctrl-C 結束
#   bash right_hand/tools/hand_panel.sh --adapter scs   接實體伺服機
#   bash right_hand/tools/hand_panel.sh url             印出網址
# 預設不需要存取碼（只在自己的內網用）；要加回來：啟動與 url 都加 --require-token。
# 開實體之前：人要能隨手切斷伺服機電源。面板的「停止」只是第二層。
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || { echo "還沒有 Python 環境，先執行：bash right_hand/tools/first_scan.sh"; exit 1; }
"$PY" -c "import yaml" 2>/dev/null || { echo "缺套件，先執行：right_hand/.venv/bin/pip install -r right_hand/requirements.txt"; exit 1; }
if [ "${1:-}" = "url" ]; then
  shift
  exec "$PY" -m hand_panel.server --print-url "$@"
fi
exec "$PY" -u -m hand_panel.server "$@"
