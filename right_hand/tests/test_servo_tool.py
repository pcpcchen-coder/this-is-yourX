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
from fake_scs_bus import ADDR_ID, ADDR_LOCK, ADDR_TORQUE, FakeBus, FakeServo  # noqa: E402


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
