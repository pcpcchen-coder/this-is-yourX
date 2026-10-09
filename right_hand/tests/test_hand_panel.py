"""hand_panel（網頁操作面板）的測試。全部跑在假 adapter 或假匯流排上，不需要硬體。

重點和 hand_api 的測試相同：被拒絕的請求不能寫出任何目標位置或開任何扭力；
任何檢查沒過、頁面不見、閒置太久，都要關扭力。
"""
import http.client
import json
import math
import os
import shutil
import socket
import stat
import sys
import threading
import time

import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RIGHT_HAND = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, RIGHT_HAND)

from hand_api import adapter as adapter_mod  # noqa: E402
from hand_api import config, gateway, motion  # noqa: E402
from hand_api.adapter import AdapterError, FakeHandAdapter  # noqa: E402
from hand_panel import core, poses, safe_off, server  # noqa: E402
from hand_panel.core import Panel, Rejected  # noqa: E402

SERVO_IDS = config.SERVO_IDS
A, B = "clientAAAA", "clientBBBB"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


def make(tmp, clock, fake=None, cfg=None, sleep=lambda s: None, **kw):
    cfg = cfg or config.load()
    fake = fake or FakeHandAdapter(clock=clock, positions=motion.open_targets(cfg.middle_offsets, SERVO_IDS))
    fake.clock = clock
    audit = gateway.AuditLog(str(tmp / "events.jsonl"), clock=clock)
    panel = Panel(cfg, fake, audit, poses.PoseStore(str(tmp / "poses.yaml")), clock=clock, monotonic=clock,
                  sleep=sleep, marker_path=str(tmp / "torque.json"), **kw)
    return panel, fake


def on(panel, client=A, speed="slow"):
    """開一個頁面並啟用扭力。"""
    panel.beat(client)
    panel.tick()
    return panel.enable(client, speed)


def writes(fake):
    """（寫過幾次目標, 寫過幾次扭力）"""
    return (sum(len(v) for v in fake.goal_history.values()), sum(len(v) for v in fake.torque_history.values()))


def events(tmp):
    path = tmp / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def rejected(fn, *args, **kw):
    with pytest.raises(Rejected) as e:
        fn(*args, **kw)
    return e.value.reason_code


def config_with(tmp, revision=None, offsets=None):
    """複製一份設定，改校正版本或中位修正。"""
    d = tmp / "config"
    shutil.copytree(config.CONFIG_DIR, d)
    cal = yaml.safe_load((d / "calibration.yaml").read_text(encoding="utf-8"))
    if revision:
        cal["revision"] = revision
    if offsets:
        cal["middle_offset_deg"].update(offsets)
    (d / "calibration.yaml").write_text(yaml.safe_dump(cal), encoding="utf-8")
    gestures = yaml.safe_load((d / "gestures.yaml").read_text(encoding="utf-8"))
    if offsets and not revision:
        for g in gestures["gestures"].values():
            g["calibration_revision"] = cal["revision"]
    (d / "gestures.yaml").write_text(yaml.safe_dump(gestures, allow_unicode=True), encoding="utf-8")
    return config.load(str(d))


# --- 換算與範圍 ---------------------------------------------------------------

def test_flex_and_side_use_the_same_convention_as_hand_api():
    assert core.servo_to_finger(74, -74) == (74.0, 0.0)
    assert core.servo_to_finger(90, -13) == (51.5, 38.5)          # gestures.yaml 的拇指
    for flex, side in ((0, 0), (51.5, 38.5), (-30, 0), (20, -15)):
        assert core.servo_to_finger(*core.finger_to_servo(flex, side)) == (flex, side)


@pytest.mark.parametrize("flex, side, want", [
    (50, 40, (50, 40, False)), (200, 0, (90, 0, True)), (-100, 0, (-30, 0, True)),
    (90, 20, (90, 0, True)), (60, 40, (60, 30, True)), (60, -40, (60, -30, True)), (0, 99, (0, 40, True)),
])
def test_clamp_keeps_both_servos_inside_the_pose_limit(flex, side, want):
    assert core.clamp_finger(flex, side) == want
    a, b = core.finger_to_servo(*core.clamp_finger(flex, side)[:2])
    assert abs(a) <= config.POSE_LIMIT_DEG and abs(b) <= config.POSE_LIMIT_DEG


def test_slider_range_matches_what_the_bring_up_tools_use():
    assert core.FLEX_RANGE_DEG[0] == motion.OPEN_DEG
    assert core.FLEX_RANGE_DEG[1] <= config.POSE_LIMIT_DEG
    assert core.finger_to_servo(motion.OPEN_DEG, 0.0) == (motion.OPEN_DEG, -motion.OPEN_DEG)


@pytest.mark.parametrize("kw", [
    {}, {"missing": {3}}, {"missing": set(SERVO_IDS)}, {"voltage_v": 3.5}, {"voltage_v": 8.0},
    {"temperature_c": 70.0}, {"stale_s": 2.0}, {"positions": {5: float("nan")}}, {"temperature_c": None},
])
def test_preconditions_are_the_same_as_the_gateways(tmp, clock, kw):
    fake = FakeHandAdapter(clock=clock, **kw)
    gw = gateway.Gateway(config.load(), fake, gateway.AuditLog(str(tmp / "g.jsonl"), clock=clock), clock=clock)
    with fake.connected():
        reading = adapter_mod.HandReading(servos={s: fake.read_servo(s) for s in SERVO_IDS},
                                          observed_at=clock(), adapter="fake")
    assert core.check_reading(reading, clock()) == gw._check_reading(reading)


# --- 沒有頁面、只有頁面 ---------------------------------------------------------

class Counting(FakeHandAdapter):
    """記下匯流排現在有沒有被佔著。"""
    open_now = 0

    def connected(self):
        import contextlib
        outer = super().connected()

        @contextlib.contextmanager
        def cm():
            with outer as a:
                self.open_now += 1
                try:
                    yield a
                finally:
                    self.open_now -= 1
        return cm()


def test_without_a_page_the_bus_is_not_touched(tmp, clock):
    panel, fake = make(tmp, clock)
    assert panel.tick() is True
    assert fake.connect_count == 0 and writes(fake) == (0, 0)
    assert panel.status()["bus"]["state"] == "detached"


def test_an_open_page_only_reads(tmp, clock):
    panel, fake = make(tmp, clock)
    for _ in range(5):
        panel.beat(A)
        panel.tick()
        clock.advance(0.25)
    s = panel.status(A)
    assert fake.connect_count == 1 and writes(fake) == (0, 0)
    assert s["bus"]["state"] == "attached" and s["torque_on"] is False and s["you_control"] is False
    assert [x["ok"] for x in s["servos"]] == [True] * 8
    assert s["fingers"]["index"]["actual"] == {"flex": -30.0, "side": 0.0}
    assert s["fingers"]["index"]["target"] is None
    assert not (tmp / "torque.json").exists()


def test_the_bus_is_released_when_the_page_goes_away(tmp, clock):
    fake = Counting(clock=clock)
    panel, _ = make(tmp, clock, fake=fake)
    panel.beat(A)
    panel.tick()
    assert fake.open_now == 1
    clock.advance(core.DETACH_AFTER_S + 0.1)
    panel.tick()
    assert fake.open_now == 0 and panel.status()["bus"]["state"] == "detached"
    assert writes(fake) == (0, 0)


def test_a_busy_bus_is_reported_and_nothing_is_written(tmp, clock):
    panel, fake = make(tmp, clock)
    fake.busy = True
    panel.beat(A)
    panel.tick()
    s = panel.status(A)
    assert s["bus"]["state"] == "unavailable" and s["bus"]["reason_code"] == "BUS_BUSY"
    clock.advance(1.1)
    assert rejected(panel.enable, A) == "BUS_BUSY"
    assert writes(fake) == (0, 0)
    fake.busy = False
    clock.advance(1.1)
    assert on(panel)["status"] == "accepted"


# --- 啟用扭力 -----------------------------------------------------------------

def test_enable_sets_each_goal_to_the_current_position_before_torque(tmp, clock):
    start = {1: 12.0, 2: -40.0, 3: 0.0, 4: 5.5, 5: -30.0, 6: 30.0, 7: 88.0, 8: -10.0}
    order = []

    class Ordered(FakeHandAdapter):
        def set_goal(self, sid, deg):
            order.append(("goal", sid, deg))
            super().set_goal(sid, deg)

        def set_torque(self, sid, on_):
            order.append(("torque", sid, on_))
            super().set_torque(sid, on_)

    fake = Ordered(clock=clock, positions=start)
    panel, _ = make(tmp, clock, fake=fake)
    r = on(panel)
    assert r["status"] == "accepted" and all(fake.torque.values())
    assert {s: fake.goal_history[s] for s in SERVO_IDS} == {s: [start[s]] for s in SERVO_IDS}
    for sid in SERVO_IDS:                                  # 每一顆都是先設目標、才開扭力
        assert order.index(("goal", sid, start[sid])) < order.index(("torque", sid, True))
    assert fake.speed == {s: motion.SPEEDS["slow"].servo_rad_s for s in SERVO_IDS}
    assert fake.positions == start                           # 開扭力的瞬間沒有動
    s = panel.status(A)
    assert s["torque_on"] and s["you_control"] and not s["moving"]
    assert s["fingers"]["index"]["target"] == s["fingers"]["index"]["actual"] == {"flex": 26.0, "side": -14.0}
    assert (tmp / "torque.json").exists()


@pytest.mark.parametrize("kw, code", [
    ({"missing": {3}}, "SERVO_MISSING"), ({"missing": set(SERVO_IDS)}, "RAIL_DOWN"),
    ({"voltage_v": 3.5}, "VOLTAGE_OUT_OF_RANGE"), ({"voltage_v": 8.0}, "VOLTAGE_OUT_OF_RANGE"),
    ({"temperature_c": 70.0}, "OVER_TEMPERATURE"), ({"stale_s": 2.0}, "STALE_STATE"),
    ({"positions": {5: float("nan")}}, "STATE_UNKNOWN"),
])
def test_enable_is_refused_when_any_precondition_fails(tmp, clock, kw, code):
    panel, fake = make(tmp, clock, fake=FakeHandAdapter(clock=clock, **kw))
    panel.beat(A)
    panel.tick()
    assert rejected(panel.enable, A) == code
    assert writes(fake) == (0, 0) and not (tmp / "torque.json").exists()
    assert panel.status(A)["torque_on"] is False
    assert [e["type"] for e in events(tmp)] == ["torque.rejected"]


def test_enable_refuses_an_unknown_speed(tmp, clock):
    panel, fake = make(tmp, clock)
    panel.beat(A)
    assert rejected(panel.enable, A, "ludicrous") == "UNKNOWN_SPEED"
    assert writes(fake) == (0, 0)


def test_a_second_page_cannot_take_over(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    before = writes(fake)
    panel.beat(B)
    assert rejected(panel.enable, B) == "CONTROL_HELD"
    assert rejected(panel.set_fingers, B, {"index": {"flex": 50}}) == "CONTROL_HELD"
    assert rejected(panel.open_hand, B) == "CONTROL_HELD"
    assert rejected(panel.play_pose, B, "ok") == "CONTROL_HELD"
    assert rejected(panel.set_speed, B, "fast") == "CONTROL_HELD"
    panel.tick()
    assert writes(fake) == before
    assert panel.status(B)["you_control"] is False and panel.status(B)["torque_on"] is True
    assert panel.enable(A)["already"] is True


def test_two_pages_enabling_at_the_same_moment_only_one_gets_control(tmp, clock):
    panel, fake = make(tmp, clock)
    for c in (A, B):
        panel.beat(c)
    panel.tick()
    reqs = [core._Request(client=A, speed="slow"), core._Request(client=B, speed="fast")]
    panel._requests.extend(reqs)                             # 兩個請求都在 worker 處理之前排進來
    panel.tick()
    panel.tick()
    assert reqs[0].result["status"] == "accepted" and reqs[1].result.reason_code == "CONTROL_HELD"
    assert all(r.done.is_set() for r in reqs)
    assert panel.status(A)["you_control"] and not panel.status(B)["you_control"]
    assert panel.status(A)["speed"] == "slow" and writes(fake) == (8, 8)


def test_a_failure_halfway_through_enable_turns_everything_off(tmp, clock):
    class Flaky(FakeHandAdapter):
        def set_torque(self, sid, on_):
            if on_ and sid == 5:
                raise AdapterError("SERVO_NO_RESPONSE", "ID 5 沒有回應")
            super().set_torque(sid, on_)

    fake = Flaky(clock=clock)
    panel, _ = make(tmp, clock, fake=fake)
    panel.beat(A)
    panel.tick()
    assert rejected(panel.enable, A) == "SERVO_LOST"
    assert not any(fake.torque.values())
    s = panel.status(A)
    assert s["torque_on"] is False and s["fault"]["reason_code"] == "SERVO_LOST"
    assert not (tmp / "torque.json").exists()


def test_the_marker_exists_before_any_torque_is_switched_on(tmp, clock):
    seen = []

    class Checking(FakeHandAdapter):
        def set_torque(self, sid, on_):
            if on_:
                seen.append((tmp / "torque.json").exists())
            super().set_torque(sid, on_)

    panel, fake = make(tmp, clock, fake=Checking(clock=clock))
    on(panel)
    assert seen == [True] * 8
    assert json.loads((tmp / "torque.json").read_text())["adapter"] == "fake"
    panel.stop(A)
    assert not (tmp / "torque.json").exists()


# --- 移動 ---------------------------------------------------------------------

def test_nothing_moves_while_torque_is_off(tmp, clock):
    panel, fake = make(tmp, clock)
    panel.beat(A)
    panel.tick()
    assert rejected(panel.set_fingers, A, {"index": {"flex": 50}}) == "TORQUE_OFF"
    assert rejected(panel.open_hand, A) == "TORQUE_OFF"
    assert rejected(panel.play_pose, A, "ok") == "TORQUE_OFF"
    assert rejected(panel.set_speed, A, "fast") == "TORQUE_OFF"
    panel.tick()
    assert writes(fake) == (0, 0)


def test_one_finger_moves_in_small_steps_and_the_others_stay(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    r = panel.set_fingers(A, {"index": {"flex": 50, "side": 10}})
    assert r["status"] == "accepted" and r["clamped"] == [] and panel.status(A)["moving"] is True
    assert panel.tick() is False
    assert fake.positions[1] == 60.0 and fake.positions[2] == -40.0
    assert {s: fake.positions[s] for s in (3, 4, 5, 6, 7, 8)} == {3: -30, 4: 30, 5: -30, 6: 30, 7: -30, 8: 30}
    path = fake.goal_history[1]
    steps = [abs(b - a) for a, b in zip(path, path[1:])]
    assert max(steps) <= motion.SPEEDS["slow"].step_deg + 1e-9 and len(path) == 31   # 開扭力 1 次 + 30 輪
    assert all(len(set(fake.goal_history[s])) == 1 for s in (3, 4, 5, 6, 7, 8))       # 其他顆的目標沒變
    s = panel.status(A)
    assert s["moving"] is False and s["last_move"]["ok"] and s["last_move"]["rounds"] == 30
    assert s["fingers"]["index"]["target"] == {"flex": 50.0, "side": 10.0}
    assert s["hold_remaining_s"] == core.HOLD_TIMEOUT_S


def test_only_the_requested_axis_changes(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.set_fingers(A, {"thumb": {"flex": 40, "side": 30}})
    panel.tick()
    panel.set_fingers(A, {"thumb": {"side": -10}})
    panel.tick()
    assert panel.status(A)["fingers"]["thumb"]["target"] == {"flex": 40.0, "side": -10.0}
    assert (fake.positions[7], fake.positions[8]) == (30.0, -50.0)


def test_middle_offsets_are_added_to_what_is_sent_and_removed_from_what_is_shown(tmp, clock):
    cfg = config_with(tmp, offsets={1: 3, 2: -2})
    fake = FakeHandAdapter(clock=clock, positions=motion.open_targets(cfg.middle_offsets, SERVO_IDS))
    panel, _ = make(tmp, clock, fake=fake, cfg=cfg)
    on(panel)
    assert panel.status(A)["fingers"]["index"]["actual"] == {"flex": -30.0, "side": 0.0}
    panel.set_fingers(A, {"index": {"flex": 50, "side": 0}})
    panel.tick()
    assert (fake.positions[1], fake.positions[2]) == (53.0, -52.0)
    s = panel.status(A)
    assert s["fingers"]["index"]["actual"] == {"flex": 50.0, "side": 0.0}
    assert s["servos"][0]["position_deg"] == 50.0


def test_out_of_range_values_are_pulled_in_and_reported(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    r = panel.set_fingers(A, {"index": {"flex": 200}, "middle": {"flex": 10}})
    assert r["clamped"] == ["index"] and r["fingers"]["index"] == {"flex": 90.0, "side": 0.0}
    panel.tick()
    assert (fake.positions[1], fake.positions[2]) == (90.0, -90.0)
    assert max(abs(g) for h in fake.goal_history.values() for g in h) <= config.POSE_LIMIT_DEG


@pytest.mark.parametrize("fingers", [
    None, {}, [], "index", {"pinky": {"flex": 1}}, {"index": {}}, {"index": 50}, {"index": {"flex": "50"}},
    {"index": {"flex": True}}, {"index": {"flex": float("nan")}}, {"index": {"side": float("inf")}},
    {"index": {"angle": 5}}, {"index": {"flex": 5, "speed": 9}}, {"index": {"flex": None}},
])
def test_malformed_finger_requests_are_refused_without_moving(tmp, clock, fingers):
    panel, fake = make(tmp, clock)
    on(panel)
    before = writes(fake)
    assert rejected(panel.set_fingers, A, fingers) in ("PARAMETER_NOT_ALLOWED", "PARAMETER_OUT_OF_RANGE",
                                                       "UNKNOWN_COMPONENT")
    panel.tick()
    assert writes(fake) == before and panel.status(A)["moving"] is False


def test_a_target_past_the_servo_limit_with_offsets_is_refused(tmp, clock):
    cfg = config_with(tmp, offsets={1: 8})
    fake = FakeHandAdapter(clock=clock, positions=motion.open_targets(cfg.middle_offsets, SERVO_IDS))
    panel, _ = make(tmp, clock, fake=fake, cfg=cfg)
    on(panel)
    before = writes(fake)
    assert rejected(panel.set_fingers, A, {"index": {"flex": 90}}) == "SERVO_LIMIT"     # 90 + 8 > 95
    panel.tick()
    assert writes(fake) == before


def test_a_newer_target_replaces_the_one_in_progress(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    fake.after_goals.append((fake.goal_count + 8 * 5, lambda a: panel.set_fingers(A, {"index": {"flex": 0}})))
    panel.set_fingers(A, {"index": {"flex": 90}})
    panel.tick()                       # 走到一半被新目標取代
    assert fake.positions[1] < 90 and panel.status(A)["moving"] is True
    assert panel.status(A)["fault"] is None
    panel.tick()
    assert (fake.positions[1], fake.positions[2]) == (0.0, 0.0)
    assert panel.status(A)["moving"] is False and panel.status(A)["torque_on"] is True


def test_changing_speed_applies_to_the_next_move(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    assert rejected(panel.set_speed, A, "warp") == "UNKNOWN_SPEED"
    panel.set_speed(A, "fast")
    panel.set_fingers(A, {"index": {"flex": 70}})
    panel.tick()
    assert fake.speed == {s: motion.SPEEDS["fast"].servo_rad_s for s in SERVO_IDS}
    assert panel.status(A)["last_move"]["rounds"] == 10 and panel.status(A)["speed"] == "fast"


def test_open_hand_goes_to_the_same_pose_as_the_skills(tmp, clock):
    cfg = config.load()
    panel, fake = make(tmp, clock, fake=FakeHandAdapter(clock=clock))      # 從全部 0° 開始
    on(panel)
    panel.open_hand(A)
    panel.tick()
    assert fake.positions == motion.open_targets(cfg.middle_offsets, SERVO_IDS)


# --- 檢查沒過：關扭力並鎖住 -----------------------------------------------------

def test_a_stuck_finger_stops_everything_and_latches_a_fault(tmp, clock):
    panel, fake = make(tmp, clock, fake=FakeHandAdapter(
        clock=clock, travel={1: (-30.0, 0.0)}, positions=motion.open_targets(config.load().middle_offsets, SERVO_IDS)))
    on(panel)
    panel.set_fingers(A, {"index": {"flex": 90}})
    panel.tick()
    s = panel.status(A)
    assert s["fault"]["reason_code"] == "LAG_EXCEEDED" and "ID 1" in s["fault"]["detail"]
    assert s["torque_on"] is False and not any(fake.torque.values())
    assert max(fake.goal_history[1]) <= 0.0 + motion.SPEEDS["slow"].lag_deg + motion.SPEEDS["slow"].step_deg
    before = writes(fake)
    assert rejected(panel.enable, A) == "ACTIVE_FAULT"
    panel.tick()
    assert writes(fake) == before
    panel.clear_fault(A)
    assert on(panel)["status"] == "accepted"
    types = [e["type"] for e in events(tmp)]
    assert types.count("fault.cleared") == 1 and "torque.off" in types


def test_a_voltage_drop_during_a_move_stops_it(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    fake.after_goals.append((fake.goal_count + 8 * 3, lambda a: setattr(a, "voltage_v", 3.6)))
    panel.set_fingers(A, {"middle": {"flex": 90}})
    panel.tick()
    s = panel.status(A)
    assert s["fault"]["reason_code"] == "VOLTAGE_SAG" and not any(fake.torque.values())
    assert fake.positions[3] < 90


def test_losing_a_servo_during_a_move_stops_it_and_says_which_did_not_switch_off(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    fake.after_goals.append((fake.goal_count + 8 * 3, lambda a: a.missing.add(4)))
    panel.set_fingers(A, {"middle": {"flex": 90}})
    panel.tick()
    s = panel.status(A)
    assert s["fault"]["reason_code"] == "SERVO_LOST" and s["torque_on"] is False
    assert s["last_release"]["torque_off_failed"] == [4]
    assert not any(fake.torque[x] for x in SERVO_IDS if x != 4)
    assert (tmp / "torque.json").exists()                    # 不確定 4 號有沒有關：標記留著


def test_torque_that_dropped_by_itself_is_noticed(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    fake.torque[3] = False                                    # 例如電源閃斷過
    panel.beat(A)
    panel.tick()
    s = panel.status(A)
    assert s["fault"]["reason_code"] == "TORQUE_LOST" and not any(fake.torque.values())


@pytest.mark.parametrize("change, code", [
    (lambda f: setattr(f, "temperature_c", 70.0), "OVER_TEMPERATURE"),
    (lambda f: setattr(f, "voltage_v", 3.0), "VOLTAGE_OUT_OF_RANGE"),
    (lambda f: f.missing.update(SERVO_IDS), "RAIL_DOWN"),
    (lambda f: f.missing.add(6), "SERVO_MISSING"),
])
def test_a_bad_reading_while_holding_releases_and_latches(tmp, clock, change, code):
    panel, fake = make(tmp, clock)
    on(panel)
    change(fake)
    panel.beat(A)
    panel.tick()
    s = panel.status(A)
    assert s["fault"]["reason_code"] == code and s["torque_on"] is False
    assert rejected(panel.enable, A) == "ACTIVE_FAULT"


def test_a_stale_check_runs_before_a_move_starts(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    clock.advance(1.0)
    panel.beat(A)
    fake.temperature_c = 80.0
    before = writes(fake)[0]
    panel.set_fingers(A, {"ring": {"flex": 60}})
    panel.tick()
    assert writes(fake)[0] == before                          # 沒有寫出任何新目標
    assert panel.status(A)["fault"]["reason_code"] == "OVER_TEMPERATURE"


def test_an_unexpected_error_in_the_worker_releases_torque(tmp, clock):
    class Boom(FakeHandAdapter):
        explode = False

        def read_servo(self, sid):
            if self.explode:
                raise RuntimeError("boom")
            return super().read_servo(sid)

    fake = Boom(clock=clock)
    panel, _ = make(tmp, clock, fake=fake)
    on(panel)
    fake.explode = True
    panel.beat(A)
    assert panel._safe_tick() is True
    s = panel.status(A)
    assert s["fault"]["reason_code"] == "INTERNAL_ERROR" and not any(fake.torque.values())


# --- 停止、看門狗、閒置 ---------------------------------------------------------

def test_stop_is_always_accepted_and_always_sends_torque_off(tmp, clock):
    panel, fake = make(tmp, clock)
    r = panel.stop(B)                                          # 沒有頁面、沒有扭力
    assert r["status"] == "accepted" and r["torque_off"] == "ok" and r["was_on"] is False
    assert all(h == [False] for h in fake.torque_history.values()) and writes(fake)[0] == 0
    on(panel)
    r = panel.stop(B)                                          # 不是控制者也能停
    assert r["torque_off"] == "ok" and r["was_on"] is True and not any(fake.torque.values())
    s = panel.status(A)
    assert s["torque_on"] is False and s["fault"] is None and s["last_release"]["reason"] == "stop"
    assert rejected(panel.set_fingers, A, {"index": {"flex": 5}}) == "TORQUE_OFF"


def test_stop_says_so_when_it_cannot_reach_the_bus(tmp, clock):
    panel, fake = make(tmp, clock)
    fake.busy = True
    r = panel.stop(A)
    assert r["status"] == "accepted" and r["torque_off"] == "unknown" and "切斷伺服機電源" in r["detail"]
    assert writes(fake) == (0, 0)


def test_stop_cancels_a_move_in_progress():
    """真的 worker 執行緒、真的時間：移動到一半按停止。"""
    import tempfile
    d = tempfile.mkdtemp()
    cfg = config.load()
    fake = FakeHandAdapter(positions=motion.open_targets(cfg.middle_offsets, SERVO_IDS))
    panel = Panel(cfg, fake, gateway.AuditLog(os.path.join(d, "e.jsonl")), poses.PoseStore(os.path.join(d, "p.yaml")))
    panel.start()
    try:
        panel.beat(A)
        assert panel.enable(A)["status"] == "accepted"
        panel.set_fingers(A, {"index": {"flex": 90}})          # 慢速 40 輪，約 1.2 秒
        time.sleep(0.25)
        t0 = time.monotonic()
        r = panel.stop(B)
        assert time.monotonic() - t0 < 1.0
        assert r["torque_off"] == "ok" and not any(fake.torque.values())
        assert -30 < fake.positions[1] < 90
        s = panel.status(A)
        assert s["torque_on"] is False and s["fault"] is None and s["moving"] is False
    finally:
        panel.shutdown()
        shutil.rmtree(d)


def test_torque_goes_off_when_the_controlling_page_stops_reporting(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    clock.advance(core.HEARTBEAT_TIMEOUT_S - 0.1)
    panel.tick()
    assert all(fake.torque.values())
    clock.advance(0.2)
    panel.beat(B)                                              # 別的頁面還開著也不算
    panel.tick()
    assert not any(fake.torque.values())
    s = panel.status(B)
    assert s["torque_on"] is False and s["fault"] is None and s["last_release"]["reason"] == "watchdog"
    assert events(tmp)[-1]["data"]["reason"] == "watchdog"


def test_the_watchdog_also_fires_in_the_middle_of_a_move(tmp, clock):
    panel, fake = make(tmp, clock, sleep=lambda s: clock.advance(1.0))    # 每一輪過 1 秒，頁面都沒回報
    on(panel)
    panel.set_fingers(A, {"index": {"flex": 90}})
    panel.tick()
    panel.tick()
    assert not any(fake.torque.values()) and fake.positions[1] < 0
    assert panel.status()["last_release"]["reason"] == "watchdog" and panel.status()["fault"] is None


def test_idle_torque_times_out_and_activity_resets_the_timer(tmp, clock):
    panel, fake = make(tmp, clock, hold_timeout_s=10.0)

    def run(seconds):
        for _ in range(seconds):
            clock.advance(1.0)
            panel.beat(A)
            panel.tick()

    on(panel)
    run(8)
    panel.set_fingers(A, {"index": {"flex": 10}})              # 有新動作：重新計時
    panel.tick()
    run(8)
    assert all(fake.torque.values()) and panel.status(A)["hold_remaining_s"] == 2.0
    run(3)
    panel.tick()
    assert not any(fake.torque.values())
    s = panel.status(A)
    assert s["last_release"]["reason"] == "hold_timeout" and s["fault"] is None


def test_shutdown_turns_torque_off(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.shutdown()
    assert not any(fake.torque.values()) and not (tmp / "torque.json").exists()
    assert events(tmp)[-1]["data"]["reason"] == "shutdown"
    assert rejected(panel.enable, A) == "SHUTTING_DOWN"


def test_shutdown_without_torque_writes_nothing(tmp, clock):
    panel, fake = make(tmp, clock)
    panel.beat(A)
    panel.tick()
    panel.shutdown()
    assert writes(fake) == (0, 0)


# --- 姿勢 ---------------------------------------------------------------------

def test_saving_with_torque_on_stores_the_targets(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.set_fingers(A, {"index": {"flex": 50, "side": 10}, "thumb": {"flex": 40, "side": 30}})
    panel.tick()
    r = panel.save_pose(A, "pinch_small", "捏小東西", "測試用")
    assert r["status"] == "ok" and r["source"] == "target"
    text = (tmp / "poses.yaml").read_text(encoding="utf-8")
    assert text.startswith("# ") and "gestures.yaml" in text.split("schema_version")[0]
    doc = yaml.safe_load(text)
    rec = doc["poses"]["pinch_small"]
    assert doc["schema_version"] == 1
    assert rec["pose"] == {1: [60.0, -40.0], 3: [-30.0, 30.0], 5: [-30.0, 30.0], 7: [70.0, -10.0]}
    assert rec["measured"] == rec["pose"]
    assert rec["label"] == "捏小東西" and rec["note"] == "測試用" and rec["source"] == "target"
    assert rec["calibration_revision"] == config.load().calibration_revision
    assert sorted(os.listdir(tmp)) == ["events.jsonl", "poses.yaml", "torque.json"]      # 沒有留下暫存檔


def test_saving_with_torque_off_stores_what_was_read(tmp, clock):
    start = {1: 12.3, 2: -40.0, 3: 0.0, 4: 5.5, 5: -30.0, 6: 30.0, 7: 88.0, 8: -10.0}
    panel, fake = make(tmp, clock, fake=FakeHandAdapter(clock=clock, positions=start))
    panel.beat(A)
    panel.tick()
    r = panel.save_pose(A, "by_hand")
    assert r["source"] == "measured" and writes(fake) == (0, 0)
    rec = yaml.safe_load((tmp / "poses.yaml").read_text(encoding="utf-8"))["poses"]["by_hand"]
    assert rec["pose"] == {1: [12.3, -40.0], 3: [0.0, 5.5], 5: [-30.0, 30.0], 7: [88.0, -10.0]}


def test_saving_needs_a_fresh_complete_reading_when_torque_is_off(tmp, clock):
    panel, fake = make(tmp, clock)
    assert rejected(panel.save_pose, A, "nothing") == "STATE_UNKNOWN"          # 沒有頁面在讀
    panel.beat(A)
    panel.tick()
    clock.advance(core.MEASURED_FRESH_S + 0.5)
    assert rejected(panel.save_pose, A, "stale") == "STATE_UNKNOWN"
    fake.missing.add(2)
    panel.beat(A)
    panel.tick()
    assert rejected(panel.save_pose, A, "partial") == "STATE_UNKNOWN"
    assert not (tmp / "poses.yaml").exists()


def test_saving_is_refused_while_the_hand_is_moving(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.set_fingers(A, {"index": {"flex": 50}})
    assert rejected(panel.save_pose, A, "too_soon") == "BUSY"
    assert not (tmp / "poses.yaml").exists()


def test_a_reading_beyond_the_pose_limit_is_not_saved(tmp, clock):
    panel, fake = make(tmp, clock, fake=FakeHandAdapter(clock=clock, positions={1: 93.0}))
    panel.beat(A)
    panel.tick()
    assert rejected(panel.save_pose, A, "too_far") == "SERVO_LIMIT"
    assert not (tmp / "poses.yaml").exists()


@pytest.mark.parametrize("name", ["", "Pinch", "a b", "x" * 33, "手勢", "a-b", "../x", None, 5, ["a"]])
def test_bad_pose_names_are_refused(tmp, clock, name):
    panel, fake = make(tmp, clock)
    on(panel)
    assert rejected(panel.save_pose, A, name) == "BAD_NAME"
    assert rejected(panel.play_pose, A, name) == "UNKNOWN_POSE"
    assert rejected(panel.delete_pose, A, name) == "UNKNOWN_POSE"
    assert rejected(panel.reconfirm_pose, A, name) == "UNKNOWN_POSE"
    assert not (tmp / "poses.yaml").exists()


def test_gesture_table_names_are_reserved_and_read_only(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    assert rejected(panel.save_pose, A, "ok") == "NAME_TAKEN"
    assert rejected(panel.delete_pose, A, "ok") == "NOT_PERMITTED"
    assert rejected(panel.reconfirm_pose, A, "ok") == "NOT_PERMITTED"
    assert not (tmp / "poses.yaml").exists()


def test_overwriting_needs_to_be_asked_for(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.save_pose(A, "p1", "第一版")
    panel.set_fingers(A, {"ring": {"flex": 45}})
    panel.tick()
    assert rejected(panel.save_pose, A, "p1", "第二版") == "NAME_TAKEN"
    assert rejected(panel.save_pose, A, "p1", "第二版", "", "yes") == "PARAMETER_NOT_ALLOWED"
    assert panel.store.load()["p1"]["label"] == "第一版"
    panel.save_pose(A, "p1", "第二版", overwrite=True)
    rec = panel.store.load()["p1"]
    assert rec["label"] == "第二版" and rec["pose"][5] == [45.0, -45.0]


def test_a_saved_pose_is_reproduced_exactly(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.set_fingers(A, {"index": {"flex": 62, "side": -7}, "middle": {"flex": 15}, "ring": {"flex": 80},
                          "thumb": {"flex": 44, "side": 33}})
    panel.tick()
    want = dict(fake.positions)
    panel.save_pose(A, "grip_a")
    panel.open_hand(A)
    panel.tick()
    assert fake.positions != want
    panel.stop(A)                                              # 關扭力、換一個新的面板（像重開機之後）
    panel2, _ = make(tmp, clock, fake=fake)
    on(panel2)
    assert panel2.play_pose(A, "grip_a")["status"] == "accepted"
    panel2.tick()
    assert fake.positions == want
    assert panel2.status(A)["fingers"]["thumb"]["target"] == {"flex": 44.0, "side": 33.0}


def test_the_calibrated_gesture_can_be_replayed_from_the_panel(tmp, clock):
    cfg = config.load()
    panel, fake = make(tmp, clock)
    on(panel)
    panel.play_pose(A, "ok")
    panel.tick()
    for a, (da, db) in cfg.gestures["ok"].pose.items():
        assert (fake.positions[a], fake.positions[a + 1]) == (da, db)


def test_replay_of_an_unknown_pose_is_refused(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    before = writes(fake)
    assert rejected(panel.play_pose, A, "nope") == "UNKNOWN_POSE"
    panel.tick()
    assert writes(fake) == before


def test_poses_are_refused_after_recalibration_until_reconfirmed(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.set_fingers(A, {"index": {"flex": 50}})
    panel.tick()
    panel.save_pose(A, "before_recal")
    panel.stop(A)

    cfg2 = config_with(tmp, revision="2099-01-01.r9")
    panel2, _ = make(tmp, clock, fake=fake, cfg=cfg2)
    on(panel2)
    listing = {p["name"]: p for p in panel2.list_poses()["poses"]}
    assert listing["before_recal"]["playable"] is False
    assert listing["before_recal"]["reason_code"] == "CALIBRATION_REVISION_CHANGED"
    assert listing["ok"]["playable"] is False                  # 手勢表裡的也一樣要重新確認
    before = writes(fake)
    assert rejected(panel2.play_pose, A, "before_recal") == "CALIBRATION_REVISION_CHANGED"
    assert rejected(panel2.play_pose, A, "ok") == "CALIBRATION_REVISION_CHANGED"
    panel2.tick()
    assert writes(fake) == before
    assert rejected(panel2.reconfirm_pose, A, "ok") == "NOT_PERMITTED"
    panel2.reconfirm_pose(A, "before_recal")
    assert panel2.play_pose(A, "before_recal")["status"] == "accepted"
    assert panel2.store.load()["before_recal"]["calibration_revision"] == "2099-01-01.r9"


@pytest.mark.parametrize("content", [
    "poses: [1, 2",                                                            # 不是合法的 YAML
    "schema_version: 1\nposes:\n  big:\n    pose: {1: [120, 0], 3: [0, 0], 5: [0, 0], 7: [0, 0]}\n",   # 超出範圍
    "schema_version: 1\nposes:\n  short:\n    pose: {1: [0, 0]}\n",            # 少了手指
    "schema_version: 1\nposes:\n  Bad Name:\n    pose: {1: [0, 0], 3: [0, 0], 5: [0, 0], 7: [0, 0]}\n",
    "schema_version: 1\nposes:\n  nan:\n    pose: {1: [.nan, 0], 3: [0, 0], 5: [0, 0], 7: [0, 0]}\n",
    "schema_version: 7\nposes: {}\n", "- just\n- a list\n",
])
def test_a_pose_file_the_panel_cannot_read_is_left_alone(tmp, clock, content):
    (tmp / "poses.yaml").write_text(content, encoding="utf-8")
    panel, fake = make(tmp, clock)
    on(panel)
    listing = panel.list_poses()
    assert listing["error"] and [p["name"] for p in listing["poses"]] == ["ok"]
    before = writes(fake)
    for name in ("big", "short", "nan", "whatever"):
        assert rejected(panel.play_pose, A, name) == "POSE_FILE_INVALID"
    assert rejected(panel.save_pose, A, "new_one") == "POSE_FILE_INVALID"
    assert rejected(panel.delete_pose, A, "big") == "POSE_FILE_INVALID"
    panel.tick()
    assert writes(fake) == before
    assert (tmp / "poses.yaml").read_text(encoding="utf-8") == content


def test_delete_and_list(tmp, clock):
    panel, fake = make(tmp, clock)
    on(panel)
    panel.save_pose(A, "one", "一")
    panel.save_pose(A, "two")
    listing = panel.list_poses()
    assert [(p["name"], p["kind"], p["playable"]) for p in listing["poses"]] == [
        ("ok", "gesture", True), ("one", "saved", True), ("two", "saved", True)]
    assert listing["poses"][0]["fingers"]["thumb"] == {"flex": 51.5, "side": 38.5}
    assert listing["error"] is None and listing["path"].endswith("poses.yaml")
    panel.delete_pose(A, "one")
    assert rejected(panel.delete_pose, A, "one") == "UNKNOWN_POSE"
    assert list(panel.store.load()) == ["two"]


def test_there_is_a_cap_on_the_number_of_poses(tmp, clock, monkeypatch):
    monkeypatch.setattr(poses, "MAX_POSES", 2)
    panel, fake = make(tmp, clock)
    on(panel)
    panel.save_pose(A, "one")
    panel.save_pose(A, "two")
    assert rejected(panel.save_pose, A, "three") == "TOO_MANY_POSES"
    panel.save_pose(A, "two", overwrite=True)
    assert list(panel.store.load()) == ["one", "two"]


def test_the_audit_log_tells_who_did_what(tmp, clock):
    panel, fake = make(tmp, clock)
    panel.beat(A, "192.0.2.7")
    panel.tick()
    panel.enable(A)
    panel.set_fingers(A, {"index": {"flex": 50}})
    panel.tick()
    panel.save_pose(A, "one")
    panel.play_pose(A, "ok")
    panel.tick()
    panel.stop(A)
    ev = events(tmp)
    assert [e["type"] for e in ev] == ["torque.on", "move.finished", "pose.saved", "pose.play", "move.finished",
                                       "torque.off"]
    assert all(e["caller"] == "operator.web:clientAA@192.0.2.7" for e in ev)
    assert ev[1]["data"]["targets"]["1"] == 50.0 and ev[1]["data"]["ok"] is True
    assert ev[-1]["data"] == {"reason": "stop", "was_on": True, "torque_off_failed": [], "fault": None}
    assert [e["type"] for e in panel.log_tail(2)["events"]] == ["move.finished", "torque.off"]


# --- 結束後的補救（safe_off）-----------------------------------------------------

def test_safe_off_does_nothing_without_a_marker(tmp):
    def never(port):
        raise AssertionError("不該碰匯流排")
    assert safe_off.main(["--runtime-dir", str(tmp)], adapter_factory=never) == 0


def test_safe_off_switches_torque_off_when_the_marker_is_there(tmp, capsys):
    fake = FakeHandAdapter()
    for sid in SERVO_IDS:
        fake.torque[sid] = True
    marker = tmp / core.MARKER_NAME
    marker.write_text(json.dumps({"adapter": "scs", "port": "/dev/ttyACM0"}))
    seen = []
    assert safe_off.main(["--runtime-dir", str(tmp)], adapter_factory=lambda p: seen.append(p) or fake) == 0
    assert seen == ["/dev/ttyACM0"] and not any(fake.torque.values()) and not marker.exists()
    assert writes(fake)[0] == 0 and "已對 8 顆送出關扭力" in capsys.readouterr().out


def test_safe_off_keeps_the_marker_when_it_cannot_confirm(tmp, capsys):
    marker = tmp / core.MARKER_NAME
    marker.write_text(json.dumps({"adapter": "scs", "port": "/dev/ttyACM0"}))
    busy = FakeHandAdapter()
    busy.busy = True
    safe_off.main(["--runtime-dir", str(tmp)], adapter_factory=lambda p: busy)
    assert marker.exists() and "切斷伺服機電源" in capsys.readouterr().out
    gone = FakeHandAdapter(missing={2})
    safe_off.main(["--runtime-dir", str(tmp)], adapter_factory=lambda p: gone)
    assert marker.exists() and "[2]" in capsys.readouterr().out


@pytest.mark.parametrize("content", ['{"adapter": "fake", "port": null}', "not json", "[]"])
def test_safe_off_ignores_markers_that_are_not_real_hardware(tmp, content):
    marker = tmp / core.MARKER_NAME
    marker.write_text(content)

    def never(port):
        raise AssertionError("不該碰匯流排")
    assert safe_off.main(["--runtime-dir", str(tmp)], adapter_factory=never) == 0
    assert not marker.exists()


# --- 真 adapter 接在假匯流排上 ---------------------------------------------------

def _real_panel(tmp, bus, **kw):
    cfg = config.load()
    a = adapter_mod.ScsHandAdapter(bus.port, timeout_s=0.2)
    panel = Panel(cfg, a, gateway.AuditLog(str(tmp / "events.jsonl")), poses.PoseStore(str(tmp / "poses.yaml")),
                  sleep=lambda s: None, marker_path=str(tmp / "torque.json"), **kw)
    return panel, a


def test_real_adapter_page_open_reads_without_writing_and_shares_the_bus_lock(tmp):
    pytest.importorskip("rustypot")
    from fake_scs_bus import FakeBus, FakeServo
    servos = [FakeServo(i) for i in SERVO_IDS]
    with FakeBus(servos) as bus:
        panel, a = _real_panel(tmp, bus, detach_after_s=0.05)
        panel.beat(A)
        panel.tick()
        s = panel.status(A)
        assert s["simulated"] is False and [x["ok"] for x in s["servos"]] == [True] * 8
        assert s["servos"][0]["voltage_v"] == 5.0 and abs(s["servos"][0]["position_deg"]) < 0.5
        assert bus.writes == []                                 # 開著頁面只有讀
        with pytest.raises(AdapterError) as e:                  # 頁面開著時，別的程式拿不到這條匯流排
            with adapter_mod.bus_lock(bus.port):
                pass
        assert e.value.code == "BUS_BUSY"
        time.sleep(0.1)
        panel.tick()                                            # 頁面不見了：放開
        with adapter_mod.bus_lock(bus.port):
            pass
        assert bus.writes == []


def test_real_adapter_reports_busy_while_another_program_holds_the_bus(tmp):
    pytest.importorskip("rustypot")
    from fake_scs_bus import FakeBus, FakeServo
    with FakeBus([FakeServo(i) for i in SERVO_IDS]) as bus:
        panel, a = _real_panel(tmp, bus)
        with adapter_mod.bus_lock(bus.port):                    # 例如 finger_cal.sh 正在跑
            panel.beat(A)
            panel.tick()
            assert panel.status(A)["bus"]["reason_code"] == "BUS_BUSY"
            with pytest.raises(Rejected) as e:
                panel.enable(A)
            assert e.value.reason_code == "BUS_BUSY"
        assert bus.writes == []


def test_real_adapter_enable_move_stop_through_the_wire_protocol(tmp):
    pytest.importorskip("rustypot")
    from fake_scs_bus import ADDR_TORQUE, FakeBus, FakeServo
    servos = [FakeServo(i) for i in SERVO_IDS]
    with FakeBus(servos) as bus:
        panel, a = _real_panel(tmp, bus)
        assert on(panel)["status"] == "accepted"
        assert [s.mem[ADDR_TORQUE] for s in servos] == [1] * 8
        panel.set_speed(A, "fast")
        panel.set_fingers(A, {"index": {"flex": 20}})
        panel.tick()
        s = panel.status(A)
        assert s["last_move"]["ok"] and abs(s["fingers"]["index"]["actual"]["flex"] - 20) < 0.5
        assert s["fault"] is None
        assert panel.stop(A)["torque_off"] == "ok"
        assert [x.mem[ADDR_TORQUE] for x in servos] == [0] * 8 and not (tmp / "torque.json").exists()
        panel.shutdown()


def test_real_adapter_power_cut_while_holding_latches_a_fault(tmp):
    pytest.importorskip("rustypot")
    from fake_scs_bus import FakeBus, FakeServo
    servos = [FakeServo(i) for i in SERVO_IDS]
    with FakeBus(servos) as bus:
        panel, a = _real_panel(tmp, bus)
        a.timeout_s = 0.05
        on(panel)
        for sv in servos:                                       # 電源閃斷：扭力自己關了
            sv.power_cycle()
        panel.beat(A)
        panel.tick()
        s = panel.status(A)
        assert s["torque_on"] is False and s["fault"]["reason_code"] == "TORQUE_LOST"
        panel.shutdown()


# --- HTTP ---------------------------------------------------------------------

TOKEN = "t0ken-for-tests-0123456789abcdef"


def _serve(tmp, token):
    cfg = config.load()
    fake = FakeHandAdapter(positions=motion.open_targets(cfg.middle_offsets, SERVO_IDS))
    panel = Panel(cfg, fake, gateway.AuditLog(str(tmp / "events.jsonl")), poses.PoseStore(str(tmp / "poses.yaml")),
                  sleep=lambda s: None, marker_path=str(tmp / "torque.json"))
    httpd = server.PanelHTTPServer(("127.0.0.1", 0), panel, token)
    panel.start()
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()

    class Web:
        port = httpd.server_address[1]

        def call(self, method, path, body=None, cookie=True, headers=None, raw=None):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            h = dict(headers or {})
            if cookie:
                h["Cookie"] = "%s=%s" % (server.COOKIE, TOKEN if cookie is True else cookie)
            data = raw
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                h.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            text = r.read().decode("utf-8")
            conn.close()
            parsed = None
            if r.getheader("Content-Type", "").startswith("application/json"):
                parsed = json.loads(text)
            return r.status, parsed if parsed is not None else text, dict(r.getheaders())

        def post(self, path, client=A, **body):
            return self.call("POST", path, dict(body, client=client))[1]

        def settle(self, client=A):
            for _ in range(200):
                s = self.call("GET", "/api/status?client=%s" % client)[1]
                if not s["moving"]:
                    return s
                time.sleep(0.02)
            raise AssertionError("沒有停下來")

    w = Web()
    w.panel, w.fake = panel, fake
    yield w
    httpd.shutdown()
    panel.shutdown()
    httpd.server_close()


@pytest.fixture
def web(tmp):
    """需要存取碼的模式（--require-token）。"""
    yield from _serve(tmp, TOKEN)


@pytest.fixture
def open_web(tmp):
    """預設模式：不需要存取碼（只在自己的內網用）。"""
    yield from _serve(tmp, None)


def test_by_default_the_page_and_the_api_need_no_token(open_web):
    w = open_web
    status, body, headers = w.call("GET", "/", cookie=False)
    assert status == 200 and "右手操作面板" in body and "Set-Cookie" not in headers
    assert w.call("GET", "/app.js", cookie=False)[0] == 200 and w.call("GET", "/app.css", cookie=False)[0] == 200
    s = w.call("GET", "/api/status?client=%s" % A, cookie=False)[1]
    assert s["status"] == "ok" and s["torque_on"] is False

    def post(path, **body):
        return w.call("POST", path, dict(body, client=A), cookie=False)[1]

    assert post("/api/enable")["status"] == "accepted"
    assert post("/api/fingers", fingers={"index": {"flex": 20}})["status"] == "accepted"
    for _ in range(200):
        s = w.call("GET", "/api/status?client=%s" % A, cookie=False)[1]
        if not s["moving"]:
            break
        time.sleep(0.02)
    assert s["you_control"] and s["fingers"]["index"]["actual"] == {"flex": 20.0, "side": 0.0}
    assert post("/api/poses/save", name="no_token")["status"] == "ok"
    assert [p["name"] for p in w.call("GET", "/api/poses", cookie=False)[1]["poses"]] == ["ok", "no_token"]
    assert post("/api/stop")["torque_off"] == "ok" and not any(w.fake.torque.values())
    assert w.call("GET", "/api/log", cookie=False)[0] == 200


def test_an_old_token_link_still_opens_the_page_when_no_token_is_needed(open_web):
    status, body, headers = open_web.call("GET", "/?token=some-old-bookmarked-token", cookie=False)
    assert status == 303 and headers["Location"] == "/" and "Set-Cookie" not in headers


def test_without_a_token_other_web_sites_still_cannot_drive_the_hand(open_web):
    w = open_web
    w.call("GET", "/api/status?client=%s" % A, cookie=False)
    body = {"client": A}
    raw = json.dumps(body).encode()
    status, r, _ = w.call("POST", "/api/enable", body, cookie=False, headers={"Origin": "http://evil.example"})
    assert status == 403 and r["reason_code"] == "CROSS_ORIGIN"
    status, r, _ = w.call("POST", "/api/enable", body, cookie=False, headers={"Origin": "null"})
    assert status == 403
    # 不需要 preflight 的跨站表單送法：Content-Type 不是 JSON，一律不收
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        assert w.call("POST", "/api/enable", raw=raw, cookie=False, headers={"Content-Type": ctype})[0] == 415
    assert w.call("POST", "/api/enable", raw=raw, cookie=False)[0] == 415
    time.sleep(0.1)
    assert writes(w.fake) == (0, 0) and w.panel.status()["torque_on"] is False


@pytest.mark.parametrize("mode", ["web", "open_web"])
def test_requests_addressed_to_another_host_name_are_refused(request, mode):
    """DNS rebinding：別的網域指到這台機器時，瀏覽器送來的 Host 是那個網域。"""
    w = request.getfixturevalue(mode)
    evil = {"Host": "panel.evil.example:%d" % w.port}
    assert w.call("GET", "/", headers=evil)[0] == 403
    assert w.call("GET", "/?token=" + TOKEN, headers=evil, cookie=False)[0] == 403
    status, body, _ = w.call("GET", "/api/status?client=%s" % A, headers=evil)
    assert status == 403 and body["reason_code"] == "BAD_HOST"
    same_origin = dict(evil, Origin="http://panel.evil.example:%d" % w.port)
    for path in server.POST_ROUTES:
        assert w.call("POST", path, {"client": A}, headers=same_origin)[0] == 403
    assert w.call("GET", "/api/poses", headers={"Host": ""})[0] == 403
    time.sleep(0.1)
    assert writes(w.fake) == (0, 0) and w.fake.connect_count == 0
    name = socket.gethostname()
    for host in ("localhost:%d" % w.port, "%s:%d" % (name, w.port), "%s.local" % name.upper(), "[::1]:%d" % w.port,
                 "192.168.0.10:8765", "10.0.0.5"):
        assert w.call("GET", "/api/poses", headers={"Host": host})[0] == 200


def test_extra_host_names_can_be_allowed():
    httpd = server.PanelHTTPServer(("127.0.0.1", 0), None, None, allowed_hosts=["Hand.Lan"])
    try:
        assert {"hand.lan", "localhost", socket.gethostname().lower()} <= httpd.allowed_hosts
        assert httpd.token is None
    finally:
        httpd.server_close()


def test_http_nothing_is_served_without_the_token(web):
    assert web.call("GET", "/", cookie=False)[0] == 401
    assert "需要存取碼" in web.call("GET", "/", cookie=False)[1]
    assert web.call("GET", "/app.js", cookie=False)[0] == 401
    for path in ("/api/status?client=%s" % A, "/api/poses", "/api/log"):
        status, body, _ = web.call("GET", path, cookie=False)
        assert status == 401 and body["reason_code"] == "UNAUTHORIZED"
    for path in server.POST_ROUTES:
        assert web.call("POST", path, {"client": A}, cookie=False)[0] == 401
        assert web.call("POST", path, {"client": A}, cookie="wrong-token-wrong-token")[0] == 401
    assert web.call("GET", "/?token=nope", cookie=False)[0] == 401
    assert writes(web.fake) == (0, 0) and web.fake.connect_count == 0


def test_http_the_token_link_sets_a_cookie_and_redirects(web):
    status, body, headers = web.call("GET", "/?token=" + TOKEN, cookie=False)
    assert status == 303 and headers["Location"] == "/" and body == ""
    cookie = headers["Set-Cookie"]
    assert cookie.startswith("%s=%s;" % (server.COOKIE, TOKEN)) and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, _, _ = web.call("GET", "/api/poses", cookie=False, headers={"Authorization": "Bearer " + TOKEN})
    assert status == 200


def test_http_static_files_come_with_a_strict_content_policy(web):
    for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")):
        status, body, headers = web.call("GET", path)
        assert status == 200 and headers["Content-Type"].startswith(kind) and len(body) > 500
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert headers["Cache-Control"] == "no-store" and headers["X-Frame-Options"] == "DENY"
        assert TOKEN not in body
    assert web.call("GET", "/../server.py")[0] == 404 and web.call("GET", "/static/app.js")[0] == 404


def test_the_page_has_no_inline_script_or_style_that_the_policy_would_block():
    html = open(os.path.join(RIGHT_HAND, "hand_panel", "static", "index.html"), encoding="utf-8").read()
    import re
    assert re.findall(r"<script(?![^>]*\bsrc=)", html) == []
    assert "<style" not in html and re.findall(r"\sstyle=|\son[a-z]+=", html) == []
    js = open(os.path.join(RIGHT_HAND, "hand_panel", "static", "app.js"), encoding="utf-8").read()
    assert "innerHTML" not in js and "eval(" not in js


def test_the_torque_area_has_fixed_slots_so_status_changes_do_not_move_the_page():
    """狀態、倒數、說明各有自己的位置；不能再有一塊會臨時插進頁面最上面的說明。"""
    static = os.path.join(RIGHT_HAND, "hand_panel", "static")
    html = open(os.path.join(static, "index.html"), encoding="utf-8").read()
    css = open(os.path.join(static, "app.css"), encoding="utf-8").read()
    for slot in ('id="power-badge"', 'id="power-timer"', 'id="power-state"'):
        assert html.count(slot) == 1
    assert 'id="notice"' not in html
    import re
    detail = re.search(r"\.power-detail \{([^}]*)\}", css).group(1)
    assert "height:" in detail and "overflow: hidden" in detail
    assert re.search(r"\.main \{[^}]*width:", css) and re.search(r"\.badge \{[^}]*min-width:", css)


def test_http_malformed_posts_are_refused_and_move_nothing(web):
    web.post("/api/enable")
    before = writes(web.fake)
    ok = {"client": A, "fingers": {"index": {"flex": 40}}}
    raw = json.dumps(ok).encode()
    assert web.call("POST", "/api/fingers", raw=raw)[0] == 415                              # 沒有 Content-Type
    assert web.call("POST", "/api/fingers", raw=raw, headers={"Content-Type": "text/plain"})[0] == 415
    status, body, _ = web.call("POST", "/api/fingers", ok, headers={"Origin": "http://evil.example"})
    assert status == 403 and body["reason_code"] == "CROSS_ORIGIN"
    assert web.call("POST", "/api/fingers", raw=b"{nope", headers={"Content-Type": "application/json"})[0] == 400
    assert web.call("POST", "/api/fingers", raw=b"[1]", headers={"Content-Type": "application/json"})[0] == 400
    assert web.call("POST", "/api/fingers", {"fingers": ok["fingers"]})[0] == 400            # 沒有 client
    assert web.call("POST", "/api/fingers", {"client": "x", "fingers": ok["fingers"]})[0] == 400
    assert web.call("POST", "/api/nope", ok)[0] == 404
    big = {"client": A, "fingers": ok["fingers"], "pad": "x" * server.MAX_BODY_BYTES}
    assert web.call("POST", "/api/fingers", big)[0] == 413
    r = web.post("/api/fingers", fingers=ok["fingers"], angle=5)
    assert r["status"] == "rejected" and r["reason_code"] == "PARAMETER_NOT_ALLOWED"
    r = web.post("/api/enable", speed="slow", torque_limit=1)
    assert r["reason_code"] == "PARAMETER_NOT_ALLOWED"
    time.sleep(0.1)
    assert writes(web.fake) == before
    same_origin = {"Origin": "http://127.0.0.1:%d" % web.port, "Host": "127.0.0.1:%d" % web.port}
    assert web.call("POST", "/api/fingers", ok, headers=same_origin)[1]["status"] == "accepted"


def test_http_full_flow_enable_move_save_replay_stop(web):
    s = web.call("GET", "/api/status?client=%s" % A)[1]
    assert s["status"] == "ok" and s["simulated"] is True and s["torque_on"] is False
    assert web.post("/api/fingers", fingers={"index": {"flex": 40}})["reason_code"] == "TORQUE_OFF"
    assert web.post("/api/enable", speed="normal")["status"] == "accepted"
    assert web.post("/api/fingers", fingers={"index": {"flex": 40, "side": 5}})["status"] == "accepted"
    s = web.settle()
    assert s["torque_on"] and s["you_control"] and s["fingers"]["index"]["actual"] == {"flex": 40.0, "side": 5.0}
    assert web.post("/api/poses/save", name="web_pose", label="網頁存的")["status"] == "ok"
    assert web.post("/api/poses/save", name="web_pose")["reason_code"] == "NAME_TAKEN"
    listing = web.call("GET", "/api/poses")[1]
    assert [p["name"] for p in listing["poses"]] == ["ok", "web_pose"]
    assert web.post("/api/open")["status"] == "accepted"
    assert web.settle()["fingers"]["index"]["actual"] == {"flex": -30.0, "side": 0.0}
    assert web.post("/api/speed", speed="fast")["status"] == "ok"
    assert web.post("/api/poses/play", name="web_pose")["status"] == "accepted"
    assert web.settle()["fingers"]["index"]["actual"] == {"flex": 40.0, "side": 5.0}
    assert (web.fake.positions[1], web.fake.positions[2]) == (45.0, -35.0)
    web.call("GET", "/api/status?client=%s" % B)
    assert web.post("/api/fingers", client=B, fingers={"index": {"flex": 0}})["reason_code"] == "CONTROL_HELD"
    r = web.post("/api/stop", client=B)                          # 另一個頁面也能停
    assert r["status"] == "accepted" and r["torque_off"] == "ok" and not any(web.fake.torque.values())
    assert web.post("/api/poses/delete", name="web_pose")["status"] == "ok"
    log = web.call("GET", "/api/log?n=50")[1]["events"]
    assert [e["type"] for e in log][:2] == ["torque.on", "move.finished"] and log[-1]["type"] == "pose.deleted"
    assert log[0]["caller"].endswith("@127.0.0.1")


def test_http_torque_goes_off_when_the_page_stops_polling(web):
    web.panel.heartbeat_timeout_s = 0.3
    web.call("GET", "/api/status?client=%s" % A)
    assert web.post("/api/enable")["status"] == "accepted"
    assert all(web.fake.torque.values())
    time.sleep(1.0)                                              # 頁面關掉了：沒有人再查狀態
    assert not any(web.fake.torque.values())
    s = web.call("GET", "/api/status?client=%s" % A)[1]
    assert s["torque_on"] is False and s["last_release"]["reason"] == "watchdog" and s["fault"] is None


def test_the_token_file_is_private_and_stable(tmp):
    d = tmp / "run"
    token = server.load_or_create_token(str(d))
    assert len(token) >= 24 and server.load_or_create_token(str(d)) == token
    assert stat.S_IMODE(os.stat(d / server.TOKEN_FILE).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_print_url_prints_the_link_and_starts_nothing(tmp, capsys):
    args = ["--print-url", "--runtime-dir", str(tmp), "--host", "127.0.0.1", "--http-port", "9"]
    assert server.main(args) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:9/"
    assert os.listdir(tmp) == []                              # 預設不用存取碼：也不產生存取碼檔
    assert server.main(args + ["--require-token"]) == 0
    token = (tmp / server.TOKEN_FILE).read_text().strip()
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:9/?token=%s" % token
    assert os.listdir(tmp) == [server.TOKEN_FILE]


def test_startup_refuses_a_silly_hold_timeout(tmp, capsys):
    assert server.main(["--runtime-dir", str(tmp), "--hold-timeout", "100000", "--http-port", "0"]) == 1
    assert "hold-timeout" in capsys.readouterr().out
