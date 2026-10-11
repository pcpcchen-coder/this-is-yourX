"""Hardware adapter：真（SCS0009 經 rustypot）與假（模擬）實作同一組基本操作。

這是整個 hand_api 裡唯一會碰序列埠的模組。上層（motion、gateway）只用下面幾個操作：
  connected()           context manager：拿到匯流排（含跨程式的檔案鎖）並在離開時釋放
  read_servo(sid)       讀一顆：位置、電壓、溫度、扭力；不回應時 ok=False，不丟例外
  read_position(sid)    只讀位置（度）；不回應時丟 AdapterError
  read_voltage(sid)     只讀電壓（V）；不回應時丟 AdapterError
  set_speed(sid, rad_s) / set_goal(sid, deg) / set_torque(sid, on)
角度一律是度、0 為中位（不含中位修正，修正由上層加）。
"""
from __future__ import annotations

import contextlib
import fcntl
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass

from .config import SERVO_IDS


class AdapterError(Exception):
    """code 是給 gateway 轉成 reason_code 用的：BUS_BUSY、PORT_UNAVAILABLE、SERVO_NO_RESPONSE。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ServoReading:
    sid: int
    ok: bool
    observed_at: float                 # time.time()
    position_deg: float | None = None
    voltage_v: float | None = None
    temperature_c: float | None = None
    torque_on: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class HandReading:
    servos: dict                        # {sid: ServoReading}
    observed_at: float                  # 這一輪讀取完成的時間
    adapter: str

    @property
    def responding(self):
        return [sid for sid, r in self.servos.items() if r.ok]


def bus_lock_path(port):
    """同一個序列埠的檔案鎖路徑。servo_tool.py 也用這個，兩邊不會同時開同一條匯流排。"""
    real = os.path.realpath(port)
    name = os.path.basename(real)
    for prefix in ("cu.", "tty."):          # macOS：/dev/cu.X 與 /dev/tty.X 是同一個裝置
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "bus"
    return os.path.join(tempfile.gettempdir(), "this-is-yourx-bus-%s.lock" % name)


@contextlib.contextmanager
def bus_lock(port):
    path = bus_lock_path(port)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AdapterError("BUS_BUSY", "序列埠 %s 正被另一個程式使用（例如 finger_cal.sh）" % port)
        yield
    finally:
        os.close(fd)   # 關檔即釋放鎖


def _one(v):
    return v[0] if isinstance(v, (list, tuple)) else v


class ScsHandAdapter:
    """8 顆 SCS0009 經 USB 序列（rustypot）。每次 connected() 才開埠，離開就關，平常不佔著埠。"""

    kind = "scs"
    real_hardware = True
    BAUD = 1_000_000

    def __init__(self, port, timeout_s=0.1):
        self.port = port
        self.timeout_s = timeout_s
        self._c = None
        self._depth = 0
        self._lock = threading.RLock()
        self._stack = None

    @contextlib.contextmanager
    def connected(self):
        with self._lock:
            if self._depth == 0:
                stack = contextlib.ExitStack()
                stack.enter_context(bus_lock(self.port))
                try:
                    from rustypot import Scs0009PyController
                    self._c = Scs0009PyController(serial_port=self.port, baudrate=self.BAUD,
                                                  timeout=self.timeout_s)
                except AdapterError:
                    stack.close()
                    raise
                except Exception as e:
                    stack.close()
                    raise AdapterError("PORT_UNAVAILABLE", "打不開 %s：%s" % (self.port, e))
                self._stack = stack
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
                if self._depth == 0:
                    c, self._c = self._c, None
                    try:
                        if c is not None and hasattr(c, "close"):
                            c.close()
                    except Exception:
                        pass
                    self._stack.close()
                    self._stack = None

    def _bus(self):
        if self._c is None:
            raise AdapterError("PORT_UNAVAILABLE", "匯流排沒有開（要在 connected() 裡呼叫）")
        return self._c

    def read_servo(self, sid):
        c = self._bus()
        try:
            pos = math.degrees(_one(c.read_present_position(sid)))
            volt = _one(c.read_present_voltage(sid)) / 10.0
            temp = float(_one(c.read_present_temperature(sid)))
            torque = bool(_one(c.read_torque_enable(sid)))
        except Exception as e:
            return ServoReading(sid=sid, ok=False, observed_at=time.time(), error=str(e) or type(e).__name__)
        return ServoReading(sid=sid, ok=True, observed_at=time.time(), position_deg=pos,
                            voltage_v=volt, temperature_c=temp, torque_on=torque)

    def read_position(self, sid):
        try:
            return math.degrees(_one(self._bus().read_present_position(sid)))
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("SERVO_NO_RESPONSE", "ID %d 沒有回應：%s" % (sid, e))

    def read_voltage(self, sid):
        try:
            return _one(self._bus().read_present_voltage(sid)) / 10.0
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("SERVO_NO_RESPONSE", "ID %d 沒有回應：%s" % (sid, e))

    def set_speed(self, sid, rad_s):
        self._bus().write_goal_speed(sid, rad_s)

    def set_goal(self, sid, deg):
        self._bus().write_goal_position(sid, math.radians(deg))

    def set_torque(self, sid, on):
        self._bus().write_torque_enable(sid, 1 if on else 0)


class FakeHandAdapter:
    """模擬的 8 顆伺服機。不是物理模型：有扭力時目標一寫入就到位，除非該顆設了 travel（模擬卡住）。

    可注入的故障：
      missing     不回應的 ID 集合（斷電、斷線、掉一顆）
      travel      {sid: (最小度, 最大度)}，位置只能在這個範圍（手指被擋住）
      voltage_v / temperature_c   全部或 {sid: 值}
      after_goals [(第幾次寫目標之後, 函式(adapter))]，用來在動作途中改狀態（例如電壓掉落、拔線）
      stale_s     讀值的 observed_at 往回推這麼多秒（模擬讀到舊資料）
    """

    kind = "fake"
    real_hardware = False

    def __init__(self, voltage_v=5.0, temperature_c=30.0, missing=(), travel=None, stale_s=0.0,
                 clock=time.time, positions=None):
        self.voltage_v = voltage_v
        self.temperature_c = temperature_c
        self.missing = set(missing)
        self.travel = dict(travel or {})
        self.stale_s = stale_s
        self.clock = clock
        self.positions = {sid: 0.0 for sid in SERVO_IDS}
        if positions:
            self.positions.update(positions)
        self.goals = dict(self.positions)
        self.torque = {sid: False for sid in SERVO_IDS}
        self.speed = {sid: None for sid in SERVO_IDS}
        self.goal_history = {sid: [] for sid in SERVO_IDS}
        self.torque_history = {sid: [] for sid in SERVO_IDS}
        self.after_goals = []
        self.goal_count = 0
        self.connect_count = 0
        self.busy = False            # True 時 connected() 丟 BUS_BUSY

    @contextlib.contextmanager
    def connected(self):
        if self.busy:
            raise AdapterError("BUS_BUSY", "模擬：匯流排被占用")
        self.connect_count += 1
        yield self

    def _value(self, v, sid):
        return v.get(sid) if isinstance(v, dict) else v

    def _check(self, sid):
        if sid in self.missing:
            raise AdapterError("SERVO_NO_RESPONSE", "模擬：ID %d 沒有回應" % sid)

    def read_servo(self, sid):
        now = self.clock() - self.stale_s
        if sid in self.missing:
            return ServoReading(sid=sid, ok=False, observed_at=now, error="no response")
        return ServoReading(sid=sid, ok=True, observed_at=now, position_deg=self.positions[sid],
                            voltage_v=self._value(self.voltage_v, sid),
                            temperature_c=self._value(self.temperature_c, sid),
                            torque_on=self.torque[sid])

    def read_position(self, sid):
        self._check(sid)
        return self.positions[sid]

    def read_voltage(self, sid):
        self._check(sid)
        return self._value(self.voltage_v, sid)

    def set_speed(self, sid, rad_s):
        self._check(sid)
        self.speed[sid] = rad_s

    def set_goal(self, sid, deg):
        self._check(sid)
        self.goals[sid] = deg
        self.goal_history[sid].append(deg)
        if self.torque[sid]:
            lo, hi = self.travel.get(sid, (-180.0, 180.0))
            self.positions[sid] = max(lo, min(hi, deg))
        self.goal_count += 1
        for n, fn in list(self.after_goals):
            if self.goal_count == n:
                fn(self)

    def set_torque(self, sid, on):
        self._check(sid)
        self.torque[sid] = bool(on)
        self.torque_history[sid].append(bool(on))
        if on:
            lo, hi = self.travel.get(sid, (-180.0, 180.0))
            self.positions[sid] = max(lo, min(hi, self.goals[sid]))
