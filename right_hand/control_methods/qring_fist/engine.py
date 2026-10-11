"""QRing Hand 的核心：戒指的握拳程度 → 手掌面板的手指目標。

流程：戒指三軸（ring.py）→ 校正模型與濾波（follow.py）→ 每根手指的彎曲角度 → 手掌面板（hand.py）。
這是給人操作的遙控工具，不含任何模型；手掌的保護（落後、電壓、溫度、掉線、閒置、心跳）都在面板那一側。

安全上的幾個決定：
  - APP 啟動、戒指連上、校正完成都不會讓手動；只有「啟動」（或設定允許時的戒指長按）才會啟用扭力。
  - 啟動前要有新鮮而且有效的握拳讀值，手掌面板要沒有 fault、匯流排正常、沒有別的頁面在控制。
  - 戒指斷線、APP 結束、按停止：對面板送關扭力。APP 當掉時面板 3 秒沒收到心跳也會自己關。
  - 面板因為任何原因關了扭力（閒置、fault、別人按停止），這裡跟著回到停止，不會自己再啟動。
  - 讀值無效（手在晃、手的方向離開校正的弧線）時不更新目標，手停在上一個位置。
"""
import asyncio
import json
import math
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import aiohttp

from follow import BUILTIN_FIST, FINGERS, FINGER_LABELS, CurlFilter, CurlModel, StabilityWindow, finger_targets
from hand import HandLink, finish_tasks, normalize_url
from ring import RingLink

VERSION = "0.1.1"
RING_NAME = "R08_E703"
SEND_PERIOD_S = 0.12            # 和面板頁面拖滑桿時的送出間隔相同
FLEX_SPAN_DEG = 120.0           # 張開 −30° 到握 90°
GESTURE_DEBOUNCE_S = 0.6
STABLE_KEEP_S = 1.0             # 單擊確認時，接受這段時間內最後一次「已停穩」的平均
UI_SAMPLING_KEEP_S = 5.0        # 畫面最近有在看才持續讀三軸
STUDIO_STATUS_URL = "http://127.0.0.1:18765/api/control-status"

DEFAULTS = {
    "hand_url": "", "auto_link": True, "speed": "normal", "mode": "continuous",
    "smoothing": 0.5, "open_margin": 0.12, "fist_margin": 0.88, "off_limit": 35.0, "deadband": 0.04,
    "fist_pose": "builtin", "fist_scale": 1.0,
    "fingers": {name: True for name in FINGERS},
    "ring_touch": True, "long_press_stop": True, "long_press_start": False, "tap_confirm": True,
}
NUMBER_LIMITS = {"smoothing": (0.0, 0.9), "open_margin": (0.0, 0.4), "fist_margin": (0.6, 1.0),
                 "off_limit": (10.0, 80.0), "deadband": (0.01, 0.2), "fist_scale": (0.2, 1.0)}
BOOLEANS = ("auto_link", "ring_touch", "long_press_stop", "long_press_start", "tap_confirm")
RING_START_WAIT_S = 2.5         # 戒指長按啟動時，等新鮮讀值的時間
ISSUE_TEXT = {
    "moving": "手在晃動，停一下",
    "off_axis": "手的方向和校正時差太多，把手擺回校正時的方向",
    "out_of_range": "讀值超出張開到握拳的範圍，把手擺回校正時的方向",
    "invalid": "戒指回報的三軸資料無效",
    "uncalibrated": "還沒校正",
    "calibrating": "校正中",
    "waiting": "等戒指的三軸資料",
}
RELEASE_TEXT = {
    "hold_timeout": "手掌停著沒有新動作超過面板的閒置時間，面板已自動放鬆。要繼續就再啟動一次",
    "watchdog": "手掌面板超過 3 秒沒收到這個 APP 的回報，已自動放鬆",
    "stop": "手掌面板收到停止（有人在面板頁面按了停止）",
    "shutdown": "手掌面板正在結束，已放鬆",
    "enable_timeout": "手掌面板啟用扭力逾時，已放鬆",
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_options(saved):
    """把存檔裡看得懂、在範圍內的值套到預設值上；其餘丟掉。"""
    out = json.loads(json.dumps(DEFAULTS))
    if not isinstance(saved, dict):
        return out
    for key, (low, high) in NUMBER_LIMITS.items():
        if _number(saved.get(key)) and low <= saved[key] <= high:
            out[key] = float(saved[key])
    for key in BOOLEANS:
        if isinstance(saved.get(key), bool):
            out[key] = saved[key]
    if saved.get("speed") in ("slow", "normal", "fast"):
        out["speed"] = saved["speed"]
    if saved.get("mode") in ("continuous", "binary"):
        out["mode"] = saved["mode"]
    if isinstance(saved.get("fist_pose"), str) and 0 < len(saved["fist_pose"]) <= 40:
        out["fist_pose"] = saved["fist_pose"]
    if isinstance(saved.get("fingers"), dict):
        for name in FINGERS:
            if isinstance(saved["fingers"].get(name), bool):
                out["fingers"][name] = saved["fingers"][name]
    if isinstance(saved.get("hand_url"), str) and saved["hand_url"].strip():
        try:
            out["hand_url"] = normalize_url(saved["hand_url"])
        except ValueError:
            pass
    return out


class Engine:
    def __init__(self, folder, ring_transport_factory=None, site=None, check_studio=True,
                 monotonic=time.monotonic):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.monotonic = monotonic
        self.check_studio = check_studio
        self.preferences_path = self.folder / "preferences.json"
        self.calibration_path = self.folder / "calibration.json"
        self.events = deque(maxlen=200)
        self.log_path = self.folder / ("session-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")
        self.log_file = None
        saved = self._read_json(self.preferences_path)
        self.options = clean_options(saved)
        if not self.options["hand_url"] and isinstance(site, dict) and site.get("hand_url"):
            try:
                self.options["hand_url"] = normalize_url(site["hand_url"])
            except ValueError:
                pass
        self.model = None
        self.calibration = None
        profile = self._read_json(self.calibration_path)
        if isinstance(profile, dict) and profile.get("version") == 1 and profile.get("name") == RING_NAME:
            try:
                self.model = CurlModel(profile["open"], profile["fist"])
                self.calibration = profile
            except (ValueError, KeyError, TypeError):
                pass
        self.ring = RingLink(RING_NAME, ring_transport_factory, self._on_accel, self._on_gesture,
                             self._on_ring_disconnect, self.log, monotonic)
        self.hand = HandLink(self.log, self._on_hand_status, monotonic)
        self.filter = CurlFilter()
        self.window = StabilityWindow()
        self.last_stable = None                # (monotonic, 平均方向, 散布度)
        self.cal = {"active": False, "step": None, "open": None, "fist": None, "message": "", "model": None}
        self.phase = "idle"                    # idle / starting / following / stopping
        self.message = ""                      # 最近一次啟動、停止的說明
        self.message_kind = "info"             # info / warn
        self.follow_started = 0.0
        self.curl = {"valid": False, "issue": "waiting"}
        self.curl_at = 0.0
        self.targets = None
        self.sent = None
        self.sent_at = 0.0
        self.sent_count = 0
        self.fist_pose = None                  # 啟動時解析出來的握拳姿勢
        self.last_gesture = 0.0
        self.ui_seen = 0.0
        self.sample_until = 0.0
        self.kick = asyncio.Event()
        self.arm_lock = asyncio.Lock()
        self.tasks = set()
        self.worker = None
        self.closing = False
        self.studio = {"running": False, "ring_connected": False}

    # ------------------------------------------------------------------ 檔案與紀錄
    @staticmethod
    def _read_json(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path, data):
        temporary = Path(str(path) + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def log(self, kind, message, **fields):
        row = dict(time=datetime.now().astimezone().isoformat(timespec="milliseconds"), kind=kind,
                   message=message, **fields)
        self.events.append(row)
        try:
            if self.log_file is None:
                self.log_file = self.log_path.open("a", buffering=1, encoding="utf-8")
            self.log_file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        return row

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _say(self, message, kind="info"):
        self.message, self.message_kind = message, kind

    # ------------------------------------------------------------------ 生命週期
    async def start(self, auto_link=True):
        self.worker = asyncio.ensure_future(self._sender())
        self.log("app", "QRing Hand %s 已啟動；戒指與手掌都不會自動動作" % VERSION)
        if auto_link and self.options["auto_link"] and self.options["hand_url"]:
            await self.hand.link(self.options["hand_url"])

    async def close(self):
        try:
            if self.phase in ("starting", "following"):
                await self.stop("APP 結束，已關掉手掌扭力")
        finally:
            self.closing = True                # sender 自己看到旗標就結束；不靠取消
            self.kick.set()
            await finish_tasks([self.worker])
            spawned = list(self.tasks)
            for task in spawned:
                task.cancel()
            await finish_tasks(spawned)
            await self.ring.disconnect()
            await self.hand.unlink()
            self.log("app", "已結束")
            if self.log_file:
                self.log_file.close()
                self.log_file = None

    # ------------------------------------------------------------------ 戒指
    async def ring_connect(self):
        if self.ring.state["connected"] or self.ring.state["busy"]:
            return
        if self.check_studio:
            await self._check_studio()
            if self.studio["ring_connected"]:
                raise ValueError("QRing Studio 正連著這顆戒指。先在 QRing Studio 中斷連線（或結束它），再回來連接")
        await self.ring.connect()

    async def ring_disconnect(self):
        if self.phase in ("starting", "following"):
            await self.stop("戒指中斷連線，已關掉手掌扭力")
        if self.cal["active"]:
            self.cal_cancel()
        await self.ring.disconnect()

    async def _check_studio(self):
        status = {"running": False, "ring_connected": False}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.6)) as session:
                async with session.get(STUDIO_STATUS_URL) as r:
                    data = await r.json(content_type=None)
            if isinstance(data, dict) and "connected" in data:
                status = {"running": True, "ring_connected": bool(data.get("connected"))}
        except Exception:
            pass
        self.studio = status

    def _on_ring_disconnect(self, expected):
        self.filter.reset()
        self.window.clear()
        self.last_stable = None
        self.curl = {"valid": False, "issue": "waiting"}
        if self.cal["active"]:
            self.cal_cancel("戒指連線中斷，校正已取消")
        if self.phase in ("starting", "following"):
            self._spawn(self.stop("戒指連線中斷，已關掉手掌扭力", kind="warn"))

    def _active_model(self):
        if self.cal["active"]:
            return self.cal["model"]
        return self.model

    def _on_accel(self, raw, now):
        self.window.add(now, raw)
        summary = self.window.summary(now)
        if summary["stable"]:
            self.last_stable = (now, summary["mean"], summary["spread"])
        model = self._active_model()
        if model is None:
            self.curl = {"valid": False, "issue": "calibrating" if self.cal["active"] else "uncalibrated"}
            return
        o = self.options
        m = model.measure(raw, o["off_limit"])
        if m["valid"]:
            self.filter.update(m["progress"], o["open_margin"], o["fist_margin"], o["smoothing"], o["mode"])
            self.curl_at = now
        m["value"] = self.filter.output
        m["span"] = round(model.span, 1)
        self.curl = m
        if self.filter.output is not None:
            self.targets = self._targets(self.filter.output)
        self.kick.set()

    def _on_gesture(self, code, now):
        if now - self.last_gesture < GESTURE_DEBOUNCE_S:
            return
        self.last_gesture = now
        if self.cal["active"]:
            if code == 3 and self.options["tap_confirm"]:
                try:
                    if self.cal["step"] == "review":
                        self.cal_save()
                    else:
                        self.cal_capture()
                except ValueError as e:
                    self.cal["message"] = str(e)
            return
        if code == 4:
            if self.phase == "following" and self.options["long_press_stop"]:
                self._spawn(self.stop("戒指長按：已停止"))
            elif self.phase == "idle" and self.options["long_press_start"]:
                self._spawn(self._arm_from_ring())

    async def _arm_from_ring(self):
        # 視窗沒開著時平常不讀三軸；先讀一小段，等到新鮮而且有效的讀值再啟動
        deadline = self.monotonic() + RING_START_WAIT_S
        self.sample_until = deadline + 1.0
        self.kick.set()
        while self.monotonic() < deadline and not (self.ring.fresh(0.5) and self.curl.get("valid")):
            await asyncio.sleep(0.1)
        try:
            await self.arm(source="戒指長按")
        except ValueError as e:
            self._say("戒指長按無法啟動：%s" % e, "warn")
            self.log("follow", self.message)

    # ------------------------------------------------------------------ 手掌
    async def hand_link(self, url=None):
        url = normalize_url(url if url is not None else self.options["hand_url"])
        if url != self.options["hand_url"]:
            self.options["hand_url"] = url
            self._save_options()
        await self.hand.link(url)

    async def hand_unlink(self):
        if self.phase in ("starting", "following"):
            await self.stop("中斷手掌連線前已關掉扭力")
        await self.hand.unlink()

    async def hand_clear_fault(self):
        result = await self.hand.clear_fault()
        if result.get("status") != "ok":
            raise ValueError(result.get("detail") or "手掌面板沒有接受解除鎖定")
        self.log("hand", "已解除手掌面板的保護鎖定", previous=result.get("cleared"))
        self._say("已解除手掌面板的保護鎖定")

    def _on_hand_status(self, status):
        if self.phase != "following":
            return
        if status is None:
            self._lost("連不到手掌面板。面板 3 秒沒收到回報會自己關掉扭力；仍請確認手已放鬆")
            self._spawn(self.hand.stop())
            return
        if self.monotonic() - self.follow_started < 1.0:
            return                         # 剛啟用：這一筆狀態可能是啟用之前讀的
        if status.get("torque_on") and status.get("you_control"):
            return
        fault = status.get("fault")
        release = status.get("last_release") or {}
        if fault:
            self._lost("手掌保護機制啟動並鎖定：%s（%s）。看過手的狀況後，在「手掌」按解除鎖定"
                       % (fault.get("reason_code"), fault.get("detail")))
        elif status.get("torque_on"):
            self._lost("手掌改由另一個頁面控制")
        else:
            self._lost(RELEASE_TEXT.get(release.get("reason"), "手掌面板已關掉扭力（%s）" % release.get("reason")))

    def _lost(self, message):
        self.phase = "idle"
        self._say(message, "warn")
        self.log("follow", "跟隨中止：%s" % message)

    def _resolve_fist_pose(self):
        name = self.options["fist_pose"]
        if name == "builtin":
            return dict(BUILTIN_FIST)
        for pose in self.hand.poses:
            if pose["name"] == name:
                if not pose["playable"]:
                    raise ValueError("握拳姿勢「%s」目前不能用（面板標示不可重現）。到設定換一個" % name)
                try:
                    return {f: (float(pose["fingers"][f]["flex"]), float(pose["fingers"][f]["side"])) for f in FINGERS}
                except (KeyError, TypeError, ValueError):
                    raise ValueError("握拳姿勢「%s」的資料不完整。到設定換一個" % name)
        raise ValueError("手掌面板上找不到握拳姿勢「%s」。到設定重新選擇" % name)

    def _targets(self, curl):
        pose = self.fist_pose if self.phase in ("starting", "following") else None
        if pose is None:
            try:
                pose = self._resolve_fist_pose()
            except ValueError:
                pose = BUILTIN_FIST
        return finger_targets(curl, pose, self.options["fingers"], self.options["fist_scale"])

    # ------------------------------------------------------------------ 啟動與停止
    def arm_problem(self):
        """現在能不能啟動；可以就回 None。"""
        if self.cal["active"]:
            return "校正進行中；先儲存或取消校正"
        if not self.ring.state["connected"]:
            return "先連接戒指"
        if self.model is None:
            return "先完成校正（張開與握拳各記一次）"
        problem = self.hand.ready_problem()
        if problem:
            return problem
        try:
            self._resolve_fist_pose()
        except ValueError as e:
            return str(e)
        if not self.ring.fresh(1.0):
            return "還沒收到戒指的三軸資料"
        if not self.curl.get("valid"):
            return ISSUE_TEXT.get(self.curl.get("issue"), "握拳讀值無效")
        if self.monotonic() - self.curl_at > 1.0 or self.filter.output is None:
            return "握拳讀值不是最新的"
        return None

    async def arm(self, source="畫面"):
        async with self.arm_lock:
            if self.phase != "idle":
                return
            problem = self.arm_problem()
            if problem:
                raise ValueError(problem)
            self.fist_pose = self._resolve_fist_pose()
            self.phase = "starting"
            result = await self.hand.enable(self.options["speed"])
            if self.phase != "starting":            # 等待期間被停止
                return
            if result.get("status") != "accepted":
                self.phase = "idle"
                detail = result.get("detail") or result.get("reason_code") or "手掌面板沒有接受"
                self.log("follow", "啟動被拒：%s" % detail, reason_code=result.get("reason_code"))
                raise ValueError("手掌面板沒有啟用扭力：%s" % detail)
            self.sent = None
            self.sent_at = 0.0
            self.sent_count = 0
            self.follow_started = self.monotonic()
            self.phase = "following"
            self._say("跟隨中（由%s啟動）" % source)
            self.log("follow", "開始跟隨", source=source, speed=self.options["speed"], mode=self.options["mode"],
                     fist_pose=self.options["fist_pose"])
            self.kick.set()

    async def stop(self, message="已停止，手掌扭力已關", kind="info"):
        """關掉手掌扭力。不管現在是不是這個 APP 在控制都會送出（和面板頁面的停止相同）。"""
        was = self.phase
        self.phase = "stopping" if was in ("starting", "following") else was
        result = None
        if self.hand.state["linked"]:
            result = await self.hand.stop()
        if self.phase == "stopping":
            self.phase = "idle"
        if result is not None and (result.get("status") != "accepted" or result.get("torque_off") != "ok"):
            detail = result.get("detail") or result.get("reason_code") or "面板沒有確認"
            self._say("停止已送出，但面板沒有確認扭力已關（%s）。請確認手已放鬆，必要時切斷伺服機電源" % detail, "warn")
            self.log("follow", self.message)
            return False
        if was in ("starting", "following") or result is not None:
            self._say(message, kind)
            self.log("follow", message, sent=self.sent_count)
        return True

    def _changed(self, targets):
        if self.sent is None:
            return True
        diff = max(max(abs(targets[f]["flex"] - self.sent[f]["flex"]), abs(targets[f]["side"] - self.sent[f]["side"]))
                   for f in FINGERS)
        if diff >= self.options["deadband"] * FLEX_SPAN_DEG:
            return True
        return diff > 0.05 and self.filter.output in (0.0, 1.0)

    async def _sender(self):
        while not self.closing:
            waiter = asyncio.ensure_future(self.kick.wait())
            try:                               # 不用 wait_for：它會在 kick 剛好被設的時候把取消吃掉
                await asyncio.wait({waiter}, timeout=0.25)
            finally:
                waiter.cancel()
            if self.closing:
                break
            self.kick.clear()
            now = self.monotonic()
            self.ring.want_accel = (self.phase in ("starting", "following") or self.cal["active"]
                                    or now - self.ui_seen < UI_SAMPLING_KEEP_S or now < self.sample_until)
            self.ring.want_touch = self.options["ring_touch"]
            if self.phase != "following" or self.targets is None or not self.curl.get("valid"):
                continue
            wait = SEND_PERIOD_S - (self.monotonic() - self.sent_at)
            if wait > 0:
                await asyncio.sleep(wait)
                if self.phase != "following" or not self.curl.get("valid"):
                    continue
            targets = self.targets
            if not self._changed(targets):
                continue
            self.sent_at = self.monotonic()
            result = await self.hand.set_fingers(targets)
            if self.phase != "following":
                continue
            status = result.get("status")
            if status == "accepted":
                self.sent = targets
                self.sent_count += 1
            elif status == "rejected":
                code = result.get("reason_code")
                if code in ("TORQUE_OFF", "CONTROL_HELD", "STOPPING"):
                    continue                   # 下一筆狀態會說明原因並結束跟隨
                self.sent = targets            # 同一組目標不重送
                self._say("手掌面板拒絕了目標：%s" % (result.get("detail") or code), "warn")
                self.log("follow", self.message, reason_code=code)

    # ------------------------------------------------------------------ 校正
    def cal_start(self):
        if self.phase != "idle":
            raise ValueError("跟隨中不能校正；先停止")
        if not self.ring.state["connected"]:
            raise ValueError("先連接戒指")
        self.cal = {"active": True, "step": "open", "open": None, "fist": None, "model": None,
                    "message": "把手張開、手掌擺成你平常要操作的方向，停住"}
        self.filter.reset()
        self.window.clear()
        self.last_stable = None
        self.log("calibration", "開始校正")

    def cal_capture(self, pose=None):
        if not self.cal["active"]:
            raise ValueError("還沒開始校正")
        pose = pose or self.cal["step"]
        if pose not in ("open", "fist"):
            raise ValueError("兩個姿勢都記好了；按儲存，或重記其中一個")
        now = self.monotonic()
        if not self.last_stable or now - self.last_stable[0] > STABLE_KEEP_S:
            raise ValueError("手還沒停穩。保持姿勢不動約 2 秒再記錄")
        mean, spread = list(self.last_stable[1]), self.last_stable[2]
        other = self.cal["fist" if pose == "open" else "open"]
        self.cal[pose] = mean
        self.cal["model"] = None
        if other is None:
            self.cal["step"] = "fist" if pose == "open" else "open"
            self.cal["message"] = ("張開已記錄。現在握拳（手掌方向不要變），停住" if pose == "open"
                                   else "握拳已記錄。現在把手張開，停住")
        else:
            try:
                self.cal["model"] = CurlModel(self.cal["open"], self.cal["fist"])
            except ValueError as e:
                self.cal[pose] = None
                self.cal["step"] = pose
                raise ValueError(str(e))
            self.cal["step"] = "review"
            self.cal["message"] = ("兩個姿勢相差 %.0f°。張開、握拳幾次看下方的握拳程度有沒有跟上，再儲存"
                                   % self.cal["model"].span)
        self.filter.reset()
        self.window.clear()                    # 下一個姿勢要重新停穩
        self.last_stable = None
        self.log("calibration", "已記錄%s" % ("張開" if pose == "open" else "握拳"), spread_deg=spread)

    def cal_retake(self, pose):
        if not self.cal["active"] or pose not in ("open", "fist"):
            raise ValueError("沒有可以重記的姿勢")
        self.cal[pose] = None
        self.cal["model"] = None
        self.cal["step"] = pose
        self.cal["message"] = "重記%s：擺好姿勢，停住" % ("張開" if pose == "open" else "握拳")
        self.filter.reset()
        self.window.clear()
        self.last_stable = None

    def cal_save(self):
        model = self.cal.get("model")
        if not self.cal["active"] or model is None:
            raise ValueError("張開與握拳都要先記錄")
        profile = {"version": 1, "name": RING_NAME, "open": self.cal["open"], "fist": self.cal["fist"],
                   "span_deg": round(model.span, 1),
                   "saved_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._write_json(self.calibration_path, profile)
        self.model, self.calibration = model, profile
        self.cal = {"active": False, "step": None, "open": None, "fist": None, "model": None, "message": ""}
        self.filter.reset()
        self.log("calibration", "校正已儲存", span_deg=profile["span_deg"])
        self._say("校正已儲存（張開與握拳相差 %.0f°）" % model.span)

    def cal_cancel(self, message=""):
        if self.cal["active"]:
            self.log("calibration", message or "校正已取消")
        self.cal = {"active": False, "step": None, "open": None, "fist": None, "model": None, "message": message}
        self.filter.reset()

    # ------------------------------------------------------------------ 選項
    def _save_options(self):
        self._write_json(self.preferences_path, self.options)

    def set_options(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise ValueError("沒有要改的選項")
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise ValueError("未知選項：%s" % "、".join(sorted(unknown)))
        new = json.loads(json.dumps(self.options))
        for key, value in changes.items():
            if key in NUMBER_LIMITS:
                low, high = NUMBER_LIMITS[key]
                if not _number(value) or not low <= value <= high:
                    raise ValueError("%s 要在 %s 到 %s 之間" % (key, low, high))
                new[key] = float(value)
            elif key in BOOLEANS:
                if not isinstance(value, bool):
                    raise ValueError("%s 要是開或關" % key)
                new[key] = value
            elif key == "speed":
                if value not in ("slow", "normal", "fast"):
                    raise ValueError("速度只能是慢、中、快")
                new[key] = value
            elif key == "mode":
                if value not in ("continuous", "binary"):
                    raise ValueError("模式只能是連續或兩段")
                new[key] = value
            elif key == "fist_pose":
                if not isinstance(value, str) or not 0 < len(value) <= 40:
                    raise ValueError("握拳姿勢名稱無效")
                new[key] = value
            elif key == "fingers":
                if not isinstance(value, dict) or set(value) - set(FINGERS) or \
                        any(not isinstance(v, bool) for v in value.values()):
                    raise ValueError("手指選項無效")
                new[key].update(value)
                if not any(new[key].values()):
                    raise ValueError("至少要有一根手指跟隨")
            elif key == "hand_url":
                new[key] = normalize_url(value) if isinstance(value, str) and value.strip() else ""
        if new["fist_margin"] - new["open_margin"] < 0.2:
            raise ValueError("握拳到底與張開的界線至少要差 20%")
        if self.phase in ("starting", "following") and \
                any(k in changes for k in ("speed", "fist_pose", "hand_url")):
            raise ValueError("跟隨中不能改速度、握拳姿勢或手掌位址；先停止")
        self.options = new
        self._save_options()
        if any(k in changes for k in ("mode", "open_margin", "fist_margin")):
            self.filter.reset()
        if self.filter.output is not None:
            self.targets = self._targets(self.filter.output)
            self.kick.set()
        self.log("options", "選項已更新", changes=changes)

    # ------------------------------------------------------------------ 給畫面的狀態
    def snapshot(self, seen=True):
        now = self.monotonic()
        if seen:
            self.ui_seen = now
        stable = self.window.summary(now) if self.ring.state["connected"] else None
        curl = dict(self.curl)
        curl["age"] = round(now - self.curl_at, 2) if self.curl_at else None
        if curl.get("issue"):
            curl["issue_text"] = ISSUE_TEXT.get(curl["issue"], curl["issue"])
        cal = {k: self.cal[k] for k in ("active", "step", "open", "fist", "message")}
        cal["span"] = round(self.cal["model"].span, 1) if self.cal.get("model") else None
        cal["stable"] = stable
        cal["saved"] = self.calibration
        problem = self.arm_problem() if self.phase == "idle" else None
        return {
            "app": {"version": VERSION, "log_file": self.log_path.name},
            "ring": self.ring.snapshot(),
            "hand": self.hand.snapshot(),
            "studio": self.studio,
            "follow": {"phase": self.phase, "message": self.message, "message_kind": self.message_kind,
                       "problem": problem, "curl": curl, "targets": self.targets, "sent": self.sent,
                       "sent_count": self.sent_count,
                       "stale": self.phase == "following" and not self.ring.fresh(2.0)},
            "calibration": cal,
            "options": self.options,
            "fingers": [{"key": f, "label": FINGER_LABELS[f]} for f in FINGERS],
            "events": list(self.events)[-40:],
        }

    def menu_status(self):
        ring = bool(self.ring.state["connected"])
        hand = bool(self.hand.reachable)
        panel = self.hand.status or {}
        if self.phase == "following":
            mode, label = "following", "跟隨中"
        elif self.phase in ("starting", "stopping"):
            mode, label = "busy", "切換中"
        elif not ring:
            mode, label = "disconnected", "戒指未連線"
        elif self.cal["active"]:
            mode, label = "calibrating", "校正中"
        elif self.model is None:
            mode, label = "uncalibrated", "未校正"
        elif not hand:
            mode, label = "nohand", "手掌未連線"
        elif panel.get("fault"):
            mode, label = "fault", "手掌鎖定"
        else:
            mode, label = "ready", "待命"
        return {"ring": ring, "hand": hand, "calibrated": self.model is not None,
                "following": self.phase == "following", "busy": self.phase in ("starting", "stopping"),
                "mode": mode, "label": label}
