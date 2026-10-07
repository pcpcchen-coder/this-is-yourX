#!/usr/bin/env bash
# 一根手指的校正：兩顆回中位（裝舵盤），或分段開合一次（微調中位）。
# 匯流排上只能接這根手指的兩顆。
# 用法：bash right_hand/tools/finger_cal.sh center <奇數 ID> <偶數 ID> [中位A 中位B]
#       bash right_hand/tools/finger_cal.sh finger <奇數 ID> <偶數 ID> [中位A 中位B]
set -u
cd "$(dirname "$0")/.." || exit 1
MODE="${1:-}"; A="${2:-}"; B="${3:-}"
case "$MODE" in
  center|finger) ;;
  *) echo "用法：bash right_hand/tools/finger_cal.sh <center|finger> <奇數 ID> <偶數 ID> [中位A 中位B]"; exit 1 ;;
esac
[ -n "$A" ] && [ -n "$B" ] || { echo "要給兩個 ID，例如：bash right_hand/tools/finger_cal.sh ${MODE} 1 2"; exit 1; }
PY=.venv/bin/python
[ -x "$PY" ] || { echo "還沒有 Python 環境，先執行：bash right_hand/tools/first_scan.sh"; exit 1; }

mkdir -p logs
LOG="logs/finger_${MODE}_${A}_${B}_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

PORTS="$($PY tools/servo_tool.py ports)" || { echo "$PORTS"; exit 1; }
if [ "$(printf '%s\n' "$PORTS" | wc -l | tr -d ' ')" != "1" ]; then
  echo "找到不只一個序列埠，請拔掉其他 USB 序列裝置後再試："
  echo "$PORTS"
  exit 1
fi

echo "== ${MODE} ${A} ${B} ${4:-0} ${5:-0}  埠 ${PORTS}"
if [ -n "${4:-}" ] && [ -n "${5:-}" ]; then
  $PY -u tools/servo_tool.py "$MODE" "$PORTS" "$A" "$B" "$4" "$5"
else
  $PY -u tools/servo_tool.py "$MODE" "$PORTS" "$A" "$B"
fi
RC=$?
echo "== 結束（代碼 ${RC}）。紀錄：right_hand/${LOG}"
exit "${RC}"
