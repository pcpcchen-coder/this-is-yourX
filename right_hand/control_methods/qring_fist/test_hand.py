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
from engine import DEFAULTS, POLL_KEYS, Engine, clean_options
from follow import (BUILTIN_FIST, CurlFilter, CurlModel, PalmModel, SwayFilter, StabilityWindow, angle_deg, clamp_finger, finger_targets, direct_targets,
                    unit)
from hand import normalize_url
from ring import POLL_DEFAULTS, POLL_PRESETS, QUIET_PERIOD_S, SnapshotStats, pack
from sim import FIST_VEC, OPEN_VEC, FakeRingTransport, slerp


def raw(vec, magnitude=1.0):
    return [int(round(x * 8192 * magnitude)) for x in unit(vec)]


def palm_vector(model, curl, side):
    theta, latitude = math.radians(model.span * curl), math.radians(side)
    return tuple(math.cos(latitude) * (math.cos(theta) * model.open[i] + math.sin(theta) * model.along[i]) +
                 math.sin(latitude) * model.normal[i] for i in range(3))


class PalmCase(unittest.TestCase):
    def setUp(self):
        self.arc = CurlModel(OPEN_VEC, FIST_VEC)
        self.model = PalmModel(OPEN_VEC, FIST_VEC, palm_vector(self.arc, 0, -25), palm_vector(self.arc, 0, 35))

    def test_both_inputs_are_independent_across_calibrated_coordinates(self):
        for curl in (0, .2, .5, .8, 1):
            for side in (-25, -12.5, 0, 17.5, 35):
                m = self.model.measure(raw(palm_vector(self.arc, curl, side)))
                self.assertTrue(m["valid"], m)
                self.assertAlmostEqual(m["progress"], curl, delta=.001)
                expected = side / (25 if side < 0 else 35)
                self.assertAlmostEqual(m["sway_progress"], expected, delta=.001)

    def test_left_and_right_follow_user_calibration_labels(self):
        model = PalmModel(OPEN_VEC, FIST_VEC, palm_vector(self.arc, 0, 25), palm_vector(self.arc, 0, -35))
        self.assertAlmostEqual(model.measure(raw(palm_vector(self.arc, .3, 25)))["sway_progress"], -1, delta=.001)
        self.assertAlmostEqual(model.measure(raw(palm_vector(self.arc, .3, -35)))["sway_progress"], 1, delta=.001)

    def test_unobservable_collinear_and_same_side_poses_rejected(self):
        for left, right in ((palm_vector(self.arc, 0, 5), palm_vector(self.arc, 0, 35)),
                            (palm_vector(self.arc, .5, 0), palm_vector(self.arc, 0, 35)),
                            (palm_vector(self.arc, 0, 25), palm_vector(self.arc, 0, 35)),
                            (palm_vector(self.arc, .4, -25), palm_vector(self.arc, 0, 35)),
                            (palm_vector(self.arc, 0, -80), palm_vector(self.arc, 0, 35))):
            with self.assertRaises(ValueError):
                PalmModel(OPEN_VEC, FIST_VEC, left, right)

    def test_invalid_magnitude_range_and_poles_are_rejected(self):
        for vector in ((0, 0, 0), (True, 0, 8192), (float("nan"), 0, 8192), (1, 2),
                       raw(palm_vector(self.arc, .5, 0), magnitude=1.3),
                       raw(palm_vector(self.arc, .5, 80)), raw(palm_vector(self.arc, 2, 0))):
            self.assertFalse(self.model.measure(vector)["valid"])

    def test_neutral_and_smoothing(self):
        f = SwayFilter()
        self.assertEqual(f.update(-1, .1, 0), -1)
        self.assertEqual(f.update(.08, .1, .9), 0)
        self.assertAlmostEqual(f.update(.55, .1, 0), .5)
        self.assertAlmostEqual(f.update(-1, .1, .5), -.25)
        f.reset()
        self.assertIsNone(f.output)

    def test_direct_targets_combined_axes_and_motor_limits(self):
        self.assertEqual(direct_targets(.5, .5)["index"], {"flex": 27.5, "side": 20})
        self.assertEqual(direct_targets(.5, .5)["thumb"], {"flex": 27.5, "side": -20})
        self.assertEqual(direct_targets(1, 1)["index"], {"flex": 85, "side": 5})
        self.assertEqual(direct_targets(0, -1)["index"], {"flex": -30, "side": -40})
        for curl in (-1, 0, .1, .5, .9, 1, 2):
            for sway in (-2, -1, -.5, 0, .5, 1, 2):
                for t in direct_targets(curl, sway).values():
                    self.assertTrue(-30 <= t["flex"] <= 85)
                    self.assertTrue(-40 <= t["side"] <= 40)
                    self.assertLessEqual(abs(t["flex"]) + abs(t["side"]), 90)
        for x in (True, None, "1", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                direct_targets(x, 0)
            with self.assertRaises(ValueError):
                direct_targets(0, x)


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
        self.assertNotIn("mode", clean)
        self.assertEqual(clean_options("junk"), clean_options({}))

    def test_snapshot_statistics(self):
        stats = SnapshotStats(10.0)
        for i in range(10):                             # 0.2 秒一筆，每兩筆重複一次
            t = 10.0 + 0.2 * i
            stats.record_send(t)
            stats.record_sample([i // 2, 0, 8192], t + 0.05)
        stats.record_wait(0.05, False)
        stats.record_wait(0.3, True)
        stats.no_wait += 3
        p = stats.snapshot(12.0)
        self.assertEqual(p["window_s"], 2.0)
        self.assertEqual((p["sent"], p["received"], p["changed"]), (10, 10, 4))
        self.assertAlmostEqual(p["rx_hz"], 5.0, delta=0.6)
        self.assertEqual((p["waited"], p["timeouts"], p["no_wait"]), (2, 1, 3))
        self.assertEqual(p["timeout_percent"], 50.0)
        self.assertEqual(p["mean_wait_ms"], 175.0)
        self.assertEqual(p["max_gap_ms"], 200.0)
        self.assertAlmostEqual(p["sample_age"], 0.15, places=2)
        fresh = SnapshotStats(5.0).snapshot(5.5)        # 剛重置：用比較短的窗口，沒有取樣就說沒有
        self.assertEqual(fresh["window_s"], 0.5)
        self.assertIsNone(fresh["sample_age"])
        self.assertIsNone(fresh["mean_wait_ms"])

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
        self.engine.ring.poll = dict(POLL_DEFAULTS, accel_period=0.02)     # 測試跑快一點；不存進設定
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

    async def calibrate(self, sway=True):
        self.engine.cal_start(sway=sway)
        poses = [("open", 0, 0), ("fist", 1, 0)]
        if sway:
            poses += [("left", 0, -25), ("right", 0, 35)]
        for pose, curl, side in poses:
            self.fake.curl, self.fake.twist = curl, side
            await self.until(lambda: self.engine.last_stable is not None, message=pose + "沒有停穩")
            self.engine.cal_capture(pose)
        self.assertEqual(self.engine.cal["step"], "review")
        self.engine.cal_save()
        self.fake.twist = 0

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
    async def test_four_pose_calibration_taps_save_and_reload_without_motion(self):
        await self.connect_all()
        self.engine.cal_start(sway=True)
        for i, (pose, curl, side) in enumerate((("open", 0, 0), ("fist", 1, 0), ("left", 0, -25), ("right", 0, 35))):
            self.fake.curl, self.fake.twist = curl, side
            await self.until(lambda: self.engine.last_stable is not None)
            self.assertEqual(self.engine.cal["step"], pose)
            self.fake.gesture(3)
            await self.until(lambda: self.engine.cal[pose] is not None)
            if i < 3:
                self.assertIsNone(self.engine.cal["model"])
        self.assertEqual(self.engine.cal["step"], "review")
        self.assertIsInstance(self.engine.cal["model"], PalmModel)
        self.fake.curl, self.fake.twist = .5, -12.5
        await self.until(lambda: self.engine.sway.get("valid") and abs(self.engine.curl.get("progress", 0) - .5) < .01)
        self.assertAlmostEqual(self.engine.curl["progress"], .5, delta=.01)
        self.assertAlmostEqual(self.engine.sway["progress"], -.5, delta=.01)
        await asyncio.sleep(.7)
        self.fake.gesture(3)
        await self.until(lambda: self.engine.model is not None)
        saved = json.loads(self.engine.calibration_path.read_text())
        self.assertEqual(saved["version"], 2)
        self.assertIn("left", saved)
        again = Engine(self.folder, check_studio=False)
        self.assertIsInstance(again.model, PalmModel)
        self.assertEqual(self.panel.posts, [])
        self.assertFalse(self.panel.torque)

    async def test_sway_retake_and_cancel_preserve_saved_legacy_calibration(self):
        await self.connect_all()
        await self.calibrate(sway=False)
        before = self.engine.calibration_path.read_bytes()
        self.engine.cal_start(sway=True)
        # Deterministic stable inputs exercise the sequence, independent of BLE timings.
        model = CurlModel(OPEN_VEC, FIST_VEC)
        for pose, vec in (("open", model.open), ("fist", model.fist), ("left", palm_vector(model, 0, -25)),
                          ("right", palm_vector(model, 0, 30))):
            self.engine.last_stable = (time.monotonic(), vec, 0)
            self.engine.cal_capture(pose)
        self.engine.cal_retake("open")
        self.assertIsNone(self.engine.cal["left"])
        self.assertIsNone(self.engine.cal["right"])
        with self.assertRaises(ValueError):
            self.engine.cal_save()
        self.engine.cal_cancel()
        self.assertEqual(self.engine.calibration_path.read_bytes(), before)
        self.assertNotIsInstance(self.engine.model, PalmModel)
        self.assertIsNone(self.engine.snapshot()["follow"]["sway"]["value"])
        self.assertEqual(self.panel.posts, [])

    async def test_invalid_sway_calibration_can_be_corrected(self):
        await self.connect_all()
        self.engine.cal_start(sway=True)
        model = CurlModel(OPEN_VEC, FIST_VEC)
        for pose, vec in (("open", model.open), ("fist", model.fist), ("left", palm_vector(model, 0, -25))):
            self.engine.last_stable = (time.monotonic(), vec, 0)
            self.engine.cal_capture(pose)
        self.engine.last_stable = (time.monotonic(), palm_vector(model, 0, -30), 0)
        with self.assertRaisesRegex(ValueError, "同一側"):
            self.engine.cal_capture("right")
        self.assertIsNone(self.engine.last_stable)
        self.assertIsNone(self.engine.cal["right"])
        self.assertEqual(self.engine.cal["step"], "right")
        self.engine.last_stable = (time.monotonic(), palm_vector(model, 0, 30), 0)
        self.engine.cal_capture("right")
        self.assertEqual(self.engine.cal["step"], "review")

    async def test_sway_input_maps_servo_sides_but_does_not_enable_motion(self):
        await self.connect_all()
        model = CurlModel(OPEN_VEC, FIST_VEC)
        self.engine.model = PalmModel(OPEN_VEC, FIST_VEC, palm_vector(model, 0, -25), palm_vector(model, 0, 35))
        self.engine.options.update(smoothing=0, sway_neutral=.1)
        for side in (-25, 0, 35):
            self.engine._on_accel(raw(palm_vector(model, .5, side)), time.monotonic())
            self.assertTrue(self.engine.sway["valid"])
            expected = 40 * self.engine.sway_filter.output
            self.assertAlmostEqual(self.engine.targets["index"]["side"], expected, delta=.1)
            self.assertAlmostEqual(self.engine.targets["thumb"]["side"], -expected, delta=.1)
            self.assertAlmostEqual(self.engine.curl["progress"], .5, delta=.01)
        self.engine._on_accel(raw(palm_vector(model, .5, 70)), time.monotonic())
        self.assertFalse(self.engine.sway["valid"])
        self.assertIsNone(self.engine.snapshot()["follow"]["sway"]["value"])
        self.engine.options["sway_neutral"] = .23
        self.engine.set_options({"sway_neutral": .23})
        again = Engine(self.folder, check_studio=False)
        self.assertEqual(again.options["sway_neutral"], .23)
        for value in (-1, .41, True, float("nan"), "0.2"):
            with self.assertRaises(ValueError):
                self.engine.set_options({"sway_neutral": value})
        self.assertEqual(self.panel.posts, [])

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
        await self.until(lambda: self.engine.curl.get("issue") == "sway_out_of_range")
        with self.assertRaisesRegex(ValueError, "側擺"):
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
        await self.calibrate(sway=False)
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
        await self.until(lambda: self.engine.curl.get("issue") == "sway_out_of_range")
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.panel.calls("fingers")), count)
        self.assertEqual(self.flex(), 85.0)
        self.assertEqual(self.engine.phase, "following")
        self.fake.twist = 0.0
        await self.until(lambda: self.flex() == -30.0)

    async def test_selected_fingers_use_direct_inputs_and_speed_lock(self):
        await self.connect_all()
        await self.calibrate()
        self.engine.set_options({"fingers": {"thumb": False}, "speed": "slow", "smoothing": 0})
        self.fake.curl, self.fake.twist = .5, 35
        await self.until(lambda: self.engine._inputs_fresh() and self.engine.sway_filter.output == 1)
        await self.engine.arm()
        await self.until(lambda: self.flex() == 27.5 and self.panel.targets["index"]["side"] == 40)
        self.assertEqual(self.panel.targets["thumb"], {"flex": -30, "side": 0})
        self.assertEqual(self.panel.calls("enable")[0]["speed"], "slow")
        with self.assertRaisesRegex(ValueError, "跟隨中"):
            self.engine.set_options({"speed": "fast"})

    async def test_removed_pose_preferences_cannot_affect_targets(self):
        old = {"fist_pose": "right_swing", "fist_scale": .2, "mode": "binary", "speed": "slow"}
        cleaned = clean_options(old)
        self.assertEqual(cleaned["speed"], "slow")
        for key in ("fist_pose", "fist_scale", "mode"):
            self.assertNotIn(key, cleaned)
            with self.assertRaises(ValueError):
                self.engine.set_options({key: old[key]})
        await self.connect_all()
        await self.calibrate()
        self.engine.hand.poses = []                # No saved poses are needed to start.
        await self.until(lambda: self.engine.arm_problem() is None)
        await self.engine.arm()
        self.assertEqual(len(self.panel.calls("enable")), 1)

    async def test_direct_follow_has_continuous_combined_inputs(self):
        await self.follow()
        self.engine.set_options({"smoothing": 0})
        for curl, side in ((.25, 35), (.5, 0), (.75, -25), (1, 35), (0, 0)):
            self.fake.curl, self.fake.twist = curl, side
            await self.until(lambda: abs(self.engine.curl.get("progress", -2) - curl) < .001 and self.engine._inputs_fresh())
            expected = direct_targets(self.engine.filter.output, self.engine.sway_filter.output)
            await self.until(lambda: self.panel.targets == expected, message=str(expected))
        self.assertEqual(self.panel.targets["index"], {"flex": -30, "side": 0})

    async def test_legacy_two_point_profile_blocks_direct_motion(self):
        await self.connect_all()
        await self.calibrate(sway=False)
        await self.until(lambda: self.engine.curl.get("valid"))
        with self.assertRaisesRegex(ValueError, "四姿勢"):
            await self.engine.arm()
        self.assertEqual(self.panel.calls("enable"), [])
        self.assertFalse(self.engine.menu_status()["calibrated"])
        self.assertEqual(self.engine.menu_status()["mode"], "uncalibrated")

    async def test_stale_dual_inputs_do_not_send_even_if_targets_change(self):
        await self.follow()
        await self.until(lambda: self.panel.targets)
        self.fake.silent = True
        await asyncio.sleep(1.2)
        count = len(self.panel.calls("fingers"))
        self.engine.targets = direct_targets(1, 1)
        self.engine.kick.set()
        await asyncio.sleep(.35)
        self.assertEqual(len(self.panel.calls("fingers")), count)
        self.assertFalse(self.engine._inputs_fresh())

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
                    {"open_margin": 0.4, "fist_margin": 0.55}, {"deadband": True}, {"hand_url": "ftp://x"},
                    {"poll_mode": "wait"}, {"accel_period": -0.1}, {"accel_timeout": 2.5},
                    {"accel_interval": True}, {"accel_period": "0.1"}, {"accel_timeout": float("nan")}, {}):
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

    async def rate(self, seconds):
        await self.watch()
        await self.engine.ring_connect()
        await self.until(lambda: self.engine.ring.state["raw"] is not None)
        self.engine.ring.reset_stats()
        await asyncio.sleep(seconds)
        return self.engine.ring.polling_snapshot()

    async def test_default_fixed_cadence_does_not_wait_for_the_ring(self):
        self.assertEqual({k: self.engine.options[k] for k in POLL_KEYS}, POLL_PRESETS["fixed10"])
        self.engine.set_options(dict(POLL_PRESETS["fixed10"]))
        self.fake.reply_delay = 0.19                    # 實機上一來一回大約這麼久
        p = await self.rate(2.2)
        self.assertGreater(p["rx_hz"], 8.0)
        self.assertLess(p["rx_hz"], 12.0)
        self.assertEqual(p["waited"], 0)
        self.assertGreater(p["no_wait"], 15)
        self.assertTrue(p["active"])
        self.assertEqual(p["label"], "輪詢中")

    async def test_wait_preset_keeps_the_request_then_wait_behaviour(self):
        self.engine.set_options(dict(POLL_PRESETS["wait"]))
        self.fake.reply_delay = 0.19
        p = await self.rate(2.2)
        self.assertLess(p["rx_hz"], 5.0)                # 一來一回 0.19 秒 + 停 0.06 秒 ≈ 4 Hz
        self.assertGreater(p["rx_hz"], 2.5)
        self.assertGreater(p["waited"], 5)
        self.assertEqual(p["timeouts"], 0)
        self.assertGreater(p["mean_wait_ms"], 150)

    async def test_period_and_pause_are_independent(self):
        self.fake.reply_delay = 0.02
        self.engine.set_options({"accel_period": 0.25, "accel_timeout": 0.3, "accel_interval": 0.0})
        p = await self.rate(2.0)                        # 回報很快，但節拍守住 0.25 秒
        self.assertGreater(p["tx_hz"], 3.0)
        self.assertLess(p["tx_hz"], 5.0)
        self.engine.set_options({"accel_period": 0.0, "accel_timeout": 0.3, "accel_interval": 0.2})
        self.assertEqual(self.engine.ring.stats.sent, 0, "套用新設定時統計要重置")
        await asyncio.sleep(2.0)
        p = self.engine.ring.polling_snapshot()        # 每輪 ≈ 0.02 等待 + 0.2 休息
        self.assertGreater(p["tx_hz"], 3.5)
        self.assertLess(p["tx_hz"], 5.5)
        self.assertEqual(self.engine.ring.poll["accel_interval"], 0.2)

    async def test_wait_limit_when_the_ring_does_not_answer(self):
        self.engine.set_options({"accel_period": 0.0, "accel_timeout": 0.1, "accel_interval": 0.0})
        await self.rate(0.3)
        self.fake.silent = True
        self.engine.ring.reset_stats()
        await asyncio.sleep(0.6)                        # 還沒到 QUIET_AFTER_S：每輪等滿 0.1 秒就放棄
        p = self.engine.ring.polling_snapshot()
        self.assertGreaterEqual(p["timeouts"], 4)
        self.assertEqual(p["timeouts"], p["waited"])
        self.assertAlmostEqual(p["mean_wait_ms"], 100, delta=25)
        await asyncio.sleep(1.0)
        self.assertTrue(self.engine.ring.backoff)
        self.assertIn("放慢", self.engine.ring.polling_snapshot()["label"])
        self.fake.silent = False
        await self.until(lambda: self.engine.ring.fresh(0.3), message="戒指恢復回報後沒有接上")
        await self.until(lambda: not self.engine.ring.backoff)

    async def test_fixed_cadence_slows_down_when_the_ring_stops_answering(self):
        self.engine.set_options({"accel_period": 0.05, "accel_timeout": 0.0, "accel_interval": 0.0})
        await self.rate(0.2)
        self.fake.silent = True
        await asyncio.sleep(1.3)                        # 超過 QUIET_AFTER_S
        request = pack(0xA1, b"\x03")
        before = self.fake.writes.count(request)
        await asyncio.sleep(2.0)
        sent = self.fake.writes.count(request) - before
        self.assertLessEqual(sent, 2.0 / QUIET_PERIOD_S + 2, "戒指不回時還在全速送要求")
        self.assertGreaterEqual(sent, 2, "戒指不回時完全不送了，恢復後接不上")
        self.fake.silent = False
        await self.until(lambda: self.engine.ring.fresh(0.3), message="戒指恢復回報後沒有接上")

    async def test_all_zero_settings_still_yield_and_write_one_at_a_time(self):
        self.engine.set_options(dict(POLL_PRESETS["fast"]))
        ticks = 0
        inflight = peak = 0
        original = self.fake.write

        async def counted(frame):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            try:
                await asyncio.sleep(0)
                return await original(frame)
            finally:
                inflight -= 1
        self.fake.write = counted

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1
        task = asyncio.ensure_future(ticker())
        try:
            p = await self.rate(0.5)
        finally:
            task.cancel()
        self.assertGreater(ticks, 25, "全部設 0 時把事件迴圈佔住了")
        self.assertGreater(p["sent"], 50)
        self.assertEqual(peak, 1, "同時有一個以上的寫入")
        self.assertEqual(p["waited"], 0)

    async def test_polling_settings_change_while_following(self):
        await self.follow()
        self.engine.set_options({"accel_period": 0.15})
        self.assertEqual(self.engine.phase, "following")
        self.assertEqual(self.engine.ring.poll["accel_period"], 0.15)
        saved = json.loads((self.folder / "preferences.json").read_text())
        self.assertEqual(saved["accel_period"], 0.15)
        self.fake.curl = 1.0
        await self.until(lambda: (self.flex() or 0) > 60, message="改了輪詢之後沒有繼續跟隨")

    async def test_old_poll_mode_preference_is_migrated(self):
        self.assertEqual({k: clean_options({"poll_mode": "wait"})[k] for k in POLL_KEYS}, POLL_PRESETS["wait"])
        self.assertEqual({k: clean_options({"poll_mode": "fixed"})[k] for k in POLL_KEYS}, POLL_DEFAULTS)
        kept = clean_options({"poll_mode": "wait", "accel_period": 0.2, "accel_timeout": 0.0, "accel_interval": 0.0})
        self.assertEqual(kept["accel_period"], 0.2)
        self.assertEqual(kept["accel_timeout"], 0.0)
        self.assertNotIn("poll_mode", clean_options({"poll_mode": "wait"}))
        (self.folder / "m").mkdir()
        (self.folder / "m" / "preferences.json").write_text(json.dumps({"poll_mode": "wait", "speed": "slow"}))
        old = Engine(self.folder / "m", check_studio=False)
        self.addCleanup(lambda: old.log_file and old.log_file.close())
        self.assertEqual(old.ring.poll, POLL_PRESETS["wait"])
        self.assertEqual(old.options["speed"], "slow")

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

    async def test_polling_state_and_reset(self):
        async with self.session.get(self.base + "/api/state") as r:
            state = await r.json()
        self.assertEqual(state["polling"]["label"], "未連線")
        self.assertEqual(state["polling"]["settings"], POLL_DEFAULTS)
        self.assertEqual(state["app"]["poll_presets"], POLL_PRESETS)
        async with self.session.post(self.base + "/api/options", json={"accel_period": 0.05, "accel_timeout": 0.03}) as r:
            self.assertEqual(r.status, 200)
        async with self.session.post(self.base + "/api/options", json={"accel_interval": 3}) as r:
            self.assertEqual(r.status, 400)
            self.assertIn("0 到 2 秒", (await r.json())["error"])
        async with self.session.post(self.base + "/api/ring/polling/reset", json={}) as r:
            self.assertEqual(r.status, 200)
        async with self.session.get(self.base + "/api/state") as r:
            state = await r.json()
        self.assertEqual(state["polling"]["settings"]["accel_period"], 0.05)
        self.assertEqual(state["polling"]["sent"], 0)

    async def test_page_and_assets_are_served(self):
        async with self.session.get(self.base + "/") as r:
            text = await r.text()
            self.assertEqual(r.status, 200)
        self.assertIn("app.js?v=", text)
        for name in ("/app.js", "/app.css"):
            async with self.session.get(self.base + name) as r:
                self.assertEqual(r.status, 200)

    async def test_sway_calibration_api_validation_and_default_compatibility(self):
        engine = self.app[app_server.ENGINE]
        await engine.ring_connect()
        for _ in range(100):
            if engine.ring.state["connected"]:
                break
            await asyncio.sleep(.02)
        self.assertTrue(engine.ring.state["connected"])
        async with self.session.post(self.base + "/api/calibration/start", json={"sway": "yes"}) as r:
            self.assertEqual(r.status, 400)
        self.assertFalse(engine.cal["active"])
        async with self.session.post(self.base + "/api/calibration/start", json={"sway": True}) as r:
            self.assertEqual(r.status, 200)
        async with self.session.get(self.base + "/api/state") as r:
            state = await r.json()
        self.assertTrue(state["calibration"]["sway"])
        self.assertIn("sway", state["follow"])
        self.assertEqual(engine.phase, "idle")
        async with self.session.post(self.base + "/api/calibration/capture", json={"pose": "right"}) as r:
            self.assertEqual(r.status, 400)
        async with self.session.post(self.base + "/api/calibration/cancel", json={}) as r:
            self.assertEqual(r.status, 200)
        async with self.session.post(self.base + "/api/calibration/start", json={}) as r:
            self.assertEqual(r.status, 200)
        self.assertFalse(engine.cal["sway"])

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
