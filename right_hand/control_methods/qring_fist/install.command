#!/bin/zsh
# 建置 QRing Hand 並安裝到 ~/Applications（macOS、Apple Silicon）。
# 在 Finder 對這個檔案按兩下，或在終端機執行：zsh right_hand/control_methods/qring_fist/install.command
# 過程：（第一次）建 Python 環境 → 跑測試 → 建置 → 封裝自我檢查 → 複製到 ~/Applications → 開啟。
# 紀錄留在這個資料夾的 build-install.log。
#
# Python 環境：預設用這個資料夾的 .venv（沒有就用 /usr/bin/python3 建一個，要是 Apple 的 Python 3.9）。
# 要沿用別處已經裝好相依套件的 Apple Python 3.9 venv，設 QRING_HAND_PYTHON，例如：
#   QRING_HAND_PYTHON=~/qringr08plus/.venv/bin/python zsh install.command
cd "$(dirname "$0")" || exit 1
LOG="build-install.log"

main() {
  echo "== QRing Hand 建置與安裝 $(date '+%Y-%m-%d %H:%M:%S') =="
  local PY="${QRING_HAND_PYTHON:-.venv/bin/python}"
  local APP=".build.noindex/QRing Hand.app"
  local DEST="$HOME/Applications/QRing Hand.app"
  if pgrep -x QRingHand >/dev/null; then
    echo "QRing Hand 正在執行。請先從選單列結束它，再執行一次。"; return 1
  fi
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
