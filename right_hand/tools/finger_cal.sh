#!/usr/bin/env bash
# 單指校正與全手測試。都是人在工作台上操作的工具。
#   center：一根手指的兩顆回中位並保持扭力（裝舵盤用）。匯流排上只接這兩顆。
#   finger：一根手指分段開合一次（微調中位用）。匯流排上只接這兩顆。
#   hand  ：8 顆全接，四根手指輪流開合一次。要能隨手切斷伺服機電源。
#   gesture：8 顆全接，四指同時動，比一個固定手勢、停住、再張開。目前只有 ok。
#            速度可選 slow（預設）、normal、fast。
# 用法：bash right_hand/tools/finger_cal.sh center <奇數 ID> <偶數 ID> [中位A 中位B]
#       bash right_hand/tools/finger_cal.sh finger <奇數 ID> <偶數 ID> [中位A 中位B]
#       bash right_hand/tools/finger_cal.sh hand [中位1 … 中位8]
#       bash right_hand/tools/finger_cal.sh gesture ok [slow|normal|fast] [中位1 … 中位8]
set -u
cd "$(dirname "$0")/.." || exit 1
MODE="${1:-}"
case "$MODE" in
  center|finger)
    [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "要給兩個 ID，例如：bash right_hand/tools/finger_cal.sh ${MODE} 1 2"; exit 1; }
    [ "$#" = 3 ] || [ "$#" = 5 ] || { echo "中位修正要一次給兩個，例如：bash right_hand/tools/finger_cal.sh ${MODE} $2 $3 3 0"; exit 1; }
    TAG="${MODE}_$2_$3" ;;
  hand)
    [ "$#" = 1 ] || [ "$#" = 9 ] || { echo "中位修正要一次給 8 個（ID 1 到 8），或都不給。"; exit 1; }
    TAG="hand" ;;
  gesture)
    case "$#" in 2|3|10|11) ;; *) echo "用法：bash right_hand/tools/finger_cal.sh gesture ok [slow|normal|fast] [中位1 … 中位8]"; exit 1 ;; esac
    case "$2" in *[!a-z0-9_]*|"") echo "手勢名稱只能是小寫英數，例如 ok"; exit 1 ;; esac
    TAG="gesture_$2"
    if [ "$#" = 3 ] || [ "$#" = 11 ]; then
      case "$3" in slow|normal|fast) TAG="${TAG}_$3" ;; *) echo "速度只能是 slow、normal、fast"; exit 1 ;; esac
    fi ;;
  *)
    echo "用法：bash right_hand/tools/finger_cal.sh <center|finger> <奇數 ID> <偶數 ID> [中位A 中位B]"
    echo "      bash right_hand/tools/finger_cal.sh hand [中位1 … 中位8]"
    echo "      bash right_hand/tools/finger_cal.sh gesture ok [slow|normal|fast] [中位1 … 中位8]"
    exit 1 ;;
esac
shift
PY=.venv/bin/python
[ -x "$PY" ] || { echo "還沒有 Python 環境，先執行：bash right_hand/tools/first_scan.sh"; exit 1; }

mkdir -p logs
LOG="logs/finger_${TAG}_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

PORTS="$($PY tools/servo_tool.py ports)" || { echo "$PORTS"; exit 1; }
if [ "$(printf '%s\n' "$PORTS" | wc -l | tr -d ' ')" != "1" ]; then
  echo "找到不只一個序列埠，請拔掉其他 USB 序列裝置後再試："
  echo "$PORTS"
  exit 1
fi

echo "== ${MODE} $*  埠 ${PORTS}"
$PY -u tools/servo_tool.py "$MODE" "$PORTS" "$@"
RC=$?
echo "== 結束（代碼 ${RC}）。紀錄：right_hand/${LOG}"
exit "${RC}"
