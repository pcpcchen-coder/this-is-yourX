"""QRing Hand 的測試：全部用模擬戒指與模擬手掌面板，不碰藍牙、不碰真的手。

  .venv/bin/python -m unittest -v test_hand.py      # 只需要 aiohttp；不需要 macOS
"""
import asyncio
import json
import math
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import server as app_server
from engine import DEFAULTS, Engine, clean_options
from follow import (BUILTIN_FIST, CurlFilter, CurlModel, StabilityWindow, angle_deg, clamp_finger, finger_targets,
                    unit)
from hand import normalize_url
from ring import pack
from sim import FIST_VEC, OPEN_VEC, FakeRingTransport, slerp


def raw(vec, magnitude=1.0):
    return [int(round(x * 8192 * magnitude)) for x in unit(vec)]


class FakePanel:
    """照 right_hand/hand_panel 的 HTTP 介面做的假面板；只記錄收到的東西，不做移動。"""

    def __init__(self):
        self.torque = False
        self.controller = None
        self.fault = None
        self.last_release = None
        self.bus = "attached"
        self.servo_ok = True
        self.targets = {}
        self.posts = []
        self.beats = 0
        self.fail = False
        self.poses = [
            {"name": "stone", "label": "", "kind": "saved", "playable": True,
             "fingers": {"index": {"flex": 90.0, "side": 0.0}, "middle": {"flex": 90.0, "side": 0.0},
                         "ring": {"flex": 89.0, "side": 0.0}, "thumb": {"flex": 89.0, "side": -1.0}}},
            {"name": "old", "label": "", "kind": "saved", "playable": False, "fingers": {}},
        ]
        app = web.Application()
        app.router.add_get("/api/status", self.status)
        app.router.add_get("/api/poses", self.list_poses)
        app.router.add_post("/api/{name}", self.post)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()
        return "http://127.0.0.1:%d" % self.server.port

    async def close(self):
        await self.server.close()

    def release(self, reason, fault=None):
        self.torque, self.controller = False, None
        self.last_release = {"reason": reason, "fault": fault["reason_code"] if fault else None}
        if fault:
            self.fault = fault

    def calls(self, path):
        return [body for p, body in self.posts if p == path]

    async def status(self, req):
        if self.fail:
            raise web.HTTPServiceUnavailable()
        client = req.query.get("client")
        self.beats += 1
        return web.json_response({
            "status": "ok", "adapter": "fake", "simulated": True,
            "bus": {"state": self.bus, "reason_code": None, "detail": "序列埠正被另一個程式使用" if self.bus != "attached" else ""},
            "torque_on": self.torque, "you_control": bool(client) and client == self.controller,
            "controller": self.controller[:8] if self.controller else None, "speed": "slow", "moving": False,
            "fault": self.fault, "hold_remaining_s": None, "hold_timeout_s": 60.0,
            "fingers": {}, "last_release": self.last_release, "last_move": None,
            "servos": [{"id": i, "ok": self.servo_ok, "voltage_v": 5.0, "temperature_c": 25.0} for i in range(1, 9)],
        })

    async def list_poses(self, req):
        return web.json_response({"status": "ok", "poses": self.poses})

    async def post(self, req):
        if self.fail:
            raise web.HTTPServiceUnavailable()
        name = req.match_info["name"]
        body = await req.json()
        client = body.get("client")
        self.posts.append((name, body))
        if name == "enable":
            if self.fault:
                return web.json_response({"status": "rejected", "reason_code": "ACTIVE_FAULT", "detail": "有還沒清除的 fault"})
            if self.torque and self.controller != client:
                return web.json_response({"status": "rejected", "reason_code": "CONTROL_HELD", "detail": "另一個頁面正在控制這隻手"})
            self.torque, self.controller, self.last_release = True, client, None
            return web.json_response({"status": "accepted", "torque_on": True, "speed": body.get("speed")})
        if name == "stop":
            was = self.torque
            self.release("stop")
            return web.json_response({"status": "accepted", "torque_off": "ok", "was_on": was})
        if name == "fingers":
            if not self.torque:
                return web.json_response({"status": "rejected", "reason_code": "TORQUE_OFF", "detail": "扭力關著"})
            if self.controller != client:
                return web.json_response({"status": "rejected", "reason_code": "CONTROL_HELD", "detail": "另一個頁面"})
            self.targets = body["fingers"]
            return web.json_response({"status": "accepted", "fingers": body["fingers"], "clamped": []})
        if name == "clear_fault":
            old, self.fault = self.fault, None
            return web.json_response({"status": "ok", "cleared": old})
        return web.json_response({"status": "error", "reason_code": "NOT_FOUND"}, status=404)


class FollowMath(unittest.TestCase):
    def test_open_fist_and_midpoint(self):
        model = CurlModel(OPEN_VEC, FIST_VEC)
        self.assertAlmostEqual(model.measure(raw(OPEN_VEC))["progress"], 0.0, places=2)
        self.assertAlmostEqual(model.measure(raw(FIST_VEC))["progress"], 1.0, places=2)
        mid = model.measure(raw(slerp(OPEN_VEC, FIST_VEC, 0.5)))
        self.assertTrue(mid["valid"])
        self.assertAlmostEqual(mid["progress"], 0.5, places=2)
        self.assertLess(abs(mid["off"]), 1.0)

    def test_progress_is_monotonic_along_the_arc(self):
        model = CurlModel(OPEN_VEC, FIST_VEC)
        values = [model.measure(raw(slerp(OPEN_VEC, FIST_VEC, t / 20)))["progress"] for t in range(-4, 25)]
        self.assertEqual(values, sorted(values))

    def test_too_small_span_is_rejected(self):
        near = slerp(OPEN_VEC, FIST_VEC, 0.1)
        self.assertLess(angle_deg(OPEN_VEC, near), 20)
        with self.assertRaises(ValueError):
            CurlModel(OPEN_VEC, near)
        with self.assertRaises(ValueError):
            CurlModel(OPEN_VEC, [float("nan"), 0, 1])
        with self.assertRaises(ValueError):
            CurlModel(OPEN_VEC, [-x for x in OPEN_VEC])

    def test_moving_hand_is_not_trusted(self):
        model = CurlModel(OPEN_VEC, FIST_VEC)
        for magnitude in (0.5, 1.5):
            m = model.measure(raw(FIST_VEC, magnitude))
            self.assertFalse(m["valid"])
            self.assertEqual(m["issue"], "moving")
        self.assertEqual(model.measure([0, 0, 0])["issue"], "moving")
        self.assertEqual(model.measure([1, "x", 3])["issue"], "invalid")

    def test_wrist_rotation_off_the_arc_is_flagged(self):
        model = CurlModel(OPEN_VEC, FIST_VEC)
        m = model.measure(raw(model.normal))
        self.assertFalse(m["valid"])
        self.assertEqual(m["issue"], "off_axis")
        mid = slerp(OPEN_VEC, FIST_VEC, 0.5)
        tilted = unit(tuple(math.cos(math.radians(20)) * mid[i] + math.sin(math.radians(20)) * model.normal[i]
                            for i in range(3)))
        self.assertTrue(model.measure(raw(tilted), off_limit=35)["valid"])
        self.assertFalse(model.measure(raw(tilted), off_limit=15)["valid"])

    def test_far_beyond_the_ends_is_out_of_range(self):
        model = CurlModel(OPEN_VEC, FIST_VEC)
        behind = model.measure(raw([-x for x in slerp(OPEN_VEC, FIST_VEC, 0.5)]))
        self.assertFalse(behind["valid"])
        self.assertEqual(behind["issue"], "out_of_range")

    def test_filter_margins_and_smoothing(self):
        f = CurlFilter()
        self.assertEqual(f.update(0.05, 0.12, 0.88, 0.0), 0.0)
        self.assertEqual(f.update(0.95, 0.12, 0.88, 0.0), 1.0)
        self.assertAlmostEqual(f.update(0.5, 0.12, 0.88, 0.0), 0.5, places=3)
        f.reset()
        f.update(0.0, 0.0, 1.0, 0.5)
        self.assertAlmostEqual(f.update(1.0, 0.0, 1.0, 0.5), 0.5)
        self.assertAlmostEqual(f.update(1.0, 0.0, 1.0, 0.5), 0.75)

    def test_binary_mode_has_hysteresis(self):
        f = CurlFilter()
        out = [f.update(p, 0.0, 1.0, 0.0, "binary") for p in (0.1, 0.5, 0.59, 0.61, 0.5, 0.41, 0.39, 0.5)]
        self.assertEqual(out, [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0])

    def test_finger_targets(self):
        self.assertEqual(finger_targets(0.0)["index"], {"flex": -30.0, "side": 0.0})
        self.assertEqual(finger_targets(1.0)["thumb"], {"flex": 85.0, "side": 0.0})
        self.assertEqual(finger_targets(0.5)["ring"], {"flex": 27.5, "side": 0.0})
        self.assertEqual(finger_targets(1.0, scale=0.5)["middle"], {"flex": 27.5, "side": 0.0})
        only = finger_targets(1.0, enabled={"thumb": False, "index": True, "middle": False, "ring": False})
        self.assertEqual(only["index"]["flex"], 85.0)
        self.assertEqual(only["thumb"]["flex"], -30.0)
        self.assertEqual(finger_targets(7.0), finger_targets(1.0))
        self.assertEqual(finger_targets(-3.0), finger_targets(0.0))

    def test_targets_never_leave_panel_limits(self):
        wild = {name: (400.0, 400.0) for name in BUILTIN_FIST}
        for curl in (0.0, 0.3, 0.7, 1.0):
            for target in finger_targets(curl, fist_pose=wild).values():
                self.assertTrue(-30.0 <= target["flex"] <= 90.0)
                self.assertTrue(abs(target["flex"]) + abs(target["side"]) <= 90.0 + 1e-6)
        self.assertEqual(clamp_finger(90.0, 30.0), (90.0, 0.0))
        self.assertEqual(clamp_finger(-50.0, -60.0), (-30.0, -40.0))

    def test_stability_window(self):
        w = StabilityWindow(seconds=2.0, min_samples=8, min_span_s=1.2)
        for i in range(5):
            w.add(i * 0.1, raw(OPEN_VEC))
        self.assertEqual(w.summary(0.5)["issue"], "collecting")
        for i in range(5, 16):
            w.add(i * 0.1, raw(OPEN_VEC))
        s = w.summary(1.5)
        self.assertTrue(s["stable"])
        self.assertLess(angle_deg(s["mean"], OPEN_VEC), 0.5)
        w.add(1.6, raw(FIST_VEC))
        self.assertEqual(w.summary(1.6)["issue"], "moving")
        self.assertEqual(w.summary(9.0)["issue"], "collecting")

    def test_url_normalization(self):
        self.assertEqual(normalize_url("192.168.1.50"), "http://192.168.1.50:8765")
        self.assertEqual(normalize_url(" 192.168.1.50:9000/ "), "http://192.168.1.50:9000")
        self.assertEqual(normalize_url("http://pi.local:8765"), "http://pi.local:8765")
        for bad in ("", "https://192.168.1.50", "http://192.168.1.50/x", "http://u@h:1", "http://h:99999", None):
            with self.assertRaises(ValueError):
                normalize_url(bad)

    def test_saved_options_are_sanitized(self):
        clean = clean_options({"speed": "warp", "smoothing": 5, "deadband": 0.1, "fingers": {"thumb": False, "x": 1},
                               "long_press_start": "yes", "hand_url": "ftp://x", "mode": "binary"})
        self.assertEqual(clean["speed"], "normal")
        self.assertEqual(clean["smoothing"], DEFAULTS["smoothing"])
        self.assertEqual(clean["deadband"], 0.1)
        self.assertEqual(clean["fingers"], {"thumb": False, "index": True, "middle": True, "ring": True})
        self.assertFalse(clean["long_press_start"])
        self.assertEqual(clean["hand_url"], "")
        self.assertEqual(clean["mode"], "binary")
        self.assertEqual(clean_options("junk"), clean_options({}))

    def test_packet_checksum(self):
        frame = pack(0xA1, b"\x03")
        self.assertEqual(len(frame), 16)
        self.assertEqual(sum(frame[:15]) & 255, frame[15])
        with self.assertRaises(ValueError):
            pack(1, b"x" * 15)


class EngineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.panel = FakePanel()
        self.url = await self.panel.start()
        self.fake = FakeRingTransport()
        self.engine = Engine(self.folder, ring_transport_factory=lambda: self.fake, check_studio=False)
        self.engine.window = StabilityWindow(seconds=0.8, min_samples=4, min_span_s=0.3)
        self.engine.ring.accel_interval = 0.02
        await self.engine.start()

    async def asyncTearDown(self):
        await self.engine.close()
        await self.panel.close()
        self.tmp.cleanup()

    async def until(self, condition, timeout=4.0, message="條件沒有在時間內成立"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail(message)

    async def watch(self):
        """模擬視窗開著：持續取狀態，戒指才會持續讀三軸。"""
        self.engine.snapshot()
        self.watcher = asyncio.ensure_future(self._watch())
        self.addAsyncCleanup(self._unwatch)

    async def _watch(self):
        while True:
            self.engine.snapshot()
            await asyncio.sleep(0.2)

    async def _unwatch(self):
        self.watcher.cancel()
        await asyncio.gather(self.watcher, return_exceptions=True)

    async def connect_all(self):
        await self.watch()
        await self.engine.ring_connect()
        await self.until(lambda: self.engine.ring.state["connected"], message="戒指沒有連上")
        await self.engine.hand_link(self.url)
        await self.until(lambda: self.engine.hand.reachable, message="手掌面板沒有連上")
        await self.until(lambda: self.engine.hand.poses, message="沒有讀到姿勢清單")

    async def calibrate(self):
        self.engine.cal_start()
        self.fake.curl = 0.0
        await self.until(lambda: self.engine.last_stable is not None, message="張開沒有停穩")
        self.engine.cal_capture()
        self.fake.curl = 1.0
        await asyncio.sleep(0.15)
        await self.until(lambda: self.engine.last_stable is not None, message="握拳沒有停穩")
        self.engine.cal_capture()
        self.assertEqual(self.engine.cal["step"], "review")
        self.engine.cal_save()

    async def follow(self):
        await self.connect_all()
        await self.calibrate()
        self.fake.curl = 0.0
        await self.until(lambda: self.engine.arm_problem() is None, message=str(self.engine.arm_problem()))
        await self.engine.arm()
        self.assertEqual(self.engine.phase, "following")

    def flex(self, finger="index"):
        return self.panel.targets.get(finger, {}).get("flex")

    # ---------------------------------------------------------------- 啟動前
    async def test_nothing_moves_until_started(self):
        await self.connect_all()
        await self.calibrate()
        self.fake.curl = 1.0
        await asyncio.sleep(0.6)
        self.assertEqual(self.panel.posts, [])
        self.assertFalse(self.panel.torque)
        self.assertEqual(self.engine.phase, "idle")
        self.assertGreater(self.panel.beats, 0)

    async def test_start_is_refused_until_everything_is_ready(self):
        with self.assertRaisesRegex(ValueError, "戒指"):
            await self.engine.arm()
        await self.watch()
        await self.engine.ring_connect()
        await self.until(lambda: self.engine.ring.state["connected"])
        with self.assertRaisesRegex(ValueError, "校正"):
            await self.engine.arm()
        await self.calibrate()
        with self.assertRaisesRegex(ValueError, "手掌"):
            await self.engine.arm()
        self.assertEqual(self.panel.posts, [])

    async def test_start_is_refused_on_panel_problems(self):
        await self.connect_all()
        await self.calibrate()
        await self.until(lambda: self.engine.arm_problem() is None)
        for setup, pattern in (
            (lambda: setattr(self.panel, "fault", {"reason_code": "LAG_EXCEEDED", "detail": "x"}), "LAG_EXCEEDED"),
            (lambda: (setattr(self.panel, "fault", None), setattr(self.panel, "bus", "unavailable")), "匯流排"),
            (lambda: (setattr(self.panel, "bus", "attached"), setattr(self.panel, "servo_ok", False)), "沒有回應"),
            (lambda: (setattr(self.panel, "servo_ok", True), setattr(self.panel, "torque", True),
                      setattr(self.panel, "controller", "someoneelse")), "另一個頁面"),
        ):
            setup()
            await asyncio.sleep(0.6)
            with self.assertRaisesRegex(ValueError, pattern):
                await self.engine.arm()
        self.assertEqual(self.panel.calls("enable"), [])
        self.assertEqual(self.engine.phase, "idle")

    async def test_start_needs_a_valid_fresh_reading(self):
        await self.connect_all()
        await self.calibrate()
        self.fake.twist = 60.0
        await self.until(lambda: self.engine.curl.get("issue") == "off_axis")
        with self.assertRaisesRegex(ValueError, "方向"):
            await self.engine.arm()
        self.fake.twist = 0.0
        self.fake.magnitude = 1.6
        await self.until(lambda: self.engine.curl.get("issue") == "moving")
        with self.assertRaisesRegex(ValueError, "晃動"):
            await self.engine.arm()
        self.fake.magnitude = 1.0
        self.fake.silent = True
        await asyncio.sleep(1.3)
        with self.assertRaisesRegex(ValueError, "三軸"):
            await self.engine.arm()
        self.assertEqual(self.panel.calls("enable"), [])

    # ---------------------------------------------------------------- 校正
    async def test_calibration_is_saved_and_reloaded(self):
        await self.connect_all()
        await self.calibrate()
        saved = json.loads((self.folder / "calibration.json").read_text())
        self.assertEqual(saved["version"], 1)
        self.assertLess(angle_deg(saved["open"], OPEN_VEC), 1.0)
        self.assertLess(angle_deg(saved["fist"], FIST_VEC), 1.0)
        again = Engine(self.folder, ring_transport_factory=lambda: self.fake, check_studio=False)
        self.assertIsNotNone(again.model)
        self.assertAlmostEqual(again.model.span, saved["span_deg"], places=0)

    async def test_calibration_rejects_unsteady_or_identical_poses(self):
        await self.connect_all()
        self.engine.cal_start()
        with self.assertRaisesRegex(ValueError, "停穩"):
            self.engine.cal_capture()
        await self.until(lambda: self.engine.last_stable is not None)
        self.engine.cal_capture()
        await self.until(lambda: self.engine.last_stable is not None)
        with self.assertRaisesRegex(ValueError, "只差"):
            self.engine.cal_capture()               # 握拳和張開是同一個姿勢
        self.assertEqual(self.engine.cal["step"], "fist")
        self.assertIsNone(self.engine.cal["fist"])
        with self.assertRaises(ValueError):
            self.engine.cal_save()
        self.engine.cal_cancel()
        self.assertIsNone(self.engine.model)
        self.assertFalse((self.folder / "calibration.json").exists())

    async def test_ring_tap_confirms_calibration_steps(self):
        await self.connect_all()
        self.engine.cal_start()
        await self.until(lambda: self.engine.last_stable is not None)
        self.fake.gesture(3)
        await self.until(lambda: self.engine.cal["open"] is not None)
        self.fake.curl = 1.0
        await asyncio.sleep(0.7)                    # 也超過觸控的防重複間隔
        await self.until(lambda: self.engine.last_stable is not None)
        self.fake.gesture(3)
        await self.until(lambda: self.engine.cal["step"] == "review")
        await asyncio.sleep(0.7)
        self.fake.gesture(3)
        await self.until(lambda: self.engine.model is not None)
        self.assertFalse(self.engine.cal["active"])

    # ---------------------------------------------------------------- 跟隨
    async def test_fist_follows_and_opens_again(self):
        await self.follow()
        enable = self.panel.calls("enable")
        self.assertEqual(len(enable), 1)
        self.assertEqual(enable[0]["speed"], "normal")
        await self.until(lambda: self.flex() == -30.0, message="啟動後沒有送出張開的目標")
        self.fake.curl = 1.0
        await self.until(lambda: self.flex() == 85.0, message="握拳沒有傳到手掌")
        self.assertEqual(self.panel.targets["thumb"], {"flex": 85.0, "side": 0.0})
        self.assertEqual(set(self.panel.targets), {"thumb", "index", "middle", "ring"})
        self.fake.curl = 0.5
        await self.until(lambda: self.flex() is not None and abs(self.flex() - 27.5) < 6)
        self.fake.curl = 0.0
        await self.until(lambda: self.flex() == -30.0, message="張開沒有傳到手掌")
        await self.engine.stop()
        self.assertFalse(self.panel.torque)
        self.assertEqual(self.engine.phase, "idle")
        count = len(self.panel.calls("fingers"))
        self.fake.curl = 1.0
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.panel.calls("fingers")), count, "停止後還在送目標")

    async def test_small_jitter_is_not_sent(self):
        await self.follow()
        self.fake.curl = 0.5
        await self.until(lambda: self.flex() is not None and abs(self.flex() - 27.5) < 6)
        await asyncio.sleep(0.5)
        count = len(self.panel.calls("fingers"))
        for curl in (0.505, 0.495, 0.51, 0.5):
            self.fake.curl = curl
            await asyncio.sleep(0.2)
        self.assertEqual(len(self.panel.calls("fingers")), count)

    async def test_sends_are_rate_limited(self):
        await self.follow()
        began = time.monotonic()
        for i in range(40):
            self.fake.curl = (i % 2) * 1.0
            await asyncio.sleep(0.03)
        elapsed = time.monotonic() - began
        self.assertLessEqual(len(self.panel.calls("fingers")), elapsed / 0.12 + 3)

    async def test_invalid_readings_hold_the_last_target(self):
        await self.follow()
        self.fake.curl = 1.0
        await self.until(lambda: self.flex() == 85.0)
        count = len(self.panel.calls("fingers"))
        self.fake.twist = 70.0                      # 轉手腕：離開校正的弧線
        self.fake.curl = 0.0
        await self.until(lambda: self.engine.curl.get("issue") == "off_axis")
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.panel.calls("fingers")), count)
        self.assertEqual(self.flex(), 85.0)
        self.assertEqual(self.engine.phase, "following")
        self.fake.twist = 0.0
        await self.until(lambda: self.flex() == -30.0)

    async def test_selected_fingers_pose_and_scale(self):
        await self.connect_all()
        await self.calibrate()
        self.engine.set_options({"fist_pose": "stone", "fingers": {"thumb": False}, "fist_scale": 0.5,
                                 "speed": "slow"})
        self.fake.curl = 1.0
        await self.until(lambda: self.engine.arm_problem() is None)
        await self.engine.arm()
        self.assertEqual(self.panel.calls("enable")[0]["speed"], "slow")
        await self.until(lambda: self.flex() is not None)
        await self.until(lambda: self.flex() == 30.0)
        self.assertEqual(self.panel.targets["ring"]["flex"], 29.5)
        self.assertEqual(self.panel.targets["thumb"], {"flex": -30.0, "side": 0.0})
        with self.assertRaisesRegex(ValueError, "跟隨中"):
            self.engine.set_options({"speed": "fast"})

    async def test_unknown_or_unplayable_fist_pose_blocks_start(self):
        await self.connect_all()
        await self.calibrate()
        for name in ("gone", "old"):
            self.engine.set_options({"fist_pose": name})
            await asyncio.sleep(0.2)
            with self.assertRaisesRegex(ValueError, name):
                await self.engine.arm()
        self.assertEqual(self.panel.calls("enable"), [])

    async def test_binary_mode_sends_only_the_two_ends(self):
        await self.connect_all()
        await self.calibrate()
        self.engine.set_options({"mode": "binary", "smoothing": 0.0})
        self.fake.curl = 0.0
        await self.until(lambda: self.engine.arm_problem() is None)
        await self.engine.arm()
        for curl in (0.2, 0.45, 0.7, 0.55, 0.3, 0.0):
            self.fake.curl = curl
            await asyncio.sleep(0.3)
        sent = {body["fingers"]["index"]["flex"] for body in self.panel.calls("fingers")}
        self.assertEqual(sent, {-30.0, 85.0})

    # ---------------------------------------------------------------- 中止
    async def test_ring_drop_turns_torque_off(self):
        await self.follow()
        self.fake.drop()
        await self.until(lambda: self.engine.phase == "idle" and not self.panel.torque, message="戒指斷線後沒有關扭力")
        self.assertEqual(len(self.panel.calls("stop")), 1)
        self.assertIn("戒指", self.engine.message)
        self.assertFalse(self.engine.ring.state["connected"])

    async def test_panel_release_ends_following_without_restart(self):
        await self.follow()
        await asyncio.sleep(1.1)
        self.panel.release("hold_timeout")
        await self.until(lambda: self.engine.phase == "idle")
        self.assertIn("閒置", self.engine.message)
        self.fake.curl = 1.0
        await asyncio.sleep(0.8)
        self.assertEqual(len(self.panel.calls("enable")), 1, "面板放鬆後 APP 自己又啟用了扭力")
        self.assertFalse(self.panel.torque)

    async def test_panel_fault_ends_following_and_needs_clearing(self):
        await self.follow()
        await asyncio.sleep(1.1)
        self.panel.release("fault", {"reason_code": "LAG_EXCEEDED", "detail": "ID 7 落後"})
        await self.until(lambda: self.engine.phase == "idle")
        self.assertIn("LAG_EXCEEDED", self.engine.message)
        with self.assertRaisesRegex(ValueError, "LAG_EXCEEDED"):
            await self.engine.arm()
        await self.engine.hand_clear_fault()
        self.fake.curl = 0.0
        await self.until(lambda: self.engine.arm_problem() is None)
        await self.engine.arm()
        self.assertEqual(self.engine.phase, "following")

    async def test_unreachable_panel_ends_following(self):
        await self.follow()
        await asyncio.sleep(1.1)
        self.panel.fail = True
        await self.until(lambda: self.engine.phase == "idle", timeout=6)
        self.assertIn("連不到", self.engine.message)
        with self.assertRaisesRegex(ValueError, "連不到"):
            await self.engine.arm()

    async def test_stop_is_always_forwarded(self):
        await self.connect_all()
        self.panel.torque, self.panel.controller = True, "someoneelse"
        self.assertTrue(await self.engine.stop())
        self.assertFalse(self.panel.torque)
        self.assertEqual(len(self.panel.calls("stop")), 1)

    async def test_close_while_following_turns_torque_off(self):
        await self.follow()
        await self.engine.close()
        self.assertFalse(self.panel.torque)
        self.assertEqual(len(self.panel.calls("stop")), 1)

    async def test_starting_calibration_or_disconnecting_requires_stop_first(self):
        await self.follow()
        with self.assertRaisesRegex(ValueError, "停止"):
            self.engine.cal_start()
        await self.engine.ring_disconnect()
        self.assertFalse(self.panel.torque)
        self.assertEqual(self.engine.phase, "idle")

    # ---------------------------------------------------------------- 戒指觸控
    async def test_long_press_stops_but_does_not_start_by_default(self):
        await self.follow()
        self.fake.gesture(4)
        await self.until(lambda: self.engine.phase == "idle" and not self.panel.torque)
        await asyncio.sleep(0.7)
        self.fake.gesture(4)
        await asyncio.sleep(0.6)
        self.assertEqual(self.engine.phase, "idle")
        self.assertEqual(len(self.panel.calls("enable")), 1)

    async def test_long_press_starts_only_when_allowed(self):
        await self.connect_all()
        await self.calibrate()
        self.engine.set_options({"long_press_start": True})
        await asyncio.sleep(0.7)
        self.fake.gesture(4)
        await self.until(lambda: self.engine.phase == "following")
        self.assertIn("戒指長按", self.engine.message)

    async def test_repeated_touch_events_are_debounced(self):
        await self.follow()
        self.engine.set_options({"long_press_start": True})
        await asyncio.sleep(0.7)
        for _ in range(3):
            self.fake.gesture(4)
        await asyncio.sleep(0.5)
        self.assertEqual(self.engine.phase, "idle")
        self.assertEqual(len(self.panel.calls("enable")), 1)

    # ---------------------------------------------------------------- 其他
    async def test_options_are_validated_and_persisted(self):
        for bad in ({"speed": "warp"}, {"smoothing": 2}, {"unknown": 1}, {"fingers": {"toe": True}},
                    {"fingers": {"thumb": False, "index": False, "middle": False, "ring": False}},
                    {"open_margin": 0.4, "fist_margin": 0.55}, {"deadband": True}, {"hand_url": "ftp://x"}, {}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.engine.set_options(bad)
        self.engine.set_options({"speed": "fast", "smoothing": 0.2, "fingers": {"ring": False}})
        saved = json.loads((self.folder / "preferences.json").read_text())
        self.assertEqual(saved["speed"], "fast")
        again = Engine(self.folder, check_studio=False)
        self.addCleanup(lambda: again.log_file and again.log_file.close())
        self.assertEqual(again.options["smoothing"], 0.2)
        self.assertFalse(again.options["fingers"]["ring"])
        self.assertFalse(again.options["long_press_start"])

    async def test_ring_settings_are_restored_on_disconnect(self):
        await self.watch()
        await self.engine.ring_connect()
        await self.until(lambda: self.engine.ring.state["touch"])
        await self.until(lambda: self.engine.ring.state["raw"] is not None)
        self.assertEqual(self.engine.ring.state["battery"], 77)
        await self.engine.ring_disconnect()
        frames = self.fake.writes
        self.assertIn(pack(0xA1, b"\x00"), frames)
        self.assertEqual(frames[-1], pack(0x3B, b"\x01\x00"))
        self.assertEqual(self.fake.touch[3:5], [0, 0])
        self.assertFalse(self.engine.ring.state["connected"])

    async def test_sampling_stops_when_nobody_is_watching(self):
        await self.engine.ring_connect()
        await self.until(lambda: self.engine.ring.state["connected"])
        await asyncio.sleep(0.6)
        polls = self.fake.writes.count(pack(0xA1, b"\x03"))
        await asyncio.sleep(0.5)
        self.assertEqual(self.fake.writes.count(pack(0xA1, b"\x03")), polls)

    async def test_site_default_address_is_used_only_when_unset(self):
        first = Engine(self.folder / "a", site={"hand_url": "10.0.0.9"}, check_studio=False)
        self.assertEqual(first.options["hand_url"], "http://10.0.0.9:8765")
        first.set_options({"hand_url": "10.0.0.7:9000"})
        first.log_file.close()
        second = Engine(self.folder / "a", site={"hand_url": "10.0.0.9"}, check_studio=False)
        self.assertEqual(second.options["hand_url"], "http://10.0.0.7:9000")


class ShutdownCase(unittest.IsolatedAsyncioTestCase):
    async def test_close_finishes_while_samples_keep_arriving(self):
        """回歸：結束時 sender 剛好被新取樣叫醒，取消會被 asyncio.wait_for 吃掉，close() 就永遠不回來。"""
        for _ in range(25):
            with tempfile.TemporaryDirectory() as tmp:
                engine = Engine(tmp, check_studio=False)
                await engine.start()

                async def hammer():
                    while True:
                        engine.kick.set()
                        await asyncio.sleep(0)
                task = asyncio.ensure_future(hammer())
                await asyncio.sleep(0.01)
                closer = asyncio.ensure_future(engine.close())
                done, _ = await asyncio.wait({closer}, timeout=6)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if not done:
                    closer.cancel()
                    self.fail("engine.close() 沒有在時間內結束")
                self.assertTrue(engine.worker.done())

    async def test_unlink_finishes_while_the_panel_is_unresponsive(self):
        panel = FakePanel()
        url = await panel.start()
        panel.fail = True
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(tmp, check_studio=False)
            await engine.start()
            await engine.hand_link(url)
            await asyncio.sleep(0.4)
            closer = asyncio.ensure_future(engine.close())
            done, _ = await asyncio.wait({closer}, timeout=6)
            self.assertTrue(done, "engine.close() 沒有在時間內結束")
            self.assertIsNone(engine.hand.task)
            self.assertFalse(engine.hand.state["linked"])
        await panel.close()


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = 18790
        self.app = await app_server.create_app(self.tmp.name, self.port, fake_ring=True, self_test=True)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.session = ClientSession()
        self.base = "http://127.0.0.1:%d" % self.port

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_state_and_menu_status_start_safe(self):
        async with self.session.get(self.base + "/api/state") as r:
            self.assertEqual(r.status, 200)
            state = await r.json()
        self.assertEqual(state["follow"]["phase"], "idle")
        self.assertFalse(state["ring"]["connected"])
        self.assertFalse(state["hand"]["linked"])
        self.assertIn("options", state)
        async with self.session.get(self.base + "/api/menu-status") as r:
            menu = await r.json()
        self.assertEqual(menu, {"ring": False, "hand": False, "calibrated": False, "following": False,
                                "busy": False, "mode": "disconnected", "label": "戒指未連線"})

    async def test_page_and_assets_are_served(self):
        async with self.session.get(self.base + "/") as r:
            text = await r.text()
            self.assertEqual(r.status, 200)
        self.assertIn("app.js?v=", text)
        for name in ("/app.js", "/app.css"):
            async with self.session.get(self.base + name) as r:
                self.assertEqual(r.status, 200)

    async def test_foreign_origin_host_and_form_posts_are_refused(self):
        async with self.session.post(self.base + "/api/follow/stop", json={}, headers={"Origin": "http://evil.example"}) as r:
            self.assertEqual(r.status, 403)
        async with self.session.get(self.base + "/api/state", headers={"Host": "evil.example"}) as r:
            self.assertEqual(r.status, 403)
        async with self.session.post(self.base + "/api/follow/stop", data={"a": "b"}) as r:
            self.assertEqual(r.status, 415)

    async def test_errors_are_reported_as_400(self):
        async with self.session.post(self.base + "/api/follow/start", json={}) as r:
            self.assertEqual(r.status, 400)
            self.assertIn("戒指", (await r.json())["error"])
        async with self.session.post(self.base + "/api/options", json={"speed": "warp"}) as r:
            self.assertEqual(r.status, 400)
        async with self.session.post(self.base + "/api/hand/link", json={"url": "nonsense://"}) as r:
            self.assertEqual(r.status, 400)

    async def test_shutdown_endpoint_closes_cleanly(self):
        async with self.session.post(self.base + "/api/shutdown", json={}) as r:
            self.assertEqual(r.status, 200)
        self.assertTrue(self.app[app_server.SHUTDOWN].is_set())


if __name__ == "__main__":
    unittest.main()
