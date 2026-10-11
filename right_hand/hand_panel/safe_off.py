"""面板結束後的補救：上次結束時如果可能還留著扭力，送一次關扭力。

面板每次開扭力前會寫一個標記檔，確認 8 顆都關掉後才刪。面板被強制結束（SIGKILL、當掉）時標記會留著；
systemd 的 ExecStopPost 會跑這支程式。沒有標記就什麼都不做，不碰匯流排。
這只是第二層：面板和這支程式都跑不了的時候，只有伺服機電源上的實體開關能讓手放鬆。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from hand_api import motion
from hand_api.adapter import AdapterError, ScsHandAdapter
from hand_api.config import SERVO_IDS
from hand_api.daemon import DEFAULT_RUNTIME_DIR

from .core import MARKER_NAME


def main(argv=None, adapter_factory=ScsHandAdapter):
    ap = argparse.ArgumentParser(prog="hand_panel.safe_off")
    ap.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)
    args = ap.parse_args(argv)
    marker = os.path.join(args.runtime_dir, MARKER_NAME)
    try:
        with open(marker, encoding="utf-8") as f:
            info = json.load(f)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict) or info.get("adapter") != "scs" or not info.get("port"):
        os.unlink(marker)                  # 模擬，或標記讀不懂：沒有實體可以關
        return 0
    adapter = adapter_factory(info["port"])
    try:
        with adapter.connected():
            failed = motion.torque_off(adapter, SERVO_IDS)
    except AdapterError as e:
        print("面板上次結束時可能還留著扭力，但現在送不出關扭力（%s）。請切斷伺服機電源。" % e)
        return 0
    if failed:
        print("關扭力時這些 ID 沒有回應：%s。請切斷伺服機電源確認。" % failed)
        return 0
    os.unlink(marker)
    print("面板上次沒有正常關扭力；已對 8 顆送出關扭力。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
