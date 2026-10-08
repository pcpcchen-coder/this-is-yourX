"""操作者指令 `hand`：只連 handd 的操作者 socket。

  hand status            手的狀態、進行中的執行、fault、待核准數
  hand pending           列出待核准的提議
  hand approve <id>      核准一筆提議（要在互動終端機確認）
  hand stop              取消進行中的動作並關扭力
  hand clear-fault       看過失敗原因後，清除 fault
  hand log [n]           最近 n 筆稽核事件（預設 20）
"""
from __future__ import annotations

import json
import sys

from . import client


def _call(method, params=None):
    try:
        return client.call(client.socket_path("operator"), method, params)
    except client.HanddUnavailable as e:
        print("%s。先在另一個終端機執行：bash right_hand/tools/handd.sh" % e)
        sys.exit(2)


def _print_status(r):
    print("狀態：%s　adapter：%s　模擬：%s　實機動作：%s　核准方式：逐次核准" % (
        r["status"], r.get("adapter"), r.get("simulated"), "允許" if r.get("real_motion_enabled") else "拒絕"))
    st = r.get("state")
    if st:
        print("讀值時間：%s（%d ms 前，%s）" % (st["observed_at"], st["age_ms"], st["source"]))
        print("電源軌：%s　沒回應的 ID：%s" % ({True: "有電", None: "unknown"}.get(st["rail_up"], st["rail_up"]),
                                         st["servos_missing"] or "無"))
        for s in st["servos"]:
            if s["ok"]:
                print("  ID %d  %+6.1f°  %.1f V  %d °C  扭力%s" % (s["id"], s["position_deg"], s["voltage_v"],
                                                            s["temperature_c"], "開" if s["torque_on"] else "關"))
            else:
                print("  ID %d  沒有回應" % s["id"])
    elif r.get("reason_code"):
        print("讀不到狀態：%s（%s）" % (r["reason_code"], r.get("detail", "")))
    print("進行中的執行：%s　fault：%s　待核准：%s" % (r.get("active_execution") or "無",
                                             (r.get("active_fault") or {}).get("reason_code", "無"),
                                             r.get("pending_proposals", 0)))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "status" and not rest:
        _print_status(_call("hand_status"))
        return 0
    if cmd == "pending" and not rest:
        r = _call("pending")
        if not r["proposals"]:
            print("沒有待核准的提議。")
        for p in r["proposals"]:
            print("%s  %s  %s  （%d 秒後過期%s）" % (p["proposal_id"], p["skill"], json.dumps(p["parameters"], ensure_ascii=False),
                                              p["expires_in_s"], "，已核准" if p["approved"] else ""))
            print("    %s" % p["description"])
        return 0
    if cmd == "approve" and len(rest) == 1:
        pid = rest[0]
        match = [p for p in _call("pending")["proposals"] if p["proposal_id"] == pid]
        if not match:
            print("沒有這筆待核准的提議：%s（可能已過期或已執行）。用 hand pending 查。" % pid)
            return 1
        p = match[0]
        print("要核准的動作：%s" % p["description"])
        print("skill：%s　參數：%s" % (p["skill"], json.dumps(p["parameters"], ensure_ascii=False)))
        if not sys.stdin.isatty():
            print("核准要在互動終端機輸入，不接受管線或腳本輸入。")
            return 1
        print("確認手的周圍淨空、一隻手放在伺服機電源開關上。")
        if input("核准請輸入 y：").strip().lower() != "y":
            print("沒有核准。")
            return 1
        r = _call("approve", {"proposal_id": pid})
        if r["status"] == "approved":
            print("已核准，%s 前有效。請回到對話讓 AI 執行。" % r["approval_expires_at"])
            return 0
        print("核准失敗：%s（%s）" % (r.get("reason_code"), r.get("detail")))
        return 1
    if cmd == "stop" and not rest:
        r = _call("stop")
        print("已送出停止。取消的執行：%s　關扭力：%s" % (r.get("cancelled_execution") or "無",
                                             r.get("execution_status") or r.get("torque_off")))
        return 0
    if cmd == "clear-fault" and not rest:
        r = _call("clear_fault")
        old = r.get("cleared")
        print("已清除 fault：%s" % (("%s（%s）" % (old["reason_code"], old["detail"])) if old else "原本就沒有"))
        return 0
    if cmd == "log" and len(rest) <= 1:
        n = int(rest[0]) if rest else 20
        for e in _call("log", {"n": n})["events"]:
            print("%s  %-20s %-14s %s  %s" % (e["at"], e["type"], e["caller"], e.get("trace_id") or "",
                                             json.dumps(e["data"], ensure_ascii=False)[:160]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
