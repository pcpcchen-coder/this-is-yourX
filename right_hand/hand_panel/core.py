"""操作面板的核心：人在瀏覽器上逐指操作右手、存下與重現姿勢。

這是給「人」用的工作台工具，和 tools/servo_tool.py 同一類；它不是 AI 的控制路徑，
AI 的動作仍然只走 hand_api 的 skill gateway（ADR-0006）。

做法：
  - 所有碰匯流排的事都在一條 worker 執行緒裡做（tick()）。HTTP 那一側只改「想要的狀態」。
  - 沿用 hand_api.adapter（含跨程式的匯流排鎖）與 hand_api.motion.move_together（落後、電壓、到位檢查）。
  - 沒有瀏覽器開著頁面時不佔匯流排，servo_tool.py、handd 可以照常使用。
  - 啟動時不寫任何東西。只有「啟用扭力」會開扭力，而且先把目標設成目前位置。
  - 控制者的頁面超過 heartbeat_timeout_s 沒回報、閒置超過 hold_timeout_s、任何檢查沒過，都關扭力。
  - 檢查沒過會鎖住 fault；人看過、按「清除」之後才能再啟用扭力。
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import threading
import time

from hand_api import motion
from hand_api.adapter import AdapterError, HandReading
from hand_api.config import FINGER_KEYS, FINGER_NAMES_ZH, POSE_LIMIT_DEG, SERVO_IDS, SERVO_LIMIT_DEG
from hand_api.gateway import STATE_FRESH_S, TEMP_MAX_C, VOLT_RANGE, _iso

from .poses import NAME_RE, StoreError, StoreFull

# 滑桿範圍（度）。彎曲 = (奇 − 偶) / 2，側擺 = (奇 + 偶) / 2，都不含中位修正。
# 彎曲的兩端是 bring-up 工具在這隻手上走過的張開（−30）與閉合（90）；側擺的 ±40 只有拇指走過 38.5。
FLEX_RANGE_DEG = (motion.OPEN_DEG, 90.0)
SIDE_RANGE_DEG = (-40.0, 40.0)
HEARTBEAT_TIMEOUT_S = 3.0     # 控制者的頁面這麼久沒回報就關扭力
DETACH_AFTER_S = 6.0          # 沒有任何頁面回報這麼久就放開匯流排
HOLD_TIMEOUT_S = 60.0         # 有扭力但沒有新目標這麼久就關扭力（免得伺服機一直出力發熱）
POLL_PERIOD_S = 0.25
REQUEST_WAIT_S = 4.0
MEASURED_FRESH_S = 1.0        # 存「讀到的位置」時，讀值不能比這個舊
CLIENT_KEEP_S = 60.0
MARKER_NAME = "panel_torque.json"   # 開扭力前寫、確認關掉後刪；見 safe_off.py

FINGER_ODD = {key: sid for sid, key in FINGER_KEYS.items()}     # "index" → 1
FINGER_ORDER = ("thumb", "index", "middle", "ring")


class Rejected(Exception):
    def __init__(self, reason_code, detail):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def finger_to_servo(flex, side):
    return side + flex, side - flex


def servo_to_finger(a, b):
    return (a - b) / 2.0, (a + b) / 2.0


def number(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise Rejected("PARAMETER_OUT_OF_RANGE", "%s 要是有限數字" % name)
    return float(v)


def clamp_finger(flex, side):
    """把彎曲與側擺收進滑桿範圍，並讓兩顆伺服機都在 ±POSE_LIMIT_DEG 內。回傳 (彎曲, 側擺, 有沒有被改)。"""
    f = min(max(flex, FLEX_RANGE_DEG[0]), FLEX_RANGE_DEG[1])
    room = POSE_LIMIT_DEG - abs(f)
    s = min(max(side, max(SIDE_RANGE_DEG[0], -room)), min(SIDE_RANGE_DEG[1], room))
    return f, s, (f != flex or s != side)


def check_reading(reading, now):
    """和 hand_api.gateway.Gateway._check_reading 相同的前置條件（有測試確認兩邊一致）。"""
    missing = [sid for sid, r in reading.servos.items() if not r.ok]
    if len(missing) == len(SERVO_IDS):
        return "RAIL_DOWN", "8 顆都沒有回應（伺服機電源關著、開關切斷或 USB 沒接）"
    if missing:
        return "SERVO_MISSING", "這些 ID 沒有回應：%s" % missing
    age = now - min(r.observed_at for r in reading.servos.values())
    if age > STATE_FRESH_S:
        return "STALE_STATE", "狀態是 %.0f ms 前的，超過 %d ms" % (age * 1000, STATE_FRESH_S * 1000)
    for r in reading.servos.values():
        for name, v in (("position_deg", r.position_deg), ("voltage_v", r.voltage_v),
                        ("temperature_c", r.temperature_c)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return "STATE_UNKNOWN", "ID %d 的 %s 讀值無效（%r）" % (r.sid, name, v)
        if not (VOLT_RANGE[0] <= r.voltage_v <= VOLT_RANGE[1]):
            return "VOLTAGE_OUT_OF_RANGE", "ID %d 電壓 %.1f V，不在 %.1f–%.1f V" % (r.sid, r.voltage_v, *VOLT_RANGE)
        if r.temperature_c > TEMP_MAX_C:
            return "OVER_TEMPERATURE", "ID %d 溫度 %.0f °C，超過 %.0f °C" % (r.sid, r.temperature_c, TEMP_MAX_C)
    return None, None


class _Request:
    def __init__(self, **kw):
        self.kw = kw
        self.done = threading.Event()
        self.result = None
        self.started = False
        self.cancelled = False


class _Tap:
    """包住 adapter：把移動途中讀到的位置記下來，頁面在移動時也看得到實際位置。"""

    def __init__(self, adapter, sink):
        self._a = adapter
        self._sink = sink

    def read_position(self, sid):
        v = self._a.read_position(sid)
        self._sink(sid, v)
        return v

    def read_voltage(self, sid):
        return self._a.read_voltage(sid)

    def set_goal(self, sid, deg):
        self._a.set_goal(sid, deg)

    def set_speed(self, sid, rad_s):
        self._a.set_speed(sid, rad_s)

    def set_torque(self, sid, on):
        self._a.set_torque(sid, on)


class Panel:
    def __init__(self, cfg, adapter, audit, store, clock=time.time, monotonic=time.monotonic, sleep=time.sleep,
                 hold_timeout_s=HOLD_TIMEOUT_S, heartbeat_timeout_s=HEARTBEAT_TIMEOUT_S,
                 detach_after_s=DETACH_AFTER_S, marker_path=None):
        self.cfg = cfg
        self.adapter = adapter
        self.audit = audit
        self.store = store
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.hold_timeout_s = float(hold_timeout_s)
        self.heartbeat_timeout_s = float(heartbeat_timeout_s)
        self.detach_after_s = float(detach_after_s)
        self.marker_path = marker_path
        self._tap = _Tap(adapter, self._saw_position)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._thread = None
        self._conn = None                 # 只有 worker 碰：attached 時是 ExitStack
        self._retry_at = 0.0
        self._bus = {"state": "detached", "reason_code": None, "detail": ""}
        self._beats = {}                  # client → (monotonic, 來源位址)
        self._torque = False
        self._controller = None
        self._speed = "slow"
        self._applied_speed = None
        self._fingers = {}                # 手指 → (彎曲, 側擺) 的目標
        self._target_seq = 0
        self._done_seq = 0
        self._cancel = threading.Event()
        self._moving = False
        self._last_move = None
        self._last_release = None
        self._fault = None
        self._requests = []               # 等著處理的「啟用扭力」
        self._release_reason = None
        self._release_caller = None
        self._release_waiters = []
        self._reading = None
        self._live = {}                   # 移動途中讀到的位置 {sid: 度}
        self._last_activity = 0.0

    # ================================================================== 給 HTTP 那一側用（不碰匯流排）
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="hand-panel-worker", daemon=True)
        self._thread.start()

    def shutdown(self, timeout_s=8.0):
        self._shutdown.set()
        with self._lock:
            self._cancel.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout_s)
        else:
            self._safe_tick()
            self._detach()

    def beat(self, client, remote=None):
        now = self.monotonic()
        with self._lock:
            self._beats[client] = (now, remote)
            for c in [c for c, (t, _) in self._beats.items() if now - t > CLIENT_KEEP_S]:
                del self._beats[c]
        self._wake.set()

    def _caller(self, client):
        with self._lock:
            remote = self._beats.get(client, (0, None))[1]
        return "operator.web:%s%s" % (str(client)[:8], "@%s" % remote if remote else "")

    def _audit(self, type_, caller, **data):
        try:
            return self.audit.write(type_, caller, None, **data)
        except Exception as e:          # 稽核寫不進去不能卡住關扭力
            print("警告：稽核紀錄寫入失敗：%s" % e, file=sys.stderr)
            return None

    def _envelope(self, status, **kw):
        d = {"status": status, "observed_at": _iso(self.clock())}
        d.update(kw)
        return d

    def _finger_view(self, a, b):
        flex, side = servo_to_finger(a, b)
        return {"flex": round(flex, 1), "side": round(side, 1)}

    def status(self, client=None):
        now_m, now = self.monotonic(), self.clock()
        mids = self.cfg.middle_offsets
        with self._lock:
            reading, live = self._reading, dict(self._live)
            pos, servos = {}, []
            for sid in SERVO_IDS:
                r = reading.servos.get(sid) if reading else None
                row = {"id": sid, "ok": bool(r and r.ok)}
                if r and r.ok:
                    pos[sid] = r.position_deg - mids[sid]
                    row.update(voltage_v=r.voltage_v, temperature_c=r.temperature_c, torque_on=r.torque_on)
                elif r:
                    row["error"] = r.error
                if sid in live:
                    pos[sid] = live[sid] - mids[sid]
                if sid in pos and math.isfinite(pos[sid]):
                    row["position_deg"] = round(pos[sid], 1)
                servos.append(row)
            fingers = {}
            for key in FINGER_ORDER:
                a = FINGER_ODD[key]
                t = self._fingers.get(key)
                actual = None
                if a in pos and a + 1 in pos and math.isfinite(pos[a]) and math.isfinite(pos[a + 1]):
                    actual = self._finger_view(pos[a], pos[a + 1])
                fingers[key] = {
                    "label": FINGER_NAMES_ZH[a], "servo_ids": [a, a + 1],
                    "target": None if t is None else {"flex": round(t[0], 1), "side": round(t[1], 1)},
                    "actual": actual,
                }
            hold = None
            if self._torque and not self._moving and self._target_seq == self._done_seq:
                hold = round(max(0.0, self.hold_timeout_s - (now_m - self._last_activity)), 1)
            return self._envelope(
                "ok", adapter=self.adapter.kind, simulated=not self.adapter.real_hardware,
                calibration_revision=self.cfg.calibration_revision, bus=dict(self._bus),
                torque_on=self._torque, you_control=bool(client) and self._controller == client,
                controller=self._controller[:8] if self._controller else None,
                speed=self._speed, speeds=list(motion.SPEEDS), moving=self._moving or self._target_seq != self._done_seq,
                fault=dict(self._fault) if self._fault else None, hold_remaining_s=hold,
                hold_timeout_s=self.hold_timeout_s, heartbeat_timeout_s=self.heartbeat_timeout_s,
                limits={"flex": list(FLEX_RANGE_DEG), "side": list(SIDE_RANGE_DEG), "servo": POSE_LIMIT_DEG},
                reading_age_ms=None if reading is None else int(max(0.0, now - reading.observed_at) * 1000),
                fingers=fingers, servos=servos, last_move=self._last_move, last_release=self._last_release)

    def _wait(self, req, timeout=REQUEST_WAIT_S):
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            if not req.done.wait(timeout):
                with self._lock:
                    if not req.started:
                        req.cancelled = True
                if req.cancelled or not req.done.wait(timeout):
                    with self._lock:
                        req.cancelled = True
                    raise Rejected("TIMEOUT", "面板沒有在 %.0f 秒內處理這個請求" % timeout)
        else:                              # 測試：沒有 worker 執行緒，就在這裡跑
            for _ in range(20):
                if req.done.is_set():
                    break
                self.tick()
            if not req.done.is_set():
                req.cancelled = True
                raise Rejected("TIMEOUT", "請求沒有被處理")
        if isinstance(req.result, Rejected):
            raise req.result
        return req.result

    def enable(self, client, speed="slow"):
        if speed not in motion.SPEEDS:
            raise Rejected("UNKNOWN_SPEED", "速度只能是 %s" % "、".join(motion.SPEEDS))
        self.beat(client, self._beats.get(client, (0, None))[1])
        with self._lock:
            if self._fault:
                raise Rejected("ACTIVE_FAULT", "有還沒清除的 fault：%s" % self._fault["reason_code"])
            if self._torque:
                if self._controller == client:
                    return self._envelope("accepted", torque_on=True, already=True)
                raise Rejected("CONTROL_HELD", "另一個頁面正在控制這隻手；那一頁按停止後才能接手")
            if self._shutdown.is_set():
                raise Rejected("SHUTTING_DOWN", "面板正在結束")
            req = _Request(client=client, speed=speed)
            self._requests.append(req)
        try:
            return self._wait(req)
        except Rejected as e:
            self._audit("torque.rejected", self._caller(client), reason_code=e.reason_code, detail=e.detail)
            raise

    def stop(self, client=None):
        """取消進行中的移動並關扭力。任何開著的頁面都可以按，永遠接受。"""
        with self._lock:
            if self._release_reason is None:
                self._release_reason = "stop"
            self._release_caller = self._caller(client) if client else "operator.web"
            self._cancel.set()
            for r in self._requests:
                r.cancelled = True
                r.result = Rejected("STOPPED", "收到停止")
                r.done.set()
            self._requests = []
            req = _Request()
            self._release_waiters.append(req)
        try:
            return self._wait(req)
        except Rejected as e:
            return self._envelope("accepted", torque_off="unknown", detail=e.detail)

    def clear_fault(self, client):
        with self._lock:
            old, self._fault = self._fault, None
        self._audit("fault.cleared", self._caller(client), previous=old)
        return self._envelope("ok", cleared=old)

    def _require_control(self, client):
        if not self._torque:
            raise Rejected("TORQUE_OFF", "扭力關著。先按「啟用扭力」")
        if self._controller != client:
            raise Rejected("CONTROL_HELD", "另一個頁面正在控制這隻手")
        if self._release_reason is not None or self._shutdown.is_set():
            raise Rejected("STOPPING", "正在關扭力")

    def set_speed(self, client, speed):
        if speed not in motion.SPEEDS:
            raise Rejected("UNKNOWN_SPEED", "速度只能是 %s" % "、".join(motion.SPEEDS))
        with self._lock:
            self._require_control(client)
            self._speed = speed
        return self._envelope("ok", speed=speed)

    def _commit_targets(self, new):
        """呼叫時已持有鎖。檢查含中位修正後的伺服機限制，再換上新目標。"""
        for key, (flex, side) in new.items():
            a = FINGER_ODD[key]
            for sid, deg in zip((a, a + 1), finger_to_servo(flex, side)):
                if abs(deg) > POSE_LIMIT_DEG + 1e-6 or abs(deg + self.cfg.middle_offsets[sid]) > SERVO_LIMIT_DEG:
                    raise Rejected("SERVO_LIMIT", "ID %d 的目標 %+.1f° 超出限制" % (sid, deg))
        self._fingers = new
        self._target_seq += 1
        self._cancel.set()
        self._last_activity = self.monotonic()
        return self._target_seq

    def set_fingers(self, client, fingers):
        if not isinstance(fingers, dict) or not fingers:
            raise Rejected("PARAMETER_NOT_ALLOWED", "fingers 要是 {手指: {flex, side}}")
        parsed = {}
        for key, v in fingers.items():
            if key not in FINGER_ODD:
                raise Rejected("UNKNOWN_COMPONENT", "沒有這根手指：%r" % (key,))
            if not isinstance(v, dict) or not v or set(v) - {"flex", "side"}:
                raise Rejected("PARAMETER_NOT_ALLOWED", "%s 只接受 flex 與 side" % key)
            parsed[key] = {k: number(x, "%s.%s" % (key, k)) for k, x in v.items()}
        with self._lock:
            self._require_control(client)
            new, clamped = dict(self._fingers), []
            for key, v in parsed.items():
                cur = new[key]
                flex, side, _ = clamp_finger(v.get("flex", cur[0]), v.get("side", cur[1]))
                # 只有「被要求的那一軸」和結果不同才算被收進範圍；另一軸沿用原本的目標
                if ("flex" in v and flex != v["flex"]) or ("side" in v and side != v["side"]):
                    clamped.append(key)
                new[key] = (flex, side)
            seq = self._commit_targets(new)
            view = {k: {"flex": round(new[k][0], 1), "side": round(new[k][1], 1)} for k in parsed}
        self._wake.set()
        return self._envelope("accepted", target_seq=seq, fingers=view, clamped=clamped)

    def open_hand(self, client):
        with self._lock:
            self._require_control(client)
            seq = self._commit_targets({key: (motion.OPEN_DEG, 0.0) for key in FINGER_ORDER})
        self._wake.set()
        return self._envelope("accepted", target_seq=seq)

    # ------------------------------------------------------------------ 姿勢
    def _pose_fingers(self, pose):
        return {FINGER_KEYS[a]: self._finger_view(*pose[a]) for a in sorted(pose)}

    def _pose_problem(self, pose, revision):
        if revision != self.cfg.calibration_revision:
            return "CALIBRATION_REVISION_CHANGED", ("存檔時的校正版本是 %s，現在是 %s；確認姿勢仍然正確後按「重新確認」"
                                                    % (revision or "（沒有）", self.cfg.calibration_revision))
        for a, pair in pose.items():
            for sid, deg in zip((a, a + 1), pair):
                if abs(deg) > POSE_LIMIT_DEG or abs(deg + self.cfg.middle_offsets[sid]) > SERVO_LIMIT_DEG:
                    return "SERVO_LIMIT", "ID %d 的角度 %+.1f° 超出限制" % (sid, deg)
        return None, None

    def list_poses(self):
        items, error = [], None
        for g in self.cfg.gestures.values():
            pose = {a: list(p) for a, p in g.pose.items()}
            code, why = (None, None)
            if g.status != "confirmed":
                code, why = "GESTURE_NOT_CALIBRATED", "手勢表裡的狀態是 %s" % g.status
            else:
                code, why = self._pose_problem(pose, g.calibration_revision)
            items.append({"name": g.name, "label": g.label, "note": g.description, "kind": "gesture",
                          "playable": code is None, "reason_code": code, "why_not": why,
                          "fingers": self._pose_fingers(pose), "saved_at": None, "source": "gestures.yaml"})
        try:
            saved = self.store.load()
        except StoreError as e:
            saved, error = {}, str(e)
        for name, rec in saved.items():
            code, why = self._pose_problem(rec["pose"], rec["calibration_revision"])
            items.append({"name": name, "label": rec["label"], "note": rec["note"], "kind": "saved",
                          "playable": code is None, "reason_code": code, "why_not": why,
                          "fingers": self._pose_fingers(rec["pose"]), "saved_at": rec["saved_at"],
                          "source": rec["source"]})
        return self._envelope("ok", poses=items, error=error, path=self.store.path,
                              calibration_revision=self.cfg.calibration_revision)

    def _find_pose(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise Rejected("UNKNOWN_POSE", "姿勢名稱只能是小寫英數與底線")
        g = self.cfg.gestures.get(name)
        if g is not None:
            if g.status != "confirmed":
                raise Rejected("GESTURE_NOT_CALIBRATED", "手勢 %s 的狀態是 %s" % (name, g.status))
            return {a: list(p) for a, p in g.pose.items()}, g.calibration_revision
        try:
            rec = self.store.load().get(name)
        except StoreError as e:
            raise Rejected("POSE_FILE_INVALID", str(e))
        if rec is None:
            raise Rejected("UNKNOWN_POSE", "沒有叫 %s 的姿勢" % name)
        return rec["pose"], rec["calibration_revision"]

    def play_pose(self, client, name):
        pose, revision = self._find_pose(name)
        code, why = self._pose_problem(pose, revision)
        if code:
            raise Rejected(code, why)
        with self._lock:
            self._require_control(client)
            seq = self._commit_targets({FINGER_KEYS[a]: servo_to_finger(*pose[a]) for a in pose})
        self._wake.set()
        self._audit("pose.play", self._caller(client), name=name, pose={str(k): v for k, v in pose.items()})
        return self._envelope("accepted", target_seq=seq, name=name)

    def save_pose(self, client, name, label="", note="", overwrite=False):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise Rejected("BAD_NAME", "名稱只能是小寫英數與底線，最多 32 個字，例如 pinch_small")
        if name in self.cfg.gestures:
            raise Rejected("NAME_TAKEN", "%s 是手勢表（gestures.yaml）裡的名稱，換一個" % name)
        if not isinstance(label, str) or not isinstance(note, str) or not isinstance(overwrite, bool):
            raise Rejected("PARAMETER_NOT_ALLOWED", "label、note 要是文字，overwrite 要是 true/false")
        mids = self.cfg.middle_offsets
        with self._lock:
            if self._moving or self._target_seq != self._done_seq:
                raise Rejected("BUSY", "手還在移動，停下來再存")
            reading = self._reading
            measured = None
            if reading is not None and not reading.servos.keys() - set(reading.responding):
                # 剛移動完、下一輪完整讀取還沒來時，移動途中最後讀到的位置比較新
                vals = {sid: self._live.get(sid, reading.servos[sid].position_deg) - mids[sid] for sid in SERVO_IDS}
                if all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
                       for v in vals.values()):
                    measured = {a: [round(vals[a], 1), round(vals[a + 1], 1)] for a in FINGER_KEYS}
            if self._torque:
                source = "target"
                pose = {FINGER_ODD[k]: [round(x, 1) for x in finger_to_servo(*self._fingers[k])] for k in FINGER_ORDER}
            else:
                source = "measured"
                age = None if reading is None else self.clock() - reading.observed_at
                if measured is None or age is None or age > MEASURED_FRESH_S:
                    raise Rejected("STATE_UNKNOWN", "現在讀不到 8 顆的位置，沒有東西可以存")
                pose = measured
        for a, pair in pose.items():
            for sid, deg in zip((a, a + 1), pair):
                if abs(deg) > POSE_LIMIT_DEG:
                    raise Rejected("SERVO_LIMIT", "ID %d 現在在 %+.1f°，超過姿勢表的 ±%d°，不存" % (sid, deg, POSE_LIMIT_DEG))
        record = {"label": label.strip(), "note": note.strip(), "saved_at": _iso(self.clock()),
                  "calibration_revision": self.cfg.calibration_revision, "source": source, "pose": pose}
        if measured is not None:
            record["measured"] = measured
        try:
            saved = self.store.save(name, record, overwrite=overwrite)
        except KeyError:
            raise Rejected("NAME_TAKEN", "已經有叫 %s 的姿勢。要覆蓋就再按一次「覆蓋」" % name)
        except StoreFull as e:
            raise Rejected("TOO_MANY_POSES", str(e))
        except StoreError as e:
            raise Rejected("POSE_FILE_INVALID", str(e))
        self._audit("pose.saved", self._caller(client), name=name, source=source, overwrite=overwrite,
                    pose={str(k): v for k, v in saved["pose"].items()})
        return self._envelope("ok", name=name, source=source, fingers=self._pose_fingers(saved["pose"]))

    def delete_pose(self, client, name):
        if isinstance(name, str) and name in self.cfg.gestures:
            raise Rejected("NOT_PERMITTED", "%s 在手勢表（gestures.yaml）裡，面板不能刪" % name)
        try:
            removed = self.store.delete(name if isinstance(name, str) else "")
        except KeyError:
            raise Rejected("UNKNOWN_POSE", "沒有叫 %s 的姿勢" % (name,))
        except StoreError as e:
            raise Rejected("POSE_FILE_INVALID", str(e))
        self._audit("pose.deleted", self._caller(client), name=name, pose={str(k): v for k, v in removed["pose"].items()})
        return self._envelope("ok", name=name)

    def reconfirm_pose(self, client, name):
        if isinstance(name, str) and name in self.cfg.gestures:
            raise Rejected("NOT_PERMITTED", "手勢表裡的手勢要照 hand_api_design.md §5 的流程重新確認")
        try:
            self.store.update(name if isinstance(name, str) else "", calibration_revision=self.cfg.calibration_revision)
        except KeyError:
            raise Rejected("UNKNOWN_POSE", "沒有叫 %s 的姿勢" % (name,))
        except StoreError as e:
            raise Rejected("POSE_FILE_INVALID", str(e))
        self._audit("pose.reconfirmed", self._caller(client), name=name,
                    calibration_revision=self.cfg.calibration_revision)
        return self._envelope("ok", name=name)

    def log_tail(self, n=20):
        try:
            n = max(1, min(int(n), 100))
        except (TypeError, ValueError):
            n = 20
        return self._envelope("ok", events=self.audit.tail(n))

    # ================================================================== worker（唯一碰匯流排的地方）
    def _loop(self):
        while not self._shutdown.is_set():
            self._wake.clear()
            if self._safe_tick():
                self._wake.wait(POLL_PERIOD_S)
        self._safe_tick()                  # 結束前：有扭力就關
        self._detach()

    def _safe_tick(self):
        try:
            return self.tick()
        except Exception as e:             # 不讓 worker 死掉；有扭力就關，並鎖住 fault
            detail = "%s: %s" % (type(e).__name__, e)
            print("面板內部錯誤：%s" % detail, file=sys.stderr)
            try:
                self._release("fault", fault=self._make_fault("INTERNAL_ERROR", detail))
            except Exception as e2:
                print("關扭力時又出錯：%s" % e2, file=sys.stderr)
            return True

    def _clients_active(self, now):
        return any(now - t <= self.detach_after_s for t, _ in self._beats.values())

    def _controller_alive(self, now):
        t = self._beats.get(self._controller, (None, None))[0]
        return t is not None and now - t <= self.heartbeat_timeout_s

    def _saw_position(self, sid, deg):
        with self._lock:
            self._live[sid] = deg

    def _make_fault(self, code, detail):
        return {"reason_code": code, "detail": detail, "at": _iso(self.clock())}

    def tick(self):
        """做一輪工作。回傳 True 表示現在沒事可做（worker 可以睡一下）。"""
        now = self.monotonic()
        with self._lock:
            if self._torque and self._release_reason is None and not self._controller_alive(now):
                self._release_reason = "watchdog"
                self._release_caller = "panel.watchdog"
            torque = self._torque
            if torque and self._shutdown.is_set() and self._release_reason is None:
                self._release_reason = "shutdown"
                self._release_caller = "panel"
            release = self._release_reason is not None or bool(self._release_waiters)
            enable = bool(self._requests)
            active = self._clients_active(now)
        if self._shutdown.is_set() and not release:
            self._fail_pending("SHUTTING_DOWN", "面板正在結束")
            return True
        if not (torque or release or active):
            self._detach()
            return True
        if self._conn is None and not self._attach(now, force=release):
            self._fail_pending(self._bus["reason_code"], self._bus["detail"])
            return True
        if release:
            self._release("stop")
            return False
        if not torque:
            if enable:
                self._handle_enable()
                return False
            self._read_all()
            return True
        with self._lock:
            late, self._requests = self._requests, []          # 扭力已經開著才排到的「啟用」：不做
            controller = self._controller
            pending = self._target_seq != self._done_seq
        for r in late:
            if r.kw["client"] == controller:
                r.result = self._envelope("accepted", torque_on=True, already=True)
            else:
                r.result = Rejected("CONTROL_HELD", "另一個頁面正在控制這隻手；那一頁按停止後才能接手")
            r.done.set()
        if pending:
            self._move()
            return False
        self._hold()
        return True

    def _attach(self, now, force=False):
        if not force and now < self._retry_at:
            return False
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(self.adapter.connected())
        except AdapterError as e:
            self._retry_at = now + 1.0
            with self._lock:
                self._bus = {"state": "unavailable", "reason_code": e.code, "detail": str(e)}
                self._reading, self._live = None, {}
            return False
        self._conn = stack
        with self._lock:
            self._bus = {"state": "attached", "reason_code": None, "detail": ""}
        return True

    def _detach(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
        with self._lock:
            if self._bus["state"] != "unavailable" or conn is not None:
                self._bus = {"state": "detached", "reason_code": None, "detail": ""}
            self._reading, self._live = None, {}

    def _fail_pending(self, code, detail):
        with self._lock:
            requests, self._requests = self._requests, []
            waiters, self._release_waiters = self._release_waiters, []
            if waiters and not self._torque:
                self._release_reason = None
        for r in requests:
            r.result = Rejected(code or "BUS_UNAVAILABLE", detail or "拿不到匯流排")
            r.done.set()
        for w in waiters:
            w.result = self._envelope("accepted", torque_off="unknown", reason_code=code,
                                      detail="沒有辦法送出關扭力（%s）。手如果有扭力，請切斷伺服機電源。" % detail)
            w.done.set()

    def _read_all(self):
        servos = {sid: self.adapter.read_servo(sid) for sid in SERVO_IDS}
        reading = HandReading(servos=servos, observed_at=max(r.observed_at for r in servos.values()),
                              adapter=self.adapter.kind)
        with self._lock:
            self._reading, self._live = reading, {}
        return reading

    def _problem(self, reading):
        """有扭力時每一輪都要過的檢查。回傳 fault 或 None。"""
        code, detail = check_reading(reading, self.clock())
        if code:
            return self._make_fault(code, detail)
        lost = [sid for sid, r in reading.servos.items() if not r.torque_on]
        if lost:
            return self._make_fault("TORQUE_LOST", "這些 ID 的扭力自己關了（斷過電？）：%s" % lost)
        return None

    def _write_marker(self):
        if not self.marker_path:
            return
        try:
            with open(self.marker_path, "w", encoding="utf-8") as f:
                json.dump({"adapter": self.adapter.kind, "port": getattr(self.adapter, "port", None),
                           "at": _iso(self.clock())}, f)
        except OSError as e:
            print("警告：寫不了 %s：%s" % (self.marker_path, e), file=sys.stderr)

    def _clear_marker(self):
        if self.marker_path:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.marker_path)

    def _handle_enable(self):
        with self._lock:
            if not self._requests:
                return
            req = self._requests.pop(0)
            if req.cancelled:
                return
            req.started = True
            fault = self._fault
        client, speed = req.kw["client"], motion.SPEEDS[req.kw["speed"]]
        try:
            if fault:
                req.result = Rejected("ACTIVE_FAULT", "有還沒清除的 fault：%s" % fault["reason_code"])
                return
            reading = self._read_all()
            code, detail = check_reading(reading, self.clock())
            if code:
                req.result = Rejected(code, detail)
                return
            self._write_marker()
            try:
                motion.engage(self.adapter, SERVO_IDS, speed.servo_rad_s)
            except Exception as e:
                failed = motion.torque_off(self.adapter, SERVO_IDS)
                detail = "開扭力途中出錯（%s）；已送出關扭力%s" % (
                    e, "" if not failed else "，但這些 ID 沒有成功：%s。請切斷伺服機電源" % failed)
                with self._lock:
                    self._fault = self._make_fault("TORQUE_OFF_FAILED" if failed else "SERVO_LOST", detail)
                if not failed:
                    self._clear_marker()
                req.result = Rejected(self._fault["reason_code"], detail)
                return
            mids = self.cfg.middle_offsets
            with self._lock:
                self._torque = True
                self._controller = client
                self._speed = self._applied_speed = speed.name
                self._fingers = {
                    key: servo_to_finger(reading.servos[a].position_deg - mids[a],
                                         reading.servos[a + 1].position_deg - mids[a + 1])
                    for key, a in FINGER_ODD.items()}
                self._done_seq = self._target_seq
                self._last_activity = self.monotonic()
                self._last_move = None
                self._last_release = None
            self._audit("torque.on", self._caller(client), speed=speed.name,
                        positions={str(s): round(reading.servos[s].position_deg, 1) for s in SERVO_IDS})
            req.result = self._envelope("accepted", torque_on=True, speed=speed.name)
        finally:
            req.done.set()
            if req.cancelled and self._torque:     # 等的人已經放棄了：不留著扭力
                with self._lock:
                    self._release_reason = self._release_reason or "enable_timeout"

    def _move_sleep(self, s):
        self.sleep(s)
        with self._lock:                   # 移動途中也看著控制者；頁面不見了就取消
            if self._release_reason is None and not self._controller_alive(self.monotonic()):
                self._release_reason = "watchdog"
                self._release_caller = "panel.watchdog"
                self._cancel.set()

    def _servo_targets(self, fingers):
        out = {}
        for key, (flex, side) in fingers.items():
            a = FINGER_ODD[key]
            da, db = finger_to_servo(flex, side)
            out[a] = da + self.cfg.middle_offsets[a]
            out[a + 1] = db + self.cfg.middle_offsets[a + 1]
        return dict(sorted(out.items()))

    def _move(self):
        with self._lock:
            reading = self._reading
        if reading is None or self.clock() - reading.observed_at > STATE_FRESH_S:
            fault = self._problem(self._read_all())
            if fault:
                self._release("fault", fault=fault)
                return
        with self._lock:
            if self._release_reason is not None or self._shutdown.is_set():
                return
            cancel = self._cancel = threading.Event()
            seq = self._target_seq
            targets = self._servo_targets(self._fingers)
            speed = motion.SPEEDS[self._speed]
            self._moving = True
        try:
            if self._applied_speed != speed.name:
                for sid in SERVO_IDS:
                    self.adapter.set_speed(sid, speed.servo_rad_s)
                self._applied_speed = speed.name
            out = motion.move_together(self._tap, targets, speed, cancel=cancel, sleep=self._move_sleep,
                                       monotonic=self.monotonic)
        except Exception as e:
            out = motion.MoveOutcome(ok=False, reason_code="SERVO_LOST" if isinstance(e, AdapterError)
                                     else "INTERNAL_ERROR", detail="%s: %s" % (type(e).__name__, e))
        summary = {"ok": out.ok, "reason_code": out.reason_code, "detail": out.detail, "rounds": out.rounds,
                   "elapsed_s": round(out.elapsed_s, 2), "speed": speed.name, "target_seq": seq,
                   "final": {str(k): v for k, v in out.final.items()}, "at": _iso(self.clock())}
        fault = None
        with self._lock:
            self._moving = False
            if out.ok:
                self._done_seq = seq
                self._last_activity = self.monotonic()
                self._last_move = summary
            elif out.reason_code != "CANCELLED":
                self._last_move = summary
                fault = self._make_fault(out.reason_code, out.detail)
            controller = self._controller
        if out.ok or fault:
            self._audit("move.finished", self._caller(controller) if controller else "panel",
                        targets={str(k): round(v, 1) for k, v in targets.items()}, **summary)
        if fault:
            self._release("fault", fault=fault)

    def _hold(self):
        fault = self._problem(self._read_all())
        if fault:
            self._release("fault", fault=fault)
            return
        with self._lock:
            idle = self.monotonic() - self._last_activity
            if idle > self.hold_timeout_s and self._release_reason is None:
                self._release_reason = "hold_timeout"
                self._release_caller = "panel.hold_timeout"

    def _release(self, reason, fault=None):
        """關 8 顆扭力，回到「扭力關」的狀態。fault 不是 None 時鎖住，之後要人清除。"""
        failed = motion.torque_off(self.adapter, SERVO_IDS) if self._conn is not None else list(SERVO_IDS)
        with self._lock:
            was = self._torque
            reason = self._release_reason or reason
            caller = self._release_caller or "panel"
            self._release_reason = None
            self._release_caller = None
            waiters, self._release_waiters = self._release_waiters, []
            self._torque = False
            self._controller = None
            self._moving = False
            self._applied_speed = None
            self._fingers = {}
            self._done_seq = self._target_seq
            if failed and was and fault is None:
                fault = self._make_fault("TORQUE_OFF_FAILED",
                                         "這些 ID 關扭力沒有成功：%s。請切斷伺服機電源。" % failed)
            if fault and not self._fault:
                self._fault = fault
            self._last_release = {"reason": reason, "torque_off_failed": failed, "at": _iso(self.clock()),
                                  "fault": fault["reason_code"] if fault else None}
        if not failed:
            self._clear_marker()
        if was or waiters or fault:
            self._audit("torque.off", caller, reason=reason, was_on=was, torque_off_failed=failed,
                        fault=fault)
        result = self._envelope("accepted", torque_off="ok" if not failed else "unknown",
                                torque_off_failed=failed, was_on=was)
        for w in waiters:
            w.result = result
            w.done.set()
