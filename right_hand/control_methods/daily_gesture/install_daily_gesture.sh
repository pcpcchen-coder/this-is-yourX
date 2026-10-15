#!/usr/bin/env bash
# 在 Raspberry Pi（kergpt-1）上安裝「每天 07:00 自創手勢」。SSH 進 RPi 後執行：bash install_daily_gesture.sh
# 會寫入 ~/this-is-yourX/right_hand/control_methods/daily_gesture/ 與 ~/.config/systemd/user/，啟用 timer，最後做一次只檢查（不動）。
set -euo pipefail
DIR="$HOME/this-is-yourX/right_hand/control_methods/daily_gesture"
UNIT="$HOME/.config/systemd/user"
mkdir -p "$DIR" "$UNIT"
cat > "$DIR/daily_gesture.py" <<'__DG_PY__'
#!/usr/bin/env python3
"""每天早上的自創手勢：手有連線就從 Claude 自創的手勢表隨機挑一個，慢速擺一次再張開、關扭力。

2026-10-15 George 決定：這個動作不逐次核准、手勢不經工作台校正（偏離 ADR-0006 的 A2 與手勢登記流程）。
彌補的做法：只用網頁操作面板的公開 HTTP 介面（面板的落後／電壓／溫度／掉線／到位檢查與 fault 鎖定照舊），
只用慢速，側擺限 ±20°（面板允許 ±40°），一次只擺一個手勢，結束一定關扭力，任何前置條件不明就不動。

只用 Python 標準函式庫，在 Raspberry Pi 上由 systemd timer 執行：
  python3 daily_gesture.py              正式執行（手沒連線就記一筆 skipped）
  python3 daily_gesture.py --dry-run    只檢查連線與挑手勢，不動
  python3 daily_gesture.py --list       列出手勢表
  python3 daily_gesture.py --gesture claw   指定手勢（測試用）
"""
import argparse
import datetime
import json
import os
import random
import secrets
import sys
import time
import urllib.error
import urllib.request

FINGERS = ("index", "middle", "ring", "thumb")
FLEX_MIN, FLEX_MAX, SIDE_MAX, SERVO_MAX = -30.0, 90.0, 20.0, 90.0
JITTER_DEG = 5.0                  # 每天在彎曲上加的隨機變化
HOLD_S = 3.0                      # 擺好後停留
AVOID_LAST = 4                    # 不重複最近幾天用過的手勢
MOVE_TIMEOUT_S, TOTAL_TIMEOUT_S, WARMUP_S = 20.0, 60.0, 6.0
DEFAULT_PANEL = "http://127.0.0.1:8765"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.normpath(os.path.join(HERE, "..", "..", "logs", "daily_gesture.jsonl"))

# Claude 自創的手勢。(彎曲, 側擺)，度；彎曲 −30 張開、+90 握緊。側擺同號＝同方向
# （面板存的 right_swing：食指／中指／無名指 +40 同向、拇指相反），所以「張開成扇形」是食指 +、無名指 −。
GESTURES = {
    "claw":          ("爪子",     "四指都彎到一半，像要抓東西",                {"index": (45, 0), "middle": (45, 0), "ring": (45, 0), "thumb": (40, 0)}),
    "fan":           ("扇子",     "四指伸直，食指與無名指往外撇開",            {"index": (-30, 18), "middle": (-30, 0), "ring": (-30, -18), "thumb": (-30, 0)}),
    "staircase":     ("階梯",     "從食指到拇指一階比一階彎",                  {"index": (0, 0), "middle": (30, 0), "ring": (60, 0), "thumb": (85, 0)}),
    "staircase_rev": ("反階梯",   "從拇指到食指一階比一階彎",                  {"index": (60, 0), "middle": (30, 0), "ring": (0, 0), "thumb": (-30, 0)}),
    "rock_on":       ("搖滾",     "食指與無名指伸直，中指與拇指收起",          {"index": (-30, 0), "middle": (88, 0), "ring": (-30, 0), "thumb": (85, 0)}),
    "call_me":       ("打給我",   "拇指與無名指伸出，食指與中指收起",          {"index": (88, 0), "middle": (88, 0), "ring": (-30, 0), "thumb": (-30, 0)}),
    "trigger":       ("扣扳機",   "食指半彎扣著，拇指翹起，其他收起",          {"index": (40, 0), "middle": (88, 0), "ring": (88, 0), "thumb": (-30, 0)}),
    "bunny":         ("兔耳朵",   "食指與中指伸直略撇開，其他收起",            {"index": (-30, 12), "middle": (-30, -12), "ring": (85, 0), "thumb": (80, 0)}),
    "lean_left":     ("集體左傾", "四指伸直一起往同一側偏",                    {"index": (-30, 15), "middle": (-30, 15), "ring": (-30, 15), "thumb": (-30, 0)}),
    "lean_right":    ("集體右傾", "四指伸直一起往另一側偏",                    {"index": (-30, -15), "middle": (-30, -15), "ring": (-30, -15), "thumb": (-30, 0)}),
    "ripple":        ("漣漪",     "一指直、一指彎、一指再彎一點，像波紋",      {"index": (-30, 0), "middle": (25, 0), "ring": (65, 0), "thumb": (20, 0)}),
    "half_fist":     ("半拳",     "四指都彎到六成，還沒握緊",                  {"index": (60, 0), "middle": (60, 0), "ring": (60, 0), "thumb": (55, 0)}),
    "tilted_point":  ("歪頭指",   "食指伸直帶點側偏，其他收起",                {"index": (-30, 15), "middle": (88, 0), "ring": (88, 0), "thumb": (85, 0)}),
    "pinch_air":     ("空捏",     "食指與拇指彎向彼此但不碰，其他伸直",        {"index": (50, 0), "middle": (-30, 0), "ring": (-30, 0), "thumb": (45, 0)}),
    "thinker":       ("沉思",     "食指半彎、中指微彎、無名指收起、拇指微翹",  {"index": (35, 0), "middle": (55, 0), "ring": (85, 0), "thumb": (10, 0)}),
    "morning_stretch": ("早安伸懶腰", "四指伸直略撇開、拇指半彎，像剛睡醒",    {"index": (-25, 10), "middle": (-30, 0), "ring": (-25, -10), "thumb": (30, 0)}),
}


def problems_of(pose):
    out = []
    if set(pose) != set(FINGERS):
        out.append("手指要剛好是 %s" % "、".join(FINGERS))
    for f in FINGERS:
        v = pose.get(f)
        if not isinstance(v, dict) or set(v) != {"flex", "side"}:
            out.append("%s 要有 flex 與 side" % f)
            continue
        flex, side = v["flex"], v["side"]
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")
                   for x in (flex, side)):
            out.append("%s 的值要是有限數字" % f)
            continue
        if not FLEX_MIN <= flex <= FLEX_MAX:
            out.append("%s.flex %.1f 超出 %g–%g" % (f, flex, FLEX_MIN, FLEX_MAX))
        if abs(side) > SIDE_MAX:
            out.append("%s.side %.1f 超出 ±%g" % (f, side, SIDE_MAX))
        if abs(flex) + abs(side) > SERVO_MAX:
            out.append("%s 的 |flex|+|side| 超過 %g" % (f, SERVO_MAX))
    return out


def vary(base, rng):
    """在彎曲上加 ±JITTER_DEG 的隨機變化，收在限制內；側擺不變。"""
    out = {}
    for f, (flex, side) in base.items():
        room = SERVO_MAX - abs(side)
        flex = min(max(flex + rng.uniform(-JITTER_DEG, JITTER_DEG), FLEX_MIN), min(FLEX_MAX, room))
        out[f] = {"flex": round(flex, 1), "side": float(side)}
    return out


def recent_names(log_path, n):
    names = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("outcome") == "done" and rec.get("gesture"):
                    names.append(rec["gesture"])
    except FileNotFoundError:
        pass
    return names[-n:] if n else []


def pick(log_path, rng, forced=None):
    if forced:
        if forced not in GESTURES:
            raise SystemExit("沒有這個手勢：%s（--list 看清單）" % forced)
        return forced
    recent = set(recent_names(log_path, AVOID_LAST))
    choices = [g for g in sorted(GESTURES) if g not in recent] or sorted(GESTURES)
    return rng.choice(choices)


class Panel:
    def __init__(self, base, timeout=3.0):
        self.base = base.rstrip("/")
        self.client = "dailygesture" + secrets.token_hex(6)
        self.timeout = timeout

    def status(self):
        req = urllib.request.Request("%s/api/status?client=%s" % (self.base, self.client))
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path, body=None):
        data = json.dumps(dict(body or {}, client=self.client)).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "Origin": self.base})
        with urllib.request.urlopen(req, timeout=max(self.timeout, 8.0)) as r:
            return json.loads(r.read().decode("utf-8"))


class Abort(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


def summarize(st):
    servos = st.get("servos") or []
    ok = [s for s in servos if s.get("ok")]
    volts = [s["voltage_v"] for s in ok if isinstance(s.get("voltage_v"), (int, float))]
    temps = [s["temperature_c"] for s in ok if isinstance(s.get("temperature_c"), (int, float))]
    return {
        "simulated": st.get("simulated"), "bus": (st.get("bus") or {}).get("state"),
        "servos_ok": len(ok), "servos_total": len(servos),
        "voltage_min": min(volts) if volts else None, "voltage_max": max(volts) if volts else None,
        "temperature_max": max(temps) if temps else None, "torque_on": st.get("torque_on"),
        "fault": (st.get("fault") or {}).get("reason_code"),
    }


def skip_reason(snap, allow_simulated=False):
    if snap["simulated"] and not allow_simulated:
        return "SIMULATED_PANEL"
    if snap["bus"] != "attached":
        return "BUS_NOT_ATTACHED"
    if snap["servos_total"] != 8 or snap["servos_ok"] != 8:
        return "HAND_NOT_CONNECTED"
    if snap["fault"]:
        return "ACTIVE_FAULT"
    if snap["torque_on"]:
        return "SOMEONE_ELSE_IN_CONTROL"
    if snap["voltage_min"] is None or snap["voltage_min"] < 4.0 or snap["voltage_max"] > 7.4:
        return "VOLTAGE_OUT_OF_RANGE"
    if snap["temperature_max"] is None or snap["temperature_max"] > 55:
        return "TOO_WARM"
    return None


def run(panel, name, pose, hold_s=HOLD_S, dry_run=False, allow_simulated=False,
        sleep=time.sleep, monotonic=time.monotonic):
    t0 = monotonic()
    log = []

    def say(msg):
        log.append("%.1fs %s" % (monotonic() - t0, msg))

    rec = {"gesture": name, "pose": pose, "client": panel.client, "log": log}
    bad = problems_of(pose)
    if bad:
        return dict(rec, outcome="rejected", problems=bad)

    # 面板只在有客戶端回報時才接上匯流排、開始讀伺服機
    deadline = monotonic() + WARMUP_S
    while True:
        try:
            st = panel.status()
        except (urllib.error.URLError, OSError, ValueError) as e:
            return dict(rec, outcome="skipped", reason="PANEL_UNREACHABLE", detail=str(e))
        if st.get("status") != "ok":
            return dict(rec, outcome="skipped", reason="PANEL_BAD_RESPONSE", detail=str(st)[:200])
        snap = summarize(st)
        if (snap["bus"] == "attached" and snap["servos_ok"] == 8) or monotonic() > deadline:
            break
        sleep(0.3)
    rec["status"] = snap
    why = skip_reason(snap, allow_simulated)
    if why:
        return dict(rec, outcome="skipped", reason=why)
    if dry_run:
        return dict(rec, outcome="dry_run")

    def must(r, what):
        if not isinstance(r, dict) or r.get("status") not in ("accepted", "ok"):
            raise Abort((r or {}).get("reason_code") or "REJECTED", "%s：%s" % (what, (r or {}).get("detail", "")))
        return r

    def fresh():
        try:
            return panel.status()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise Abort("PANEL_UNREACHABLE", str(e))

    def wait_move(seq, label):
        until = monotonic() + MOVE_TIMEOUT_S
        misses = 0
        while monotonic() < until:
            sleep(0.25)
            if monotonic() - t0 > TOTAL_TIMEOUT_S:
                raise Abort("TOTAL_TIMEOUT")
            try:
                s = panel.status()
                misses = 0
            except (urllib.error.URLError, OSError, ValueError):
                misses += 1
                if misses >= 3:
                    raise Abort("PANEL_UNREACHABLE")
                continue
            if s.get("fault"):
                raise Abort(s["fault"].get("reason_code", "FAULT"), s["fault"].get("detail", ""))
            if not s.get("torque_on") or not s.get("you_control"):
                raise Abort("TORQUE_RELEASED", json.dumps(s.get("last_release"), ensure_ascii=False))
            m = s.get("last_move")
            if not s.get("moving") and m and m.get("target_seq") == seq:
                if not m.get("ok"):
                    raise Abort(m.get("reason_code", "MOVE_FAILED"), m.get("detail", ""))
                say("%s 到位（%.2f s）" % (label, m.get("elapsed_s", 0)))
                return s
        raise Abort("MOVE_TIMEOUT", label)

    outcome, failure, actual = "done", None, None
    try:
        must(panel.post("/api/enable", {"speed": "slow"}), "啟用扭力")
        say("扭力已啟用（慢速）")
        wait_move(must(panel.post("/api/open"), "張開")["target_seq"], "張開")
        r = must(panel.post("/api/fingers", {"fingers": pose}), "下手勢目標")
        if r.get("clamped"):
            say("面板收進範圍：%s" % ",".join(r["clamped"]))
        s = wait_move(r["target_seq"], "手勢 " + name)
        actual = {f: (s.get("fingers") or {}).get(f, {}).get("actual") for f in FINGERS}
        end = monotonic() + hold_s
        while monotonic() < end:          # 停留期間繼續回報（面板 3 秒沒回報就關扭力）
            sleep(min(0.4, max(0.0, end - monotonic())))
            s = fresh()
            if s.get("fault") or not s.get("torque_on"):
                raise Abort((s.get("fault") or {}).get("reason_code") or "TORQUE_RELEASED")
        say("停留 %.1f 秒" % hold_s)
        wait_move(must(panel.post("/api/open"), "收回張開")["target_seq"], "收回張開")
    except Abort as e:
        outcome, failure = "aborted", {"code": e.code, "detail": e.detail}
        say("中止：%s" % e.code)
    except Exception as e:                # 不認得的錯誤也一樣走到關扭力
        outcome, failure = "aborted", {"code": "INTERNAL_ERROR", "detail": "%s: %s" % (type(e).__name__, e)}
        say("中止：INTERNAL_ERROR")
    finally:
        try:
            off = panel.post("/api/stop")
            say("關扭力：%s" % off.get("status"))
        except Exception as e:
            say("關扭力送不出去：%s" % e)
        try:
            after = summarize(panel.status())
            rec["after"] = {"torque_on": after["torque_on"], "fault": after["fault"]}
        except Exception:
            rec["after"] = None
    return dict(rec, outcome=outcome, failure=failure, actual=actual, hold_s=hold_s,
                elapsed_s=round(monotonic() - t0, 2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="每天早上的自創手勢（經網頁操作面板，慢速）")
    ap.add_argument("--panel", default=os.environ.get("HAND_PANEL_URL", DEFAULT_PANEL))
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--gesture")
    ap.add_argument("--hold", type=float, default=HOLD_S)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--allow-simulated", action="store_true", help="測試用：面板是模擬時也照跑")
    args = ap.parse_args(argv)

    if args.list:
        for g, (label, desc, pose) in sorted(GESTURES.items()):
            print("%-16s %s：%s  %s" % (g, label, desc,
                                        " ".join("%s(%+g,%+g)" % (f, *pose[f]) for f in FINGERS)))
        return 0
    if not 0.5 <= args.hold <= 5.0:
        print("--hold 要在 0.5–5 秒")
        return 2
    for g, (_, _, pose) in GESTURES.items():   # 手勢表本身也要過限制
        bad = problems_of({f: {"flex": float(p[0]), "side": float(p[1])} for f, p in pose.items()})
        if bad:
            print("手勢表裡的 %s 不合規：%s" % (g, bad))
            return 2

    rng = random.Random(args.seed)
    name = pick(args.log, rng, args.gesture)
    label, desc, base = GESTURES[name]
    pose = vary(base, rng)
    rec = run(Panel(args.panel), name, pose, hold_s=args.hold, dry_run=args.dry_run,
              allow_simulated=args.allow_simulated)
    rec.update(at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"), label=label,
               description=desc, panel=args.panel)
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    with open(args.log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    head = {"done": "完成", "dry_run": "只檢查", "skipped": "沒動", "aborted": "中止", "rejected": "手勢不合規"}
    print("[%s] %s：%s（%s）" % (rec["at"], head.get(rec["outcome"], rec["outcome"]), label, name))
    if rec.get("reason"):
        print("  原因：%s" % rec["reason"])
    if rec.get("failure"):
        print("  失敗：%s %s" % (rec["failure"]["code"], rec["failure"].get("detail", "")))
    for line in rec["log"]:
        print("  " + line)
    return {"done": 0, "dry_run": 0, "skipped": 0}.get(rec["outcome"], 1)


if __name__ == "__main__":
    sys.exit(main())
__DG_PY__
cat > "$DIR/daily-gesture.service" <<'__DG_SVC__'
# 每天早上的自創手勢（一次性執行；由 daily-gesture.timer 叫起）。說明：right_hand/control_methods/daily_gesture/README.md
[Unit]
Description=Amazing Hand right hand: daily Claude-invented gesture (via operator panel, slow)

[Service]
Type=oneshot
WorkingDirectory=%h/this-is-yourX/right_hand
ExecStart=/usr/bin/python3 %h/this-is-yourX/right_hand/control_methods/daily_gesture/daily_gesture.py
TimeoutStartSec=120
__DG_SVC__
cat > "$DIR/daily-gesture.timer" <<'__DG_TMR__'
# 每天台北時間 07:00。錯過（主機關著）就不補做，免得開機後手突然自己動。
[Unit]
Description=Daily gesture at 07:00 Asia/Taipei

[Timer]
OnCalendar=*-*-* 07:00:00 Asia/Taipei
Persistent=false
AccuracySec=1s

[Install]
WantedBy=timers.target
__DG_TMR__
cp "$DIR/daily-gesture.service" "$DIR/daily-gesture.timer" "$UNIT/"
systemctl --user daemon-reload
systemctl --user enable --now daily-gesture.timer
echo
systemctl --user list-timers daily-gesture.timer --no-pager
echo
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "⚠ linger 沒開：你登出 SSH 後 systemd 使用者服務會停，07:00 不會觸發，hand-panel 也會停。"
  echo "  要讓它在沒登入時也跑：sudo loginctl enable-linger $USER"
fi
if ! systemctl --user is-active --quiet hand-panel; then
  echo "⚠ hand-panel 沒在跑：07:00 時面板連不到就只會記一筆 skipped。要開機自動啟動：systemctl --user enable hand-panel"
fi
echo
echo "== 只檢查一次（不動手）"
python3 "$DIR/daily_gesture.py" --dry-run || true
echo
echo "手動試跑一次（會動）：python3 $DIR/daily_gesture.py"
echo "看紀錄：tail -n 5 ~/this-is-yourX/right_hand/logs/daily_gesture.jsonl；journalctl --user -u daily-gesture -n 30"
echo "停用：systemctl --user disable --now daily-gesture.timer"
