#!/bin/zsh
# 建置 QRing Hand 並安裝到 ~/Applications（macOS、Apple Silicon）。
# 在 Finder 對這個檔案按兩下，或在終端機執行：zsh right_hand/control_methods/qring_fist/install.command
# 過程：結束執行中的 QRing Hand →（第一次）建 Python 環境 → 跑測試 → 建置 → 封裝自我檢查 → 複製到 ~/Applications → 開啟。
# 紀錄留在這個資料夾的 build-install.log。
#
# Python 環境：預設用這個資料夾的 .venv（沒有就用 /usr/bin/python3 建一個，要是 Apple 的 Python 3.9）。
# 要沿用別處已經裝好相依套件的 Apple Python 3.9 venv，設 QRING_HAND_PYTHON，例如：
#   QRING_HAND_PYTHON=~/qringr08plus/.venv/bin/python zsh install.command
cd "$(dirname "$0")" || exit 1
LOG="build-install.log"

stop_running_app() {
  # 已經在執行的 QRing Hand 先正常收尾：請內建服務結束（它會先關手掌扭力、還原戒指的觸控設定），再結束視窗程序。
  local SERVER="QRing Hand.app/Contents/Resources/app/server.py"
  if pgrep -x QRingHand >/dev/null; then
    echo "-- QRing Hand 正在執行：先請它結束（關手掌扭力、還原戒指設定）"
    curl -s -m 20 -o /dev/null -X POST -H 'Content-Type: application/json' -d '{}' \
      http://127.0.0.1:18775/api/shutdown || echo "   內建服務沒有在 20 秒內回應（0.1.0 結束時可能卡住；扭力那一步排在前面）"
    pkill -x QRingHand
    local i=0
    while pgrep -x QRingHand >/dev/null && [ $i -lt 15 ]; do sleep 1; i=$((i + 1)); done
    if pgrep -x QRingHand >/dev/null; then
      echo "QRing Hand 沒有結束。請從選單列手動結束它，再執行一次。"; return 1
    fi
  fi
  # 視窗程序不在了，內建服務卻還留著（卡住的舊版）：它會佔住連接埠與戒指的藍牙連線
  if pgrep -f "$SERVER" >/dev/null; then
    sleep 3
    if pgrep -f "$SERVER" >/dev/null; then
      echo "-- 舊的內建服務還在，送結束訊號"
      pkill -TERM -f "$SERVER"; sleep 3
      pgrep -f "$SERVER" >/dev/null && pkill -KILL -f "$SERVER"
    fi
  fi
  return 0
}

main() {
  echo "== QRing Hand 建置與安裝 $(date '+%Y-%m-%d %H:%M:%S') =="
  local PY="${QRING_HAND_PYTHON:-.venv/bin/python}"
  local APP=".build.noindex/QRing Hand.app"
  local DEST="$HOME/Applications/QRing Hand.app"
  stop_running_app || return 1
  if [ ! -x "$PY" ]; then
    if [ -n "${QRING_HAND_PYTHON:-}" ]; then
      echo "QRING_HAND_PYTHON 指到的 Python 不存在：$PY"; return 1
    fi
    echo "-- 0/4 建立 Python 環境（.venv）"
    /usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 9) else 1)' || {
      echo "/usr/bin/python3 不是 Python 3.9。原生容器固定用 Apple 的 Python 3.9（Xcode 或 Command Line Tools）。"; return 1; }
    /usr/bin/python3 -m venv .venv || { echo "建不了 .venv。"; return 1; }
    .venv/bin/python -m pip install --upgrade pip || return 1
    .venv/bin/python -m pip install -r requirements.txt || { echo "相依套件安裝失敗。"; return 1; }
  fi
  echo "-- 1/4 測試（模擬戒指與模擬手掌，約一分鐘）"
  "$PY" -m unittest test_hand.py || { echo "測試沒過，停止安裝。"; return 1; }
  echo "-- 2/4 建置"
  "$PY" build_app.py || { echo "建置失敗。"; return 1; }
  echo "-- 3/4 封裝自我檢查"
  "$APP/Contents/MacOS/QRingHand" --self-test || { echo "封裝自我檢查沒過。"; return 1; }
  echo "-- 4/4 安裝到 $DEST"
  mkdir -p "$HOME/Applications"
  if [ -d "$DEST" ]; then
    rm -rf "$DEST.previous"
    mv "$DEST" "$DEST.previous" || { echo "移不開舊版。"; return 1; }
  fi
  ditto "$APP" "$DEST" || { echo "複製失敗。"; [ -d "$DEST.previous" ] && mv "$DEST.previous" "$DEST"; return 1; }
  codesign --verify --deep --strict "$DEST" || { echo "安裝後的簽章檢查沒過。"; return 1; }
  rm -rf "$DEST.previous"
  echo "INSTALL_OK $(defaults read "$DEST/Contents/Info" CFBundleShortVersionString 2>/dev/null)"
  open "$DEST"
}

main 2>&1 | tee "$LOG"
echo
echo "完成。這個視窗可以關掉。"
