"""和 Raspberry Pi 上的右手操作面板（this-is-yourX 的 right_hand/hand_panel）連線。

這個 APP 在面板眼中就是一個「控制的頁面」：
  - 每 0.25 秒 GET /api/status?client=…，同時是心跳；面板 3 秒沒收到就自己關扭力。
  - POST /api/enable 啟用扭力（面板會先把目標設成目前位置）、/api/fingers 下目標、/api/stop 關扭力。
落後、電壓、溫度、掉線、到位的檢查與 fault 鎖定都在面板那一側，這裡不重做，也不繞過。
"""
import asyncio
import ipaddress
import secrets
import time
from urllib.parse import urlsplit

import aiohttp

DEFAULT_PORT = 8765
POLL_PERIOD_S = 0.25
POSES_PERIOD_S = 10.0
UNREACHABLE_AFTER = 3          # 連續幾次狀態查詢失敗就當作連不到


def normalize_url(text):
    """接受 192.168.x.x、192.168.x.x:8765、http://主機:埠；回傳 http://主機:埠。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("請輸入手掌面板的位址，例如 192.168.1.50:8765")
    text = text.strip()
    if "://" not in text:
        text = "http://" + text
    try:
        u = urlsplit(text)
        port = u.port or DEFAULT_PORT
    except ValueError:
        raise ValueError("手掌面板的位址格式不對")
    if u.scheme != "http" or not u.hostname or u.path not in ("", "/") or u.query or u.username:
        raise ValueError("手掌面板的位址要像 http://192.168.1.50:8765")
    host = u.hostname
    try:
        if ipaddress.ip_address(host).version == 6:
            host = "[%s]" % host
    except ValueError:
        pass
    return "http://%s:%d" % (host, port)


class HandLink:
    def __init__(self, log=None, on_status=None, monotonic=time.monotonic):
        self.log = log or (lambda *a, **k: None)
        self.on_status = on_status or (lambda status: None)
        self.monotonic = monotonic
        self.client_id = "ringhand" + secrets.token_hex(6)
        self.url = None
        self.session = None
        self.task = None
        self.status = None             # 面板最近一次回的狀態（原樣）
        self.status_at = 0.0
        self.failures = 0
        self.poses = []
        self.state = {"linked": False, "reachable": False, "message": "尚未連線", "url": None, "latency_ms": None}

    # ------------------------------------------------------------------ 狀態
    @property
    def reachable(self):
        return self.state["reachable"] and self.status is not None

    def snapshot(self):
        out = dict(self.state, client=self.client_id[:8], poses=self.poses, panel=None)
        s = self.status
        if s and self.state["reachable"]:
            servos = s.get("servos") or []
            ok = [r for r in servos if r.get("ok")]
            volts = [r["voltage_v"] for r in ok if isinstance(r.get("voltage_v"), (int, float))]
            temps = [r["temperature_c"] for r in ok if isinstance(r.get("temperature_c"), (int, float))]
            out["panel"] = {
                "adapter": s.get("adapter"), "simulated": s.get("simulated"), "bus": s.get("bus"),
                "torque_on": s.get("torque_on"), "you_control": s.get("you_control"),
                "controller": s.get("controller"), "speed": s.get("speed"), "moving": s.get("moving"),
                "fault": s.get("fault"), "hold_remaining_s": s.get("hold_remaining_s"),
                "hold_timeout_s": s.get("hold_timeout_s"), "fingers": s.get("fingers"),
                "last_release": s.get("last_release"), "last_move": s.get("last_move"),
                "servos_ok": len(ok), "servos_total": len(servos),
                "missing": [r.get("id") for r in servos if not r.get("ok")],
                "voltage_min": min(volts) if volts else None, "temperature_max": max(temps) if temps else None,
                "age_s": round(self.monotonic() - self.status_at, 2),
            }
        return out

    def ready_problem(self):
        """能不能啟用扭力；可以就回 None，否則回一句給人看的原因。"""
        if not self.state["linked"]:
            return "還沒連接手掌"
        if not self.reachable:
            return "連不到手掌面板（%s）" % self.state["message"]
        s = self.status
        bus = s.get("bus") or {}
        if bus.get("state") != "attached":
            return "手掌面板拿不到伺服機匯流排：%s" % (bus.get("detail") or bus.get("state") or "未知")
        if s.get("fault"):
            return "手掌面板有還沒解除的保護鎖定：%s" % s["fault"].get("reason_code")
        if s.get("torque_on") and not s.get("you_control"):
            return "另一個頁面正在控制這隻手；那一頁按停止後才能接手"
        servos = s.get("servos") or []
        missing = [r.get("id") for r in servos if not r.get("ok")]
        if servos and len(missing) == len(servos):
            return "8 顆伺服機都沒有回應（伺服機電源沒開？）"
        if missing:
            return "這些伺服機沒有回應：%s" % missing
        return None

    # ------------------------------------------------------------------ 連線
    async def link(self, url):
        url = normalize_url(url)
        await self.unlink()
        self.url = url
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5))
        self.failures = 0
        self.state.update(linked=True, reachable=False, message="正在連接手掌面板…", url=url)
        self.task = asyncio.ensure_future(self._poll())
        self.log("hand", "開始連接手掌面板 %s" % url)

    async def unlink(self):
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        session, self.session = self.session, None
        if session:
            await session.close()
        was = self.state["linked"]
        self.status = None
        self.poses = []
        self.state.update(linked=False, reachable=False, message="尚未連線", latency_ms=None)
        if was:
            self.log("hand", "已中斷和手掌面板的連線")

    async def _get(self, path, timeout=1.5):
        async with self.session.get(self.url + path, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return await r.json(content_type=None)

    async def _poll(self):
        next_poses = 0.0
        while True:
            began = self.monotonic()
            try:
                data = await self._get("/api/status?client=" + self.client_id)
                if not isinstance(data, dict) or data.get("status") != "ok":
                    raise ValueError((data or {}).get("detail") or "面板回應的格式不對")
                first = not self.state["reachable"]
                self.status = data
                self.status_at = self.monotonic()
                self.failures = 0
                self.state.update(reachable=True, message="已連線",
                                  latency_ms=int((self.status_at - began) * 1000))
                if first:
                    self.log("hand", "手掌面板已連線（%s）" % ("模擬" if data.get("simulated") else "實機"))
                self.on_status(data)
                if began >= next_poses:
                    next_poses = began + POSES_PERIOD_S
                    await self._load_poses()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failures += 1
                if self.failures >= UNREACHABLE_AFTER and (self.state["reachable"] or self.failures == UNREACHABLE_AFTER):
                    detail = str(e) or type(e).__name__
                    if self.state["reachable"]:
                        self.log("hand", "連不到手掌面板：%s" % detail)
                    self.state.update(reachable=False, message=detail, latency_ms=None)
                    self.on_status(None)
            await asyncio.sleep(max(0.05, POLL_PERIOD_S - (self.monotonic() - began)))

    async def _load_poses(self):
        try:
            data = await self._get("/api/poses", timeout=2.0)
            self.poses = [{"name": p["name"], "label": p.get("label") or "", "kind": p.get("kind"),
                           "playable": bool(p.get("playable")), "fingers": p.get("fingers") or {}}
                          for p in data.get("poses", []) if isinstance(p, dict) and "name" in p]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def post(self, path, body=None, timeout=6.0):
        """回傳面板的 JSON。連不到時回 {"status": "error", "reason_code": "UNREACHABLE", …}。"""
        if not self.session or not self.url:
            return {"status": "error", "reason_code": "NOT_LINKED", "detail": "還沒連接手掌"}
        payload = dict(body or {}, client=self.client_id)
        try:
            async with self.session.post(self.url + path, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                data = await r.json(content_type=None)
            if not isinstance(data, dict):
                raise ValueError("面板回應的格式不對")
            return data
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"status": "error", "reason_code": "UNREACHABLE", "detail": str(e) or type(e).__name__}

    async def enable(self, speed):
        return await self.post("/api/enable", {"speed": speed})

    async def stop(self):
        return await self.post("/api/stop")

    async def set_fingers(self, fingers):
        return await self.post("/api/fingers", {"fingers": fingers}, timeout=2.0)

    async def clear_fault(self):
        return await self.post("/api/clear_fault")
