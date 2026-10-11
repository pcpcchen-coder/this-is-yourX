#!/usr/bin/env bash
# 單顆伺服機：掃描 → 轉動測試 → 把 ID 從 1 改成指定值 → 再掃描確認。
# 匯流排上只能接一顆、出力軸不要接任何東西。
# 用法：bash right_hand/tools/assign_id.sh <新 ID 1-8> [序列埠]
#       新 ID 給 1 表示只測試、不改 ID。
set -u
cd "$(dirname "$0")/.." || exit 1
NEW="${1:-}"
case "$NEW" in
  [1-8]) ;;
  *) echo "用法：bash right_hand/tools/assign_id.sh <新 ID 1-8> [序列埠]"; exit 1 ;;
esac
PY=.venv/bin/python
[ -x "$PY" ] || { echo "還沒有 Python 環境，先執行：bash right_hand/tools/first_scan.sh"; exit 1; }

mkdir -p logs
LOG="logs/assign_id_${NEW}_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

PORT="${2:-}"
if [ -z "$PORT" ]; then
  PORTS="$($PY tools/servo_tool.py ports)" || { echo "$PORTS"; exit 1; }
  if [ "$(printf '%s\n' "$PORTS" | wc -l | tr -d ' ')" != "1" ]; then
    echo "找到不只一個序列埠，請指定：bash right_hand/tools/assign_id.sh ${NEW} <埠名>"
    echo "$PORTS"
    exit 1
  fi
  PORT="$PORTS"
fi

echo "== 掃描 ${PORT}"
FOUND="$($PY tools/servo_tool.py scan "$PORT")" || { echo "$FOUND"; exit 1; }
echo "$FOUND"
if [ "$FOUND" != "ID 1  SCS0009" ]; then
  echo "預期匯流排上只有一顆出廠 ID 1 的 SCS0009。請確認只接了一顆、而且是還沒設過 ID 的。"
  exit 1
fi

echo "== 轉動測試"
$PY tools/servo_tool.py test "$PORT" 1 || { echo "測試沒過，不改 ID。這顆先放旁邊。"; exit 1; }

if [ "$NEW" = "1" ]; then
  echo "== 完成：這顆維持 ID 1。請貼上標籤 1。紀錄：right_hand/${LOG}"
  exit 0
fi

echo "== 改 ID：1 → ${NEW}"
$PY tools/servo_tool.py setid "$PORT" 1 "$NEW" || exit 1

echo "== 再掃描"
$PY tools/servo_tool.py scan "$PORT"
echo "== 完成：請在這顆貼上標籤 ${NEW}。紀錄：right_hand/${LOG}"
