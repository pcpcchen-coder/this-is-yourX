#!/usr/bin/env bash
# 第一次上電掃描：建 Python 環境、找序列埠、掃描匯流排。只讀，不會讓伺服機轉動。
# 用法：bash right_hand/tools/first_scan.sh [序列埠]
# 輸出同時寫到 right_hand/logs/，方便事後查看。
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
LOG="logs/first_scan_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee "$LOG") 2>&1

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$c"; break
  fi
done
UV="$(command -v uv 2>/dev/null || true)"
[ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"

if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  rm -rf .venv                      # 舊環境的 Python 太舊，重建
fi
if [ ! -x .venv/bin/python ]; then
  if [ -n "$PY" ]; then
    "$PY" -m venv .venv || exit 1
  elif [ -n "$UV" ]; then
    echo "== 系統沒有 Python 3.10 以上，改用 uv 下載 Python 3.12"
    "$UV" venv --seed --python 3.12 .venv || exit 1
  else
    echo "找不到 Python 3.10 以上，也沒有 uv。先安裝 uv（不需要管理員權限）："
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "裝完再執行一次這支腳本。"
    exit 1
  fi
fi
echo "== Python：$(.venv/bin/python --version)"
.venv/bin/python -m pip install -q -r requirements.txt || { echo "套件安裝失敗"; exit 1; }
echo "== rustypot：$(.venv/bin/python -m pip show rustypot 2>/dev/null | sed -n 's/^Version: //p')"

PORT="${1:-}"
if [ -z "$PORT" ]; then
  echo "== 序列埠"
  PORTS="$(.venv/bin/python tools/servo_tool.py ports)" || { echo "$PORTS"; exit 1; }
  echo "$PORTS"
  if [ "$(printf '%s\n' "$PORTS" | wc -l | tr -d ' ')" != "1" ]; then
    echo "找到不只一個序列埠。請指定驅動板那一個：bash tools/first_scan.sh <埠名>"
    exit 1
  fi
  PORT="$PORTS"
fi

echo "== 掃描 ${PORT}"
.venv/bin/python tools/servo_tool.py scan "$PORT"
RC=$?
echo "== 結束（代碼 ${RC}），紀錄：right_hand/${LOG}"
exit $RC
