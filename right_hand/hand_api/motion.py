"""同時移動、落後與電壓檢查、到位確認。

演算法移植自 right_hand/tools/servo_tool.py 已在實機跑過的版本（2026-10-07，見 gesture_calibration.md）：
把路徑切成很多輪，走最遠那顆每輪前進 step_deg，其餘按比例；每一輪讀回位置與電壓，
任何一顆落後超過 lag_deg 或電壓低於 VOLT_MIN 就停。最後等每顆在 TOLERANCE_DEG 內且停下來。
這裡只用 adapter 的基本操作，所以真、假 adapter 走同一段程式。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .adapter import AdapterError

VOLT_MIN = 4.0
TOLERANCE_DEG = 8.0
SETTLE_S = 3.0
STILL_DEG = 0.5
ROUND_PERIOD_S = 0.03
OPEN_DEG = -30.0          # 奇數 ID 的張開角度；偶數 ID 取負號


@dataclass(frozen=True)
class Speed:
    name: str
    step_deg: float        # 走最遠那顆每一輪的步幅
    servo_rad_s: float     # 伺服機自己的速度上限
    lag_deg: float         # 途中容許的落後


# 和 servo_tool.GESTURE_SPEEDS 相同（有測試確認兩邊一致）。
SPEEDS = {
    "slow": Speed("slow", 3.0, 1.5, 12.0),
    "normal": Speed("normal", 6.0, 3.0, 15.0),
    "fast": Speed("fast", 10.0, 4.5, 19.0),
}


@dataclass
class MoveOutcome:
    ok: bool
    reason_code: str | None = None      # LAG_EXCEEDED、VOLTAGE_SAG、SERVO_LOST、NOT_SETTLED、CANCELLED
    detail: str = ""
    rounds: int = 0
    elapsed_s: float = 0.0
    final: dict = field(default_factory=dict)   # {sid: {"target": 度, "actual": 度}}


def engage(adapter, ids, servo_rad_s):
    """開扭力前先設速度、把目標設成目前位置，開扭力的瞬間才不會朝舊目標衝。"""
    for sid in ids:
        adapter.set_speed(sid, servo_rad_s)
        adapter.set_goal(sid, adapter.read_position(sid))
        adapter.set_torque(sid, True)


def torque_off(adapter, ids):
    """盡力關掉每一顆的扭力；不丟例外。回傳沒有關成功的 ID。"""
    failed = []
    for sid in ids:
        try:
            adapter.set_torque(sid, False)
        except Exception:
            failed.append(sid)
    return failed


def open_targets(middle_offsets, ids):
    return {sid: middle_offsets[sid] + (OPEN_DEG if sid % 2 else -OPEN_DEG) for sid in ids}


def move_together(adapter, targets, speed, cancel=None, sleep=time.sleep, monotonic=time.monotonic):
    """所有顆一起走到各自的目標（度）。cancel 是 threading.Event 或 None。"""
    began = monotonic()
    out = MoveOutcome(ok=False)
    try:
        start = {sid: adapter.read_position(sid) for sid in targets}
    except AdapterError as e:
        out.reason_code, out.detail = "SERVO_LOST", str(e)
        return out
    far = max([abs(targets[s] - start[s]) for s in targets] + [0.0])
    rounds = max(1, math.ceil(far / speed.step_deg))
    out.rounds = rounds
    for k in range(1, rounds + 1):
        if cancel is not None and cancel.is_set():
            out.reason_code, out.detail = "CANCELLED", "第 %d／%d 輪被取消" % (k, rounds)
            return out
        sub = {s: start[s] + (targets[s] - start[s]) * k / rounds for s in targets}
        try:
            for s in targets:
                adapter.set_goal(s, sub[s])
            sleep(ROUND_PERIOD_S)
            for s in targets:
                now = adapter.read_position(s)
                if abs(now - sub[s]) > speed.lag_deg:
                    out.reason_code = "LAG_EXCEEDED"
                    out.detail = "ID %d 第 %d／%d 輪目標 %+.1f° 實際 %+.1f°" % (s, k, rounds, sub[s], now)
                    return out
                volt = adapter.read_voltage(s)
                if volt < VOLT_MIN:
                    out.reason_code = "VOLTAGE_SAG"
                    out.detail = "ID %d 電壓 %.1f V（第 %d／%d 輪）" % (s, volt, k, rounds)
                    return out
        except AdapterError as e:
            out.reason_code, out.detail = "SERVO_LOST", str(e)
            return out
    out.elapsed_s = monotonic() - began
    # 到位確認：每顆在容許範圍內，而且連續兩次讀值幾乎不變。
    last = None
    now = {}
    for _ in range(int(SETTLE_S / 0.1)):
        if cancel is not None and cancel.is_set():
            out.reason_code, out.detail = "CANCELLED", "到位確認時被取消"
            return out
        sleep(0.1)
        try:
            now = {s: adapter.read_position(s) for s in targets}
        except AdapterError as e:
            out.reason_code, out.detail = "SERVO_LOST", str(e)
            return out
        near = all(abs(now[s] - targets[s]) < TOLERANCE_DEG for s in targets)
        still = last is not None and all(abs(now[s] - last[s]) < STILL_DEG for s in targets)
        if near and still:
            break
        last = now
    out.final = {s: {"target": round(targets[s], 1), "actual": round(now[s], 1)} for s in targets}
    off = [s for s in targets if abs(now[s] - targets[s]) >= TOLERANCE_DEG]
    if off:
        out.reason_code = "NOT_SETTLED"
        out.detail = "沒到位：" + "、".join("ID %d（%+.1f° / 目標 %+.1f°）" % (s, now[s], targets[s]) for s in off)
        return out
    out.ok = True
    return out
