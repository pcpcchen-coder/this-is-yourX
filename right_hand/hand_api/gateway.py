"""Skill gateway：前置條件、逐次核准、執行狀態機、稽核。全部是確定性程式，不含任何模型。

AI 能做的事：查詢、提議（propose）、執行已被操作者核准的提議（execute）、停止。
只有操作者能核准（approve）與清除 fault（clear_fault）；呼叫者身分由 daemon 依 socket 決定。
任何前置條件是 false 或 unknown 都拒絕。P2 階段對真硬體一律拒絕動作（REAL_MOTION_NOT_ENABLED）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import secrets
import sys
import threading
import time

from .adapter import AdapterError, HandReading
from .config import FINGER_KEYS, HAND_ID, SERVO_IDS, SERVO_LIMIT_DEG
from . import motion

AI = "ai.mcp"
OPERATOR = "operator.cli"

PROPOSAL_TTL_S = 120.0
APPROVAL_TTL_S = 60.0
STATE_FRESH_S = 0.5
VOLT_RANGE = (4.0, 7.4)
TEMP_MAX_C = 60.0
RATE_LIMIT_PER_MIN = 6
HOLD_RANGE_S = (0.5, 30.0)
REASON_MAX_CHARS = 200
MOTION_SKILLS = ("hand_open", "hand_gesture")
ROUND_S_ESTIMATE = 0.045   # 2026-10-07 實測每輪約 42 ms


def _iso(t):
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


class AuditLog:
    """只增不改的 JSONL 事件檔。"""

    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def write(self, type_, caller, trace_id=None, **data):
        event_id = "evt_" + secrets.token_hex(6)
        rec = {"event_id": event_id, "type": type_, "at": _iso(self.clock()), "caller": caller,
               "trace_id": trace_id, "data": data}
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return event_id

    def tail(self, n=20):
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except FileNotFoundError:
            return []
        return [json.loads(x) for x in lines if x.strip()]


def _thread_executor(fn):
    t = threading.Thread(target=fn, name="hand-execution", daemon=True)
    t.start()
    return t


class Gateway:
    def __init__(self, config, adapter, audit, clock=time.time, sleep=time.sleep,
                 monotonic=time.monotonic, executor=_thread_executor, real_motion_enabled=False):
        self.cfg = config
        self.adapter = adapter
        self.audit = audit
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.executor = executor
        # P2：寫死為 False。打開它是 P3 的工作（實體開關、故障注入實測、experiment ID）。
        self.real_motion_enabled = bool(real_motion_enabled) and not adapter.real_hardware
        self._lock = threading.RLock()        # 保護下面的狀態
        self._bus = threading.Lock()          # 同一時間只有一個人用 adapter
        self.proposals = {}
        self.executions = {}
        self.idempotency = {}
        self.accepted_times = []
        self.fault = None
        self.running = None                   # (execution_id, cancel Event, thread)
        self.last_reading = None
        self._stop_seq = 0                    # stop() 每呼叫一次加一；execute 檢查期間若變了就放棄

    # ------------------------------------------------------------------ 讀取
    def _read_all(self):
        """讀 8 顆。拿不到匯流排（另一個程式在用、正在執行動作）時丟 AdapterError。"""
        if not self._bus.acquire(blocking=False):
            raise AdapterError("BUSY", "正在執行動作")
        try:
            with self.adapter.connected():
                servos = {sid: self.adapter.read_servo(sid) for sid in SERVO_IDS}
            reading = HandReading(servos=servos, observed_at=max(r.observed_at for r in servos.values()),
                                  adapter=self.adapter.kind)
            self.last_reading = reading
            return reading
        finally:
            self._bus.release()

    def _servo_view(self, r):
        mid = self.cfg.middle_offsets[r.sid]
        d = {"id": r.sid, "component": self.cfg.servo_component(r.sid), "ok": r.ok}
        if r.ok:
            d.update(position_deg=round(r.position_deg - mid, 1), voltage_v=r.voltage_v,
                     temperature_c=r.temperature_c, torque_on=r.torque_on)
        else:
            d["error"] = r.error
        return d

    def _finger_view(self, reading):
        out = {}
        for a, key in FINGER_KEYS.items():
            ra, rb = reading.servos[a], reading.servos[a + 1]
            if ra.ok and rb.ok:
                pa = ra.position_deg - self.cfg.middle_offsets[a]
                pb = rb.position_deg - self.cfg.middle_offsets[a + 1]
                out[key] = {"flexion_deg": round((pa - pb) / 2, 1), "abduction_deg": round((pa + pb) / 2, 1)}
            else:
                out[key] = {"status": "unknown"}
        return out

    def _reading_view(self, reading, source):
        now = self.clock()
        missing = [sid for sid, r in reading.servos.items() if not r.ok]
        return {
            "observed_at": _iso(reading.observed_at),
            "age_ms": int(max(0.0, now - reading.observed_at) * 1000),
            "source": source,
            "rail_up": None if len(missing) == len(SERVO_IDS) else (True if not missing else None),
            "servos_missing": missing,
            "servos": [self._servo_view(r) for r in reading.servos.values()],
            "fingers": self._finger_view(reading),
        }

    def _envelope(self, status, **kw):
        d = {"status": status, "observed_at": _iso(self.clock()), "graph_revision": self.cfg.graph_revision}
        d.update(kw)
        return d

    def _reject(self, reason_code, detail, caller=AI, event_type=None, trace_id=None, **extra):
        event_id = None
        if event_type:
            event_id = self.audit.write(event_type, caller, trace_id, reason_code=reason_code, detail=detail, **extra)
        return self._envelope("rejected", reason_code=reason_code, detail=detail, event_id=event_id, **extra)

    def hand_status(self):
        with self._lock:
            running = self.running[0] if self.running else None
            fault = dict(self.fault) if self.fault else None
            pending = sum(1 for p in self.proposals.values() if self._proposal_live(p))
        base = dict(adapter=self.adapter.kind, simulated=not self.adapter.real_hardware,
                    real_motion_enabled=self.real_motion_enabled, approval_mode="per_action",
                    active_execution=running, active_fault=fault, pending_proposals=pending,
                    calibration_revision=self.cfg.calibration_revision)
        try:
            reading = self._read_all()
            return self._envelope("ok", state=self._reading_view(reading, "live"), **base)
        except AdapterError as e:
            if e.code == "BUSY" and self.last_reading is not None:
                return self._envelope("ok", state=self._reading_view(self.last_reading, "cached_during_execution"), **base)
            return self._envelope("unknown", reason_code=e.code, detail=str(e), state=None, **base)

    def list_components(self, kind=None):
        items = []
        for cid, c in self.cfg.components.items():
            if kind and c.get("kind") != kind:
                continue
            items.append({"id": cid, "kind": c.get("kind"), "name": c.get("name"), "parent_id": c.get("parent_id"),
                          "aliases": [a["value"] for a in c.get("aliases", []) if a.get("reviewed")]})
        return self._envelope("ok", components=items)

    def get_component(self, component_id, include=("state",)):
        c = self.cfg.components.get(component_id)
        if c is None:
            return self._envelope("unknown", reason_code="UNKNOWN_COMPONENT",
                                  detail="沒有這個元件：%s。用 self_list_components 查有哪些。" % component_id)
        include = set(include or ())
        bad = include - {"state", "relations", "capabilities", "limits"}
        if bad:
            return self._envelope("rejected", reason_code="PARAMETER_NOT_ALLOWED", detail="include 不支援：%s" % sorted(bad))
        out = {"id": component_id, "kind": c.get("kind"), "name": c.get("name"),
               "aliases": c.get("aliases", [])}
        if "capabilities" in include:
            out["capabilities"] = c.get("capabilities", [])
        if "limits" in include:
            out["limits"] = c.get("limits", [])
        if "relations" in include:
            out["parent_id"] = c.get("parent_id")
            out["children"] = [k for k, v in self.cfg.components.items() if v.get("parent_id") == component_id]
        if "state" in include:
            out["state"] = self._component_state(component_id, c)
        return self._envelope("ok", component=out)

    def _component_state(self, cid, c):
        try:
            reading = self._read_all()
            source = "live"
        except AdapterError as e:
            if e.code == "BUSY" and self.last_reading is not None:
                reading, source = self.last_reading, "cached_during_execution"
            else:
                return {"status": "unknown", "reason_code": e.code, "detail": str(e)}
        view = self._reading_view(reading, source)
        ident = c.get("binding", {}).get("device_identity", "")
        if ident.startswith("scs:"):
            sid = int(ident[4:])
            return dict(status="ok", observed_at=view["observed_at"], age_ms=view["age_ms"], source=source,
                        **next(s for s in view["servos"] if s["id"] == sid))
        for a, key in FINGER_KEYS.items():
            if cid == "%s.%s" % (HAND_ID, key):
                return dict(status="ok", observed_at=view["observed_at"], age_ms=view["age_ms"], source=source,
                            **view["fingers"][key])
        if cid == "power.servo_rail":
            return {"status": "ok", "up": view["rail_up"], "servos_missing": view["servos_missing"],
                    "observed_at": view["observed_at"], "age_ms": view["age_ms"]}
        if cid == "safety.estop":
            return {"status": "unknown", "detail": "手動電源開關沒有回讀線路；開關切斷時伺服機不回應，電源軌狀態會變成 unknown。"}
        return dict(status="ok", **view)

    def resolve_reference(self, text, locale=None):
        q = (text or "").strip().lower()
        hits = []
        for cid, c in self.cfg.components.items():
            for a in c.get("aliases", []):
                if a["value"].strip().lower() == q and (locale is None or a["locale"] == locale):
                    hits.append({"id": cid, "reviewed": bool(a.get("reviewed"))})
        uniq = {}
        for h in hits:
            uniq[h["id"]] = uniq.get(h["id"], False) or h["reviewed"]
        if not uniq:
            return self._envelope("unknown", candidates=[], detail="沒有元件叫「%s」。這個系統目前只有右手。" % text)
        cands = [{"id": k, "reviewed": v} for k, v in uniq.items()]
        if len(cands) == 1 and cands[0]["reviewed"]:
            return self._envelope("resolved", component_id=cands[0]["id"], candidates=cands)
        return self._envelope("ambiguous", candidates=cands, required_action="ask_user",
                              detail="「%s」不是審核過的唯一名稱，請向使用者確認。" % text)

    def skill_list(self):
        available, unavailable = [], []
        for g in self.cfg.gestures.values():
            item = {"name": g.name, "label": g.label, "description": g.description}
            ok, code = self._gesture_usable(g)
            if ok:
                available.append(item)
            else:
                unavailable.append(dict(item, reason_code=code))
        skills = []
        for name, s in self.cfg.skills.items():
            d = {"name": name, "version": s["version"], "authority_level": s["authority_level"],
                 "requires_approval": s["requires_approval"], "timeout_s": s["timeout_s"]}
            if name == "hand_open":
                d["parameters"] = {"speed": {"enum": list(motion.SPEEDS), "default": "slow"}}
            elif name == "hand_gesture":
                d["parameters"] = {"gesture": {"enum": [g["name"] for g in available]},
                                   "speed": {"enum": list(motion.SPEEDS), "default": "slow"},
                                   "hold_s": {"minimum": HOLD_RANGE_S[0], "maximum": HOLD_RANGE_S[1], "default": 3.0}}
                d["gestures"] = available
                d["gestures_unavailable"] = unavailable
            skills.append(d)
        return self._envelope("ok", skills=skills, approval_mode="per_action",
                              simulated=not self.adapter.real_hardware)

    def execution_get(self, execution_id):
        with self._lock:
            e = self.executions.get(execution_id)
            if e is None:
                return self._envelope("unknown", reason_code="UNKNOWN_EXECUTION", detail="沒有這個執行：%s" % execution_id)
            return self._envelope("ok", execution=json.loads(json.dumps(e, default=str)))

    # ------------------------------------------------------------------ 提議
    def _gesture_usable(self, g):
        if g.status != "confirmed":
            return False, "GESTURE_NOT_CALIBRATED"
        if g.calibration_revision != self.cfg.calibration_revision:
            return False, "CALIBRATION_REVISION_CHANGED"
        return True, None

    def _validate(self, skill, params):
        """回傳 (正規化後的參數, None) 或 (None, (reason_code, detail))。"""
        if not isinstance(params, dict):
            return None, ("PARAMETER_NOT_ALLOWED", "parameters 要是物件")
        allowed = {"hand_open": {"speed"}, "hand_gesture": {"gesture", "speed", "hold_s"}}[skill]
        extra = sorted(set(params) - allowed)
        if extra:
            return None, ("PARAMETER_NOT_ALLOWED", "%s 不接受這些參數：%s（只能用 %s）" % (skill, extra, sorted(allowed)))
        speed = params.get("speed", "slow")
        if speed not in motion.SPEEDS:
            return None, ("PARAMETER_OUT_OF_RANGE", "speed 只能是 %s" % "、".join(motion.SPEEDS))
        out = {"speed": speed}
        if skill == "hand_gesture":
            name = params.get("gesture")
            if not isinstance(name, str) or name not in self.cfg.gestures:
                return None, ("UNKNOWN_GESTURE", "沒有這個手勢：%r。用 skill_list 查可用的。" % (name,))
            ok, code = self._gesture_usable(self.cfg.gestures[name])
            if not ok:
                return None, (code, "手勢 %s 目前不能用（%s）" % (name, code))
            hold = params.get("hold_s", 3.0)
            if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not math.isfinite(hold) \
                    or not (HOLD_RANGE_S[0] <= hold <= HOLD_RANGE_S[1]):
                return None, ("PARAMETER_OUT_OF_RANGE", "hold_s 要在 %.1f–%.0f 秒" % HOLD_RANGE_S)
            out.update(gesture=name, hold_s=float(hold))
        return out, None

    def _targets(self, skill, params):
        mids = self.cfg.middle_offsets
        opened = motion.open_targets(mids, SERVO_IDS)
        plan = [("open", opened)]
        if skill == "hand_gesture":
            g = self.cfg.gestures[params["gesture"]]
            pose = dict(opened)
            for a in g.order:
                da, db = g.pose[a]
                pose[a], pose[a + 1] = da + mids[a], db + mids[a + 1]
            plan += [("pose", pose), ("hold", None), ("return", opened)]
        return plan

    def _describe(self, skill, params, reason):
        sp = motion.SPEEDS[params["speed"]]
        rounds = math.ceil(120.0 / sp.step_deg)
        if skill == "hand_open":
            text = "四指同時張開，到位後關扭力。速度 %s。" % sp.name
            est = rounds * ROUND_S_ESTIMATE + 1.0
        else:
            g = self.cfg.gestures[params["gesture"]]
            text = "比「%s」（%s），停 %.1f 秒，再張開並關扭力。速度 %s。" % (g.label, g.description, params["hold_s"], sp.name)
            est = 3 * rounds * ROUND_S_ESTIMATE + params["hold_s"] + 3.0
        return text + " 理由：" + (reason or "（未提供）"), round(est, 1)

    def _proposal_live(self, p):
        return not p["consumed"] and self.clock() < p["expires_at"]

    def propose(self, skill, target_component, parameters, reason="", caller=AI):
        if skill not in MOTION_SKILLS or skill not in self.cfg.skills:
            return self._reject("UNKNOWN_SKILL", "沒有這個 skill：%r。可提議的只有 %s；停止用 hand_stop。" % (skill, "、".join(MOTION_SKILLS)),
                                caller, "proposal.rejected", skill=skill)
        if target_component not in self.cfg.components:
            return self._reject("UNKNOWN_COMPONENT", "沒有這個元件：%r。這個系統只有 %s。" % (target_component, HAND_ID),
                                caller, "proposal.rejected", skill=skill)
        if target_component != HAND_ID:
            return self._reject("WRONG_TARGET", "%s 只能作用在 %s" % (skill, HAND_ID), caller, "proposal.rejected", skill=skill)
        if not isinstance(reason, str) or len(reason) > REASON_MAX_CHARS:
            return self._reject("PARAMETER_OUT_OF_RANGE", "reason 要是 %d 字以內的文字" % REASON_MAX_CHARS,
                                caller, "proposal.rejected", skill=skill)
        params, err = self._validate(skill, parameters)
        if err:
            return self._reject(err[0], err[1], caller, "proposal.rejected", skill=skill, parameters=parameters)
        for phase, targets in self._targets(skill, params):
            if targets and any(abs(v) > SERVO_LIMIT_DEG for v in targets.values()):
                return self._reject("PARAMETER_OUT_OF_RANGE", "加上中位修正後超出 ±%d°" % SERVO_LIMIT_DEG,
                                    caller, "proposal.rejected", skill=skill)
        body = {"skill": skill, "target_component": target_component, "parameters": params}
        phash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        pid = "prop_" + secrets.token_hex(4)
        text, est = self._describe(skill, params, reason)
        now = self.clock()
        p = {"proposal_id": pid, "params_hash": phash, "reason": reason, "caller": caller,
             "created_at": now, "expires_at": now + PROPOSAL_TTL_S, "description": text,
             "estimated_duration_s": est, "approval": None, "consumed": False, **body}
        with self._lock:
            self.proposals[pid] = p
        eid = self.audit.write("proposal.created", caller, pid, skill=skill, parameters=params,
                               params_hash=phash, reason=reason, description=text)
        return self._envelope(
            "proposed", proposal_id=pid, skill=skill, parameters=params, params_hash=phash,
            description=text, estimated_duration_s=est, expires_at=_iso(p["expires_at"]),
            requires_approval=True, simulated=not self.adapter.real_hardware, event_id=eid,
            next_step=("請使用者在接著手的電腦上執行 `hand approve %s` 核准；核准後 %d 秒內呼叫 skill_execute。"
                       % (pid, APPROVAL_TTL_S)))

    def pending(self):
        with self._lock:
            items = [p for p in self.proposals.values() if self._proposal_live(p)]
        now = self.clock()
        return self._envelope("ok", proposals=[{
            "proposal_id": p["proposal_id"], "skill": p["skill"], "parameters": p["parameters"],
            "description": p["description"], "caller": p["caller"],
            "expires_in_s": int(p["expires_at"] - now), "approved": bool(p["approval"])} for p in items])

    def approve(self, proposal_id, caller=OPERATOR):
        if caller != OPERATOR:
            return self._reject("NOT_PERMITTED", "只有操作者能核准", caller, "approval.rejected", proposal_id)
        with self._lock:
            p = self.proposals.get(proposal_id)
            if p is None:
                return self._reject("UNKNOWN_PROPOSAL", "沒有這個提議：%s" % proposal_id, caller, "approval.rejected", proposal_id)
            if p["consumed"]:
                return self._reject("PROPOSAL_USED", "這個提議已經執行過", caller, "approval.rejected", proposal_id)
            if self.clock() >= p["expires_at"]:
                return self._reject("PROPOSAL_EXPIRED", "這個提議已過期，請 AI 重新提議", caller, "approval.rejected", proposal_id)
            now = self.clock()
            p["approval"] = {"by": caller, "at": now, "expires_at": now + APPROVAL_TTL_S, "params_hash": p["params_hash"]}
        eid = self.audit.write("approval.granted", caller, proposal_id, params_hash=p["params_hash"],
                               description=p["description"])
        return self._envelope("approved", proposal_id=proposal_id, description=p["description"],
                              approval_expires_at=_iso(now + APPROVAL_TTL_S), event_id=eid)

    # ------------------------------------------------------------------ 執行
    def _check_reading(self, reading):
        missing = [sid for sid, r in reading.servos.items() if not r.ok]
        if len(missing) == len(SERVO_IDS):
            return "RAIL_DOWN", "8 顆都沒有回應（伺服機電源關著、開關切斷或 USB 沒接）"
        if missing:
            return "SERVO_MISSING", "這些 ID 沒有回應：%s" % missing
        age = self.clock() - min(r.observed_at for r in reading.servos.values())
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

    def execute(self, proposal_id, idempotency_key, caller=AI):
        def rej(code, detail, **kw):
            return self._reject(code, detail, caller, "execution.rejected", proposal_id, **kw)

        if not isinstance(idempotency_key, str) or not (1 <= len(idempotency_key) <= 64):
            return rej("PARAMETER_OUT_OF_RANGE", "idempotency_key 要是 1–64 字的文字")
        with self._lock:
            prev = self.idempotency.get(idempotency_key)
            if prev is not None:
                if prev[0] != proposal_id:
                    return rej("IDEMPOTENCY_CONFLICT", "這個 idempotency_key 已用在別的提議")
                return self._envelope("accepted", execution_id=prev[1], duplicate=True,
                                      detail="同一個 idempotency_key 重送，沒有再執行一次")
            p = self.proposals.get(proposal_id)
            if p is None:
                return rej("UNKNOWN_PROPOSAL", "沒有這個提議：%s" % proposal_id)
            if p["consumed"]:
                return rej("PROPOSAL_USED", "這個提議已經執行過，要再動一次請重新提議")
            now = self.clock()
            if now >= p["expires_at"]:
                return rej("PROPOSAL_EXPIRED", "這個提議已過期，請重新提議")
            a = p["approval"]
            if a is None:
                return rej("APPROVAL_REQUIRED", "還沒有被核准。請使用者在接著手的電腦上執行 `hand approve %s`。" % proposal_id)
            if now >= a["expires_at"]:
                return rej("APPROVAL_EXPIRED", "核准已過期（%d 秒）。請重新提議並核准。" % APPROVAL_TTL_S)
            if a["params_hash"] != p["params_hash"]:
                return rej("PROPOSAL_MISMATCH", "核准的內容和提議不一致")
            if self.adapter.real_hardware and not self.real_motion_enabled:
                return rej("REAL_MOTION_NOT_ENABLED",
                           "這一版（P2）只在模擬上執行動作；實機要等實體開關裝好、故障注入實測過（P3）。")
            if self.fault:
                return rej("ACTIVE_FAULT", "上一次執行失敗（%s），要操作者看過後執行 `hand clear-fault`。" % self.fault["reason_code"])
            if self.running:
                return rej("BUSY", "正在執行 %s" % self.running[0])
            self.accepted_times = [t for t in self.accepted_times if now - t < 60.0]
            if len(self.accepted_times) >= RATE_LIMIT_PER_MIN:
                return rej("RATE_LIMITED", "一分鐘內最多執行 %d 次" % RATE_LIMIT_PER_MIN)
            if not self._bus.acquire(blocking=False):
                return rej("BUSY", "匯流排正被使用")
            stop_seq = self._stop_seq
        # 從這裡開始持有 _bus；交給執行緒之後由它釋放，其他任何路徑都在 finally 釋放。
        handed = False
        try:
            try:
                with self.adapter.connected():
                    servos = {sid: self.adapter.read_servo(sid) for sid in SERVO_IDS}
            except AdapterError as e:
                return rej(e.code, str(e))
            reading = HandReading(servos=servos, observed_at=max(r.observed_at for r in servos.values()),
                                  adapter=self.adapter.kind)
            self.last_reading = reading
            code, detail = self._check_reading(reading)
            if code:
                return rej(code, detail)
            xid = "exec_" + secrets.token_hex(4)
            cancel = threading.Event()
            with self._lock:
                if self._stop_seq != stop_seq:
                    return rej("STOPPED", "檢查期間收到停止，沒有執行")
                if p["consumed"] or self.running or self.fault:
                    return rej("BUSY", "狀態在檢查期間改變，請重試")
                # 先寫稽核再改狀態：稽核寫不進去就整筆不執行。
                eid = self.audit.write("execution.accepted", caller, proposal_id, execution_id=xid,
                                       skill=p["skill"], parameters=p["parameters"], params_hash=p["params_hash"],
                                       approved_by=a["by"], preconditions="all true",
                                       simulated=not self.adapter.real_hardware,
                                       calibration_revision=self.cfg.calibration_revision)
                p["consumed"] = True
                rec = {"execution_id": xid, "proposal_id": proposal_id, "skill": p["skill"],
                       "parameters": p["parameters"], "status": "accepted",
                       "simulated": not self.adapter.real_hardware, "created_at": _iso(now),
                       "started_at": None, "finished_at": None, "reason_code": None, "detail": "",
                       "phases": [], "torque_off_failed": []}
                self.executions[xid] = rec
                self.idempotency[idempotency_key] = (proposal_id, xid)
                self.accepted_times.append(now)
                self.running = (xid, cancel, None)
            try:
                thread = self.executor(lambda: self._run(xid, p, cancel))
            except Exception as e:
                # 執行緒沒起來：沒有動作發生，但要把狀態收乾淨並鎖 fault，讓人看一下。
                with self._lock:
                    self.running = None
                    rec.update(status="failed", reason_code="INTERNAL_ERROR", detail="執行緒無法啟動：%s" % e,
                               finished_at=_iso(self.clock()))
                    self.fault = {"reason_code": "INTERNAL_ERROR", "detail": rec["detail"], "execution_id": xid,
                                  "at": _iso(self.clock())}
                self._audit_safe("execution.finished", "handd", proposal_id, execution_id=xid, status="failed",
                                 reason_code="INTERNAL_ERROR", detail=rec["detail"])
                return self._envelope("error", reason_code="INTERNAL_ERROR", detail=rec["detail"], execution_id=xid)
            handed = True
            with self._lock:
                if self.running and self.running[0] == xid:
                    self.running = (xid, cancel, thread)
            return self._envelope("accepted", execution_id=xid, simulated=rec["simulated"], event_id=eid,
                                  detail="已開始執行。用 execution_get 查結果，看到 succeeded 之前不要說已完成。")
        finally:
            if not handed:
                self._bus.release()

    def _audit_safe(self, *args, **kw):
        """執行中與收尾時的稽核：寫不進去也不能卡住狀態或匯流排。"""
        try:
            return self.audit.write(*args, **kw)
        except Exception as e:
            print("警告：稽核紀錄寫入失敗：%s" % e, file=sys.stderr)
            return None

    def _set(self, xid, **kw):
        with self._lock:
            self.executions[xid].update(kw)

    def _phase(self, xid, item):
        with self._lock:
            self.executions[xid]["phases"].append(item)

    def _run(self, xid, p, cancel):
        status, code, detail = "failed", "INTERNAL_ERROR", "未完成"
        try:
            skill, params = p["skill"], p["parameters"]
            speed = motion.SPEEDS[params["speed"]]
            status, code, detail = "succeeded", None, ""
            self._set(xid, status="running", started_at=_iso(self.clock()))
            self._audit_safe("execution.running", "handd", p["proposal_id"], execution_id=xid)
            with self.adapter.connected():
                try:
                    motion.engage(self.adapter, SERVO_IDS, speed.servo_rad_s)
                    for phase, targets in self._targets(skill, params):
                        if phase == "hold":
                            for _ in range(max(1, math.ceil(params["hold_s"] / 0.1))):
                                if cancel.is_set():
                                    break
                                self.sleep(0.1)
                            self._phase(xid, {"phase": "hold", "ok": not cancel.is_set(), "hold_s": params["hold_s"]})
                            if cancel.is_set():
                                status, code, detail = "cancelled", "CANCELLED", "停留時被取消"
                                break
                            continue
                        out = motion.move_together(self.adapter, targets, speed, cancel=cancel,
                                                   sleep=self.sleep, monotonic=self.monotonic)
                        self._phase(xid, {
                            "phase": phase, "ok": out.ok, "rounds": out.rounds, "elapsed_s": round(out.elapsed_s, 2),
                            "final": {str(k): v for k, v in out.final.items()}, "reason_code": out.reason_code,
                            "detail": out.detail})
                        if not out.ok:
                            if out.reason_code == "CANCELLED":
                                status, code, detail = "cancelled", "CANCELLED", out.detail
                            else:
                                status, code, detail = "failed", out.reason_code, out.detail
                            break
                except AdapterError as e:
                    status, code, detail = "failed", "SERVO_LOST", str(e)
                except Exception as e:
                    status, code, detail = "failed", "INTERNAL_ERROR", "%s: %s" % (type(e).__name__, e)
                finally:
                    failed = motion.torque_off(self.adapter, SERVO_IDS)
                    self._set(xid, torque_off_failed=failed)
                    if failed and status == "succeeded":
                        status, code, detail = "failed", "TORQUE_OFF_FAILED", "這些 ID 關扭力失敗：%s" % failed
        except AdapterError as e:
            status, code, detail = "failed", e.code, str(e)
        except Exception as e:   # 不讓例外吞掉狀態；fault 會鎖住後續動作
            status, code, detail = "failed", "INTERNAL_ERROR", "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                with self._lock:
                    self.executions[xid].update(status=status, reason_code=code, detail=detail,
                                                finished_at=_iso(self.clock()))
                    if status == "failed":
                        self.fault = {"reason_code": code, "detail": detail, "execution_id": xid,
                                      "at": _iso(self.clock())}
                    if self.running and self.running[0] == xid:
                        self.running = None
                    phases = list(self.executions[xid]["phases"])
                    torque_failed = list(self.executions[xid]["torque_off_failed"])
                self._audit_safe("execution.finished", "handd", p["proposal_id"], execution_id=xid, status=status,
                                 reason_code=code, detail=detail, phases=phases, torque_off_failed=torque_failed)
            finally:
                self._bus.release()

    def stop(self, caller=AI, timeout_s=5.0):
        """取消進行中的執行並關扭力。不需要核准，永遠接受。"""
        with self._lock:
            self._stop_seq += 1
            running = self.running
        if running:
            xid, cancel, thread = running
            cancel.set()
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                with self._lock:
                    st = self.executions[xid]["status"]
                if st in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.01)
            eid = self._audit_safe("stop", caller, None, cancelled_execution=xid)
            return self._envelope("accepted", cancelled_execution=xid, execution_status=st, event_id=eid)
        failed, detail = None, ""
        if self._bus.acquire(timeout=timeout_s):
            try:
                with self.adapter.connected():
                    failed = motion.torque_off(self.adapter, SERVO_IDS)
            except AdapterError as e:
                detail = str(e)
            finally:
                self._bus.release()
        eid = self._audit_safe("stop", caller, None, cancelled_execution=None, torque_off_failed=failed, detail=detail)
        return self._envelope("accepted", cancelled_execution=None,
                              torque_off=("ok" if failed == [] else "unknown"), detail=detail, event_id=eid)

    def clear_fault(self, caller=OPERATOR):
        if caller != OPERATOR:
            return self._reject("NOT_PERMITTED", "只有操作者能清除 fault", caller, "fault.clear_rejected")
        with self._lock:
            old, self.fault = self.fault, None
        eid = self.audit.write("fault.cleared", caller, None, previous=old)
        return self._envelope("ok", cleared=old, event_id=eid)

    def log_tail(self, n=20):
        return self._envelope("ok", events=self.audit.tail(max(1, min(int(n), 200))))
