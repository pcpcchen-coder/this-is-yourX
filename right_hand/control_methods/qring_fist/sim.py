"""模擬戒指：測試與示範用（server.py --fake-ring）。回應和實機相同格式的封包，不碰藍牙。"""
import asyncio
import math
import random
import struct
import time

from follow import cross, dot, unit
from ring import pack

OPEN_VEC = (0.05, -0.20, -0.98)        # 掌心朝下、手指伸直時的假想重力方向
FIST_VEC = (0.10, -0.97, 0.22)


def slerp(a, b, t):
    a, b = unit(a), unit(b)
    omega = math.acos(max(-1.0, min(1.0, dot(a, b))))
    if omega < 1e-6:
        return a
    return tuple((math.sin((1 - t) * omega) * a[i] + math.sin(t * omega) * b[i]) / math.sin(omega) for i in range(3))


class FakeRingTransport:
    def __init__(self, demo=False, noise=0.0, seed=1):
        self.demo = demo
        self.noise = noise
        self.random = random.Random(seed)
        self.on_frame = None
        self.connected = False
        self.curl = 0.0                 # 0 張開、1 握拳；可以超出範圍
        self.twist = 0.0                # 離開弧線平面的角度（度），模擬轉手腕
        self.magnitude = 1.0
        self.silent = False             # True：不回三軸（模擬資料中斷）
        self.reply_delay = 0.0          # 三軸回報晚這麼久才到（模擬藍牙的一來一回）
        self.touch = [0x3B, 1, 0, 0, 0]
        self.reporting = False
        self.writes = []
        self.started = time.monotonic()

    async def open(self, on_frame):
        self.on_frame = on_frame
        self.connected = True
        return {"hardware": "SIM_RING", "firmware": "SIM_1.0", "serial": ""}

    async def close(self):
        self.connected = False

    def _emit(self, frame):
        if self.connected and self.on_frame:
            asyncio.get_running_loop().call_soon(self.on_frame, frame)

    def _emit_now(self, frame):
        if self.connected and self.on_frame:
            self.on_frame(frame)

    def vector(self):
        curl = self.curl
        if self.demo:                    # 示範：張開停 3 秒、1.5 秒握起來、握拳停 3 秒、1.5 秒張開
            t = (time.monotonic() - self.started) % 9.0
            curl = 0.0 if t < 3 else (t - 3) / 1.5 if t < 4.5 else 1.0 if t < 7.5 else (9 - t) / 1.5
        g = slerp(OPEN_VEC, FIST_VEC, curl)
        if self.twist:
            a = unit(OPEN_VEC)
            d = dot(a, unit(FIST_VEC))
            along = unit(tuple(unit(FIST_VEC)[i] - d * a[i] for i in range(3)))
            n = cross(a, along)
            r = math.radians(self.twist)
            g = unit(tuple(math.cos(r) * g[i] + math.sin(r) * n[i] for i in range(3)))
        if self.noise:
            g = tuple(x + self.random.gauss(0, self.noise) for x in g)
        return tuple(int(round(x * 8192 * self.magnitude)) for x in g)

    def gesture(self, code):
        self._emit(pack(0x73, bytes([0x2D, code])))

    def drop(self):
        """模擬戒指突然斷線。"""
        self.connected = False

    async def write(self, frame):
        if not self.connected:
            raise RuntimeError("Not connected")
        self.writes.append(bytes(frame))
        op, payload = frame[0], frame[1:15]
        if op == 3:
            self._emit(pack(3, bytes([77, 0])))
        elif op == 0x3B:
            if payload[0] == 1 and payload[2] == 0 and payload[3] == 0:      # 讀取
                self._emit(pack(0x3B, bytes(self.touch[1:])))
            else:
                if payload[0] == 1:
                    self.reporting = bool(payload[3])       # 回報開關；不在讀回的設定欄位裡
                else:
                    self.touch[3], self.touch[4] = payload[2], payload[3]
                self._emit(pack(0x3B, bytes(payload[:4])))
        elif op == 0xA1 and payload[0] == 3 and not self.silent:
            frame = pack(0xA1, b"\x03" + struct.pack(">hhh", *self.vector()))
            if self.reply_delay:
                asyncio.get_running_loop().call_later(self.reply_delay, self._emit_now, frame)
            else:
                self._emit(frame)
