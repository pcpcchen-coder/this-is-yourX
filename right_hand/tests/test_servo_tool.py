"""servo_tool 的測試，全部跑在假匯流排上，不需要實體硬體。

    pip install -r right_hand/requirements.txt pytest
    pytest right_hand/tests -q
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

pytest.importorskip("rustypot")

import servo_tool  # noqa: E402
from fake_scs_bus import (ADDR_GOAL, ADDR_ID, ADDR_LOCK, ADDR_TORQUE, ADDR_VOLT,  # noqa: E402
                          FakeBus, FakeServo)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(servo_tool.time, "sleep", lambda s: None)


def run(*args):
    return servo_tool.main(["servo_tool.py", *[str(a) for a in args]])


def test_scan_reports_the_servo(capsys):
    with FakeBus([FakeServo(1)]) as bus:
        assert run("scan", bus.port) == 0
    assert "ID 1  SCS0009" in capsys.readouterr().out


def test_scan_empty_bus_fails(capsys):
    with FakeBus([]) as bus:
        assert run("scan", bus.port) == 1
    assert "沒有伺服機回應" in capsys.readouterr().out


def test_setid_unlocks_writes_then_relocks_and_survives_power_cycle():
    servo = FakeServo(1)
    with FakeBus([servo]) as bus:
        assert run("setid", bus.port, 1, 3) == 0
        assert bus.writes == [(1, ADDR_LOCK, [0]), (1, ADDR_ID, [3]), (3, ADDR_LOCK, [1])]
        servo.power_cycle()
        assert servo.sid == 3


def test_setid_refuses_when_two_servos_are_connected(capsys):
    with FakeBus([FakeServo(1), FakeServo(2)]) as bus:
        assert run("setid", bus.port, 1, 5) == 1
        assert bus.writes == []
    assert "只能接一顆" in capsys.readouterr().out


def test_setid_refuses_when_old_id_is_not_the_connected_one():
    with FakeBus([FakeServo(4)]) as bus:
        assert run("setid", bus.port, 1, 5) == 1
        assert bus.writes == []


def test_setid_rejects_out_of_range_new_id():
    with FakeBus([FakeServo(1)]) as bus:
        assert run("setid", bus.port, 1, 254) == 1
        assert bus.writes == []


def test_wiggle_passes_and_leaves_torque_off(capsys):
    servo = FakeServo(1)
    with FakeBus([servo]) as bus:
        assert run("test", bus.port, 1) == 0
    assert servo.torque_history == [1, 0]
    assert servo.mem[ADDR_TORQUE] == 0
    assert "結果：正常" in capsys.readouterr().out


def test_wiggle_on_a_stuck_servo_fails_and_leaves_torque_off(capsys):
    servo = FakeServo(1, stuck=True)
    with FakeBus([servo]) as bus:
        assert run("test", bus.port, 1) == 1
    assert servo.torque_history[-1] == 0
    assert "異常" in capsys.readouterr().out


@pytest.mark.parametrize("volt_dv", [35, 120])
def test_wiggle_refuses_out_of_range_voltage_without_enabling_torque(volt_dv, capsys):
    servo = FakeServo(1, volt_dv=volt_dv)
    with FakeBus([servo]) as bus:
        assert run("test", bus.port, 1) == 1
    assert servo.torque_history == []
    assert "不動作" in capsys.readouterr().out


def test_wiggle_refuses_overtemperature_without_enabling_torque():
    servo = FakeServo(1, temp_c=75)
    with FakeBus([servo]) as bus:
        assert run("test", bus.port, 1) == 1
    assert servo.torque_history == []


def test_wiggle_refuses_when_more_than_one_servo_is_connected():
    a, b = FakeServo(1), FakeServo(2)
    with FakeBus([a, b]) as bus:
        assert run("test", bus.port, 1) == 1
    assert a.torque_history == [] and b.torque_history == []


def test_torque_is_disabled_when_the_servo_stops_answering_mid_wiggle():
    servo = FakeServo(1)
    with FakeBus([servo]) as bus:
        real = servo_tool.one
        calls = {"n": 0}

        def flaky(v):
            calls["n"] += 1
            if calls["n"] == 5:          # 擺動途中的一次位置讀取
                raise RuntimeError("timeout")
            return real(v)

        servo_tool.one = flaky
        try:
            assert run("test", bus.port, 1) == 1
        finally:
            servo_tool.one = real
    assert servo.torque_history == [1, 0]


def test_diag_reports_a_responding_servo_and_writes_nothing(capsys):
    pytest.importorskip("serial")
    with FakeBus([FakeServo(1)]) as bus:
        assert run("diag", bus.port) == 0
        assert bus.writes == []
    assert "伺服機有回應，ID 1" in capsys.readouterr().out


def test_diag_finds_a_servo_with_an_unexpected_id_via_broadcast(capsys):
    pytest.importorskip("serial")
    with FakeBus([FakeServo(7)]) as bus:
        assert run("diag", bus.port) == 0
    assert "ID 7" in capsys.readouterr().out


def test_diag_distinguishes_echo_only_from_silence(capsys):
    pytest.importorskip("serial")
    with FakeBus([], echo=True) as bus:
        assert run("diag", bus.port) == 1
    assert "只收到自己送出的資料" in capsys.readouterr().out
    with FakeBus([]) as bus:
        assert run("diag", bus.port) == 1
    assert "完全沒有資料回來" in capsys.readouterr().out


def test_classify_reply_strips_echo_and_checks_checksum():
    sent = servo_tool._ping_packet(1)
    good = bytes([0xFF, 0xFF, 1, 2, 0, 0xFC])
    assert servo_tool.classify_reply(sent, sent + good) == (True, 1)
    assert servo_tool.classify_reply(sent, good) == (False, 1)
    assert servo_tool.classify_reply(sent, sent) == (True, None)
    assert servo_tool.classify_reply(sent, bytes([0xFF, 0xFF, 1, 2, 0, 0x00])) == (False, None)


# --- center / finger：一根手指的兩顆 ---------------------------------------

MID_RAW = 511


class Enter:
    """取代 wait_enter：記下每次被呼叫時兩顆的扭力狀態。"""

    def __init__(self, servos, raise_on=None):
        self.servos, self.raise_on, self.seen = servos, raise_on, []
        self.timeouts, self.pressed = [], True

    def __call__(self, msg, timeout=None):
        self.seen.append([s.mem[ADDR_TORQUE] for s in self.servos])
        self.timeouts.append(timeout)
        if self.raise_on == len(self.seen):
            raise KeyboardInterrupt
        return self.pressed


@pytest.fixture
def pair(monkeypatch):
    a, b = FakeServo(1), FakeServo(2)
    enter = Enter([a, b])
    monkeypatch.setattr(servo_tool, "wait_enter", enter)
    return a, b, enter


def test_center_holds_torque_until_enter_then_releases_both(pair, capsys):
    a, b, enter = pair
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2) == 0
    assert enter.seen == [[1, 1]]                 # 等人裝舵盤時兩顆都有扭力
    assert a.torque_history == [1, 0] and b.torque_history == [1, 0]
    # 第一筆是開扭力前「停在原地」的目標，第二筆才是中位
    assert a.goal_history == [MID_RAW, MID_RAW] and b.goal_history == [MID_RAW, MID_RAW]
    assert "扭力已關" in capsys.readouterr().out


def test_center_applies_middle_offsets_with_their_signs(pair):
    a, b, _ = pair
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2, 10, -10) == 0
    assert a.goal_history[-1] > MID_RAW > b.goal_history[-1]


@pytest.mark.parametrize("ids", [(2, 3), (1, 3), (2, 1), (9, 10)])
def test_center_rejects_ids_that_are_not_one_finger_without_touching_the_bus(ids, pair):
    a, b, enter = pair
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, *ids) == 1
        assert bus.writes == []
    assert enter.seen == []


@pytest.mark.parametrize("mids", [(31, 0), (0, -45)])
def test_center_rejects_out_of_range_middle_offset(mids, pair):
    a, b, _ = pair
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2, *mids) == 1
        assert bus.writes == []


@pytest.mark.parametrize("ids_on_bus", [[1], [1, 2, 3], [3, 4]])
def test_center_refuses_when_the_bus_is_not_exactly_this_finger(ids_on_bus, monkeypatch):
    servos = [FakeServo(i) for i in ids_on_bus]
    monkeypatch.setattr(servo_tool, "wait_enter", Enter(servos))
    with FakeBus(servos) as bus:
        assert run("center", bus.port, 1, 2) == 1
        assert bus.writes == []


def test_center_refuses_when_either_servo_has_bad_voltage(monkeypatch):
    a, b = FakeServo(1), FakeServo(2, volt_dv=35)
    monkeypatch.setattr(servo_tool, "wait_enter", Enter([a, b]))
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2) == 1
    assert a.torque_history == [] and b.torque_history == []


def test_center_does_not_ask_for_horns_when_a_servo_fails_to_reach_middle(monkeypatch, capsys):
    a, b = FakeServo(1), FakeServo(2, stuck=True)
    b.mem[56], b.mem[57] = 0x00, 0x64             # 卡在離中位很遠的地方
    enter = Enter([a, b])
    monkeypatch.setattr(servo_tool, "wait_enter", enter)
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2) == 1
    assert enter.seen == []
    assert a.torque_history[-1] == 0 and b.torque_history[-1] == 0
    assert "沒有到中位" in capsys.readouterr().out


def test_center_releases_torque_on_ctrl_c(monkeypatch):
    a, b = FakeServo(1), FakeServo(2)
    monkeypatch.setattr(servo_tool, "wait_enter", Enter([a, b], raise_on=1))
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2) == 130
    assert a.torque_history == [1, 0] and b.torque_history == [1, 0]


def test_finger_runs_one_mirrored_open_close_cycle_and_releases_torque(pair, capsys):
    a, b, enter = pair
    with FakeBus([a, b]) as bus:
        assert run("finger", bus.port, 1, 2) == 0
    # 開始前還沒開扭力；閉合停住時兩顆都有扭力
    assert enter.seen == [[0, 0], [1, 1]]
    assert a.torque_history == [1, 0] and b.torque_history == [1, 0]
    # 停在原地、張開、中位、三段閉合、張開、中位，共 8 個目標；兩顆以中位為軸鏡像
    assert len(a.goal_history) == 8
    for ga, gb in zip(a.goal_history, b.goal_history):       # 換算成原始值時各自四捨五入，差 1 以內
        assert abs((ga - MID_RAW) + (gb - MID_RAW)) <= 1
    assert max(a.goal_history) == a.goal_history[5] and a.goal_history[-1] == MID_RAW
    out = capsys.readouterr().out
    assert "閉合 90°" in out and "完成，扭力已關" in out


def test_finger_stops_and_releases_torque_when_the_mechanism_binds(monkeypatch, capsys):
    a = FakeServo(1, travel=(300, 650))           # 約 +40° 就走不動
    b = FakeServo(2)
    enter = Enter([a, b])
    monkeypatch.setattr(servo_tool, "wait_enter", enter)
    with FakeBus([a, b]) as bus:
        assert run("finger", bus.port, 1, 2) == 1
    assert len(enter.seen) == 1                   # 沒有走到「閉合停住」那一步
    assert len(a.goal_history) == 5               # 停在原地、張開、中位、30°、60° 就停
    assert a.torque_history[-1] == 0 and b.torque_history[-1] == 0
    assert "卡住" in capsys.readouterr().out


def test_finger_releases_torque_when_a_servo_stops_answering(pair):
    a, b, _ = pair
    with FakeBus([a, b]) as bus:
        real = servo_tool.one
        calls = {"n": 0}

        def flaky(v):
            calls["n"] += 1
            if calls["n"] == 12:                  # 開合途中的一次位置讀取
                raise RuntimeError("timeout")
            return real(v)

        servo_tool.one = flaky
        try:
            assert run("finger", bus.port, 1, 2) == 1
        finally:
            servo_tool.one = real
    assert a.torque_history == [1, 0] and b.torque_history == [1, 0]


def test_finger_releases_torque_on_ctrl_c_while_closed(monkeypatch):
    a, b = FakeServo(1), FakeServo(2)
    monkeypatch.setattr(servo_tool, "wait_enter", Enter([a, b], raise_on=2))
    with FakeBus([a, b]) as bus:
        assert run("finger", bus.port, 1, 2) == 130
    assert a.torque_history == [1, 0] and b.torque_history == [1, 0]


def test_finger_does_nothing_if_cancelled_before_start(monkeypatch):
    a, b = FakeServo(1), FakeServo(2)
    monkeypatch.setattr(servo_tool, "wait_enter", Enter([a, b], raise_on=1))
    with FakeBus([a, b]) as bus:
        assert run("finger", bus.port, 1, 2) == 130
        assert bus.writes == []


def test_move_pair_reports_the_settled_position_not_the_first_reading_in_tolerance():
    # 2026-10-07 實機：第一次進到容許範圍就回報，印出的是途中的讀值（目標 -30° 印 -23.4°）。
    readings = {1: iter([-22.0, -26.0, -29.0, -29.8, -29.9, -29.9]),
                2: iter([22.0, 26.0, 29.0, 29.8, 29.9, 29.9])}

    class Ctl:
        def write_goal_position(self, sid, rad):
            pass

        def read_present_position(self, sid):
            return [servo_tool.math.radians(next(readings[sid]))]

    ok, a, b = servo_tool._move_pair(Ctl(), 1, 2, -30, 30, servo_tool.FINGER_TOLERANCE_DEG)
    assert ok and abs(a + 29.9) < 0.05 and abs(b - 29.9) < 0.05


def test_move_pair_fails_when_a_servo_settles_outside_tolerance():
    class Ctl:
        def write_goal_position(self, sid, rad):
            pass

        def read_present_position(self, sid):
            return [servo_tool.math.radians(40.0 if sid == 1 else -90.0)]

    ok, a, b = servo_tool._move_pair(Ctl(), 1, 2, 90, -90, servo_tool.FINGER_TOLERANCE_DEG)
    assert not ok and abs(a - 40.0) < 0.05


def test_torque_is_enabled_only_after_speed_and_a_hold_position_goal_are_set(pair):
    # 開扭力時伺服機會朝目標暫存器裡的舊值走；先把目標設成目前位置才不會衝。
    a, b, _ = pair
    a.mem[56], a.mem[57] = 0x02, 0x58             # ID 1 目前在 600，不在中位
    with FakeBus([a, b]) as bus:
        assert run("center", bus.port, 1, 2) == 0
        for sid, servo in ((1, a), (2, b)):
            addrs = [addr for s_, addr, _ in bus.writes if s_ == sid]
            on = addrs.index(ADDR_TORQUE)
            assert 46 in addrs[:on] and ADDR_GOAL in addrs[:on]
    assert a.goal_history[0] == 600               # 第一個目標就是原地


# --- hand：8 顆全接 -------------------------------------------------------

def make_hand(monkeypatch, servos=None, raise_on=None):
    servos = servos or [FakeServo(i) for i in range(1, 9)]
    enter = Enter(servos, raise_on=raise_on)
    monkeypatch.setattr(servo_tool, "wait_enter", enter)
    return servos, enter


def goal_order_after_engage(bus):
    last_on = max(i for i, (_, addr, data) in enumerate(bus.writes) if addr == ADDR_TORQUE and data == [1])
    return [sid for sid, addr, _ in bus.writes[last_on + 1:] if addr == ADDR_GOAL]


def test_hand_opens_all_then_cycles_one_finger_at_a_time(monkeypatch, capsys):
    servos, enter = make_hand(monkeypatch)
    with FakeBus(servos) as bus:
        assert run("hand", bus.port) == 0
        order = goal_order_after_engage(bus)
    assert enter.seen == [[0] * 8]                # 按 Enter 前沒有任何一顆有扭力
    assert order == list(range(1, 9)) + [1, 2] * 5 + [3, 4] * 5 + [5, 6] * 5 + [7, 8] * 5
    assert all(sv.torque_history == [1, 0] for sv in servos)
    out = capsys.readouterr().out
    assert "拇指" in out and "完成，扭力已關" in out


@pytest.mark.parametrize("ids", [[1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2]])
def test_hand_refuses_unless_exactly_ids_1_to_8_answer(ids, monkeypatch, capsys):
    servos, enter = make_hand(monkeypatch, [FakeServo(i) for i in ids])
    with FakeBus(servos) as bus:
        assert run("hand", bus.port) == 1
        assert bus.writes == []
    assert enter.seen == [] and "ID 1 到 8" in capsys.readouterr().out


def test_hand_refuses_when_any_servo_has_bad_voltage(monkeypatch):
    servos, enter = make_hand(monkeypatch, [FakeServo(i, volt_dv=36 if i == 6 else 50) for i in range(1, 9)])
    with FakeBus(servos) as bus:
        assert run("hand", bus.port) == 1
        assert bus.writes == []
    assert enter.seen == []


def test_hand_rejects_a_wrong_number_of_middle_offsets(monkeypatch):
    servos, _ = make_hand(monkeypatch)
    with FakeBus(servos) as bus:
        assert servo_tool.cmd_hand(bus.port, [0, 0, 0]) == 1
        assert servo_tool.cmd_hand(bus.port, [0] * 7 + [45]) == 1
        assert bus.writes == []


def test_hand_stops_at_the_finger_that_binds_and_releases_all_torque(monkeypatch, capsys):
    servos = [FakeServo(i, travel=(300, 650) if i == 3 else None) for i in range(1, 9)]
    make_hand(monkeypatch, servos)
    with FakeBus(servos) as bus:
        assert run("hand", bus.port) == 1
    assert all(sv.torque_history[-1] == 0 for sv in servos)
    assert len(servos[4].goal_history) == 2       # 無名指只有「停在原地」和「張開」，沒有輪到它閉合
    out = capsys.readouterr().out
    assert "卡住" in out and "無名指（" not in out


def test_hand_stops_when_supply_voltage_sags_after_a_finger_cycle(monkeypatch, capsys):
    class Sagging(FakeServo):
        def write(self, addr, data):
            super().write(addr, data)
            if len(self.goal_history) >= 7:       # 食指走完一輪後電壓掉下去
                self.mem[ADDR_VOLT] = 38

    servos = [Sagging(1)] + [FakeServo(i) for i in range(2, 9)]
    make_hand(monkeypatch, servos)
    with FakeBus(servos) as bus:
        assert run("hand", bus.port) == 1
    assert all(sv.torque_history[-1] == 0 for sv in servos)
    assert len(servos[2].goal_history) == 2       # 中指沒有開始閉合
    assert "電壓掉到" in capsys.readouterr().out


def test_hand_does_nothing_if_cancelled_before_start(monkeypatch):
    servos, _ = make_hand(monkeypatch, raise_on=1)
    with FakeBus(servos) as bus:
        assert run("hand", bus.port) == 130
        assert bus.writes == []


def test_hand_applies_per_servo_middle_offsets(monkeypatch):
    servos, _ = make_hand(monkeypatch)
    with FakeBus(servos) as bus:
        assert run("hand", bus.port, 5, -5, 0, 0, 0, 0, 0, 0) == 0
    # 最後一個目標是張開：ID 1 是 mid-30、ID 2 是 mid+30；修正值各自往自己的方向平移
    assert servos[0].goal_history[-1] > servos[2].goal_history[-1]
    assert servos[1].goal_history[-1] < servos[3].goal_history[-1]


# --- gesture：固定手勢 ----------------------------------------------------

def test_gesture_ok_opens_poses_in_order_holds_then_returns_in_reverse(monkeypatch, capsys):
    servos, enter = make_hand(monkeypatch)
    with FakeBus(servos) as bus:
        assert run("gesture", bus.port, "ok") == 0
        order = goal_order_after_engage(bus)
    assert order == (list(range(1, 9))                      # 全部張開
                     + [3, 4, 5, 6, 1, 2, 7, 8]             # 中指、無名指、食指、拇指
                     + [7, 8, 1, 2, 5, 6, 3, 4])            # 反序收回
    assert enter.seen == [[0] * 8, [1] * 8]                  # 開始前沒扭力；停住時 8 顆都有
    assert enter.timeouts == [None, servo_tool.HOLD_MAX_S]   # 停住那次有上限
    assert all(sv.torque_history == [1, 0] for sv in servos)
    pose = {sv.sid: sv.goal_history[2] for sv in servos}     # [停在原地, 張開, 手勢, 張開]
    assert pose[1] > MID_RAW > pose[2]                        # 食指彎曲
    assert pose[7] - MID_RAW > 5 * abs(pose[8] - MID_RAW)     # 拇指兩顆不對稱：彎曲加側擺
    assert all(sv.goal_history[-1] == sv.goal_history[1] for sv in servos)   # 最後回到張開
    assert "完成，扭力已關" in capsys.readouterr().out


def test_gesture_returns_by_itself_when_nobody_presses_enter(monkeypatch, capsys):
    servos, enter = make_hand(monkeypatch)
    enter.pressed = False
    with FakeBus(servos) as bus:
        assert run("gesture", bus.port, "ok") == 0
    assert all(sv.torque_history == [1, 0] for sv in servos)
    assert "自動收回" in capsys.readouterr().out


def test_gesture_rejects_an_unknown_name_without_touching_the_bus(monkeypatch, capsys):
    servos, enter = make_hand(monkeypatch)
    with FakeBus(servos) as bus:
        assert run("gesture", bus.port, "fist") == 1
        assert bus.writes == []
    assert enter.seen == [] and "沒有" in capsys.readouterr().out


def test_gesture_refuses_unless_exactly_ids_1_to_8_answer(monkeypatch):
    servos, enter = make_hand(monkeypatch, [FakeServo(i) for i in range(1, 8)])
    with FakeBus(servos) as bus:
        assert run("gesture", bus.port, "ok") == 1
        assert bus.writes == []
    assert enter.seen == []


def test_gesture_refuses_a_pose_that_offsets_push_out_of_range(monkeypatch):
    servos, _ = make_hand(monkeypatch)
    monkeypatch.setitem(servo_tool.GESTURES, "far", {"label": "far", "order": (1,), "pose": {1: (90, -90)}})
    with FakeBus(servos) as bus:
        assert servo_tool.cmd_gesture(bus.port, "far", [10, 0, 0, 0, 0, 0, 0, 0]) == 1
        assert bus.writes == []


def test_gesture_stops_without_holding_when_the_thumb_cannot_reach_the_pose(monkeypatch, capsys):
    servos = [FakeServo(i, travel=(300, 600) if i == 7 else None) for i in range(1, 9)]
    _, enter = make_hand(monkeypatch, servos)
    with FakeBus(servos) as bus:
        assert run("gesture", bus.port, "ok") == 1
    assert len(enter.seen) == 1                               # 沒有進到「停住」
    assert all(sv.torque_history[-1] == 0 for sv in servos)
    assert "卡住" in capsys.readouterr().out


def test_gesture_releases_torque_on_ctrl_c_while_holding(monkeypatch):
    servos, _ = make_hand(monkeypatch, raise_on=2)
    with FakeBus(servos) as bus:
        assert run("gesture", bus.port, "ok") == 130
    assert all(sv.torque_history == [1, 0] for sv in servos)


def test_every_gesture_in_the_table_is_complete_and_within_limits():
    for name, g in servo_tool.GESTURES.items():
        assert sorted(g["order"]) == [1, 3, 5, 7], name      # 四根手指各出現一次
        assert set(g["pose"]) == {1, 3, 5, 7}, name
        for a, b in g["pose"].values():
            # 表裡的角度不含中位修正，自己不得超過 ±90°；加上修正後的 ±95° 由執行時檢查
            assert abs(a) <= 90 and abs(b) <= 90, name


def test_wait_enter_with_timeout_reports_whether_enter_was_pressed(monkeypatch):
    r, w = os.pipe()
    with os.fdopen(r) as reader:
        monkeypatch.setattr(servo_tool.sys, "stdin", reader)
        assert servo_tool.wait_enter("x", timeout=0.05) is False
        os.write(w, b"\n")
        assert servo_tool.wait_enter("x", timeout=1) is True
    os.close(w)
