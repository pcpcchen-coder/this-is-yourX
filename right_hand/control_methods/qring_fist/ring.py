"""QRing R08 的藍牙連線：三軸加速度快照輪詢、觸控事件、電量。

協定沿用 mac-app/ 已在實機驗證過的部分（R08_E703 / RT08_V3.1 / RT08_3.10.48_260309）：
  - 寫入 6e400002、通知 6e400003；封包 16 bytes，最後一個 byte 是前 15 個的加總 mod 256。
  - A1 03：要求一筆三軸快照，回報的 bytes 2..7 是三個 big-endian signed int16。A1 00：停止。
  - 3B：觸控設定。先讀回原設定，結束時還原。73 2D：觸控事件（1 上滑、2 下滑、3 單擊、4 長按）。
  - 03：電量與充電狀態。
本程式不啟動任何光學量測，也不改戒指的內建排程。
"""
import asyncio
import struct
import time
from collections import deque

WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
GATT_FIELDS = (("hardware", "00002a27-0000-1000-8000-00805f9b34fb"),
               ("firmware", "00002a26-0000-1000-8000-00805f9b34fb"),
               ("serial", "00002a25-0000-1000-8000-00805f9b34fb"))
GESTURES = {1: "上滑", 2: "下滑", 3: "單擊", 4: "長按"}
SAMPLE_WAIT_S = 0.3
BATTERY_PERIOD_S = 120.0


def pack(op, payload=b""):
    if len(payload) > 14:
        raise ValueError("Payload too long")
    frame = (bytes([op]) + payload).ljust(15, b"\0")
    return frame + bytes([sum(frame) & 255])


class BleakTransport:
    """真的藍牙連線（macOS / CoreBluetooth）。只在這裡 import bleak 與 pyobjc。"""

    def __init__(self, name):
        self.name = name
        self.client = None

    async def _find(self):
        from bleak import BleakScanner
        from bleak.backends.device import BLEDevice
        from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
        from CoreBluetooth import CBUUID
        manager = CentralManagerDelegate.alloc().init()
        ready = getattr(manager, "wait_until_ready", None)
        if ready is not None:               # 等藍牙就緒；沒有權限或藍牙關著時在這裡就知道
            try:
                await asyncio.wait_for(ready(), 10)
            except asyncio.TimeoutError:
                raise RuntimeError("藍牙沒有回應。請確認這台 Mac 的藍牙已開啟")
            except Exception as e:
                raise RuntimeError("藍牙無法使用（%s）。請開啟藍牙，並在系統設定的「隱私權與安全性 → 藍牙」允許 QRing Hand" % e)
        peripherals = manager.central_manager.retrieveConnectedPeripheralsWithServices_(
            [CBUUID.UUIDWithString_("1812"), CBUUID.UUIDWithString_("6e40fff0-b5a3-f393-e0a9-e50e24dcca9e")])
        for p in peripherals:
            if str(p.name()) == self.name:
                return BLEDevice(str(p.identifier().UUIDString()), self.name, (p, manager))
        return await BleakScanner.find_device_by_filter(
            lambda d, a: (a.local_name or d.name) == self.name, timeout=15)

    async def open(self, on_frame):
        from bleak import BleakClient
        device = await self._find()
        if device is None:
            raise RuntimeError("找不到戒指 %s。請戴好戒指，並讓手機的 QRing App 斷線" % self.name)
        client = BleakClient(device, timeout=20)
        self.client = client
        await client.connect()
        await client.start_notify(NOTIFY, lambda _, data: on_frame(bytes(data)))
        info = {}
        for key, uuid in GATT_FIELDS:
            try:
                info[key] = bytes(await client.read_gatt_char(uuid)).decode(errors="replace").strip("\0 ")
            except Exception:
                info[key] = ""
        return info

    @property
    def connected(self):
        return bool(self.client and self.client.is_connected)

    async def write(self, frame):
        await self.client.write_gatt_char(WRITE, frame, response=False)

    async def close(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


class RingLink:
    def __init__(self, name="R08_E703", transport_factory=None, on_accel=None, on_gesture=None,
                 on_disconnect=None, log=None, monotonic=time.monotonic):
        self.name = name
        self.transport_factory = transport_factory or (lambda: BleakTransport(name))
        self.on_accel = on_accel or (lambda raw, now: None)
        self.on_gesture = on_gesture or (lambda code, now: None)
        self.on_disconnect = on_disconnect or (lambda expected: None)
        self.log = log or (lambda *a, **k: None)
        self.monotonic = monotonic
        self.transport = None
        self.task = None
        self.stop = asyncio.Event()
        self.lock = asyncio.Lock()
        self.waiter = None
        self.sample = asyncio.Event()
        self.want_accel = False
        self.want_touch = True
        self.accel_interval = 0.06
        self.touch_original = None
        self.touch_on = False
        self.times = deque(maxlen=64)
        self.state = {"connected": False, "busy": False, "message": "尚未連線", "name": name, "hardware": None,
                      "firmware": None, "serial": None, "battery": None, "charging": None,
                      "raw": None, "magnitude": None, "gesture": None, "touch": False, "frames": 0, "bad_frames": 0}
        self.last_sample = 0.0

    # ------------------------------------------------------------------ 狀態
    def snapshot(self):
        now = self.monotonic()
        recent = [t for t in self.times if now - t <= 3.0]
        rate = (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) >= 2 and recent[-1] > recent[0] else 0.0
        age = now - self.last_sample if self.last_sample else None
        return dict(self.state, rate_hz=round(rate, 1), sample_age=None if age is None else round(age, 2),
                    sampling=self.want_accel)

    def fresh(self, limit=1.0):
        return bool(self.last_sample) and self.monotonic() - self.last_sample <= limit

    # ------------------------------------------------------------------ 封包
    def _frame(self, f):
        self.state["frames"] += 1
        if len(f) != 16 or sum(f[:15]) & 255 != f[15]:
            self.state["bad_frames"] += 1
            return
        op = f[0]
        waiter = self.waiter
        if waiter and not waiter[1].done() and op in (waiter[0], waiter[0] | 128):
            waiter[1].set_result(f)
        if op == 3 and f[1] <= 100:
            self.state.update(battery=f[1], charging=bool(f[2]))
        elif op == 0xA1 and f[1] == 3:
            raw = list(struct.unpack(">hhh", f[2:8]))
            now = self.monotonic()
            self.last_sample = now
            self.times.append(now)
            self.state["raw"] = raw
            self.state["magnitude"] = round((raw[0] ** 2 + raw[1] ** 2 + raw[2] ** 2) ** 0.5 / 8192, 3)
            self.sample.set()
            self.on_accel(raw, now)
        elif op == 0x73 and f[1] == 0x2D:
            code = f[2]
            now = self.monotonic()
            self.state["gesture"] = {"code": code, "name": GESTURES.get(code, "未知"), "at": time.time()}
            self.on_gesture(code, now)

    async def send(self, op, payload=b"", reply=True, timeout=2.0):
        async with self.lock:
            if not self.transport or not self.transport.connected:
                raise RuntimeError("戒指未連線")
            future = asyncio.get_running_loop().create_future()
            self.waiter = (op, future) if reply else None
            try:
                await self.transport.write(pack(op, payload))
                if not reply:
                    return None
                try:
                    return await asyncio.wait_for(future, timeout)
                except asyncio.TimeoutError:
                    return None
            finally:
                self.waiter = None

    # ------------------------------------------------------------------ 觸控設定（備份、啟用、還原）
    async def _touch(self, enable):
        if enable == self.touch_on:
            return
        if enable:
            if not self.touch_original:
                f = await self.send(0x3B, b"\x01\x00")
                if f is None or f[0] != 0x3B or f[1] != 1:
                    raise RuntimeError("讀不到戒指的觸控設定，沒有更動")
                self.touch_original = f
            a = await self.send(0x3B, b"\x01\x00\x01\x01")
            await asyncio.sleep(0.5)
            b = await self.send(0x3B, b"\x02\x00\x09\x01")
            if a is None or b is None or a[0] != 0x3B or b[0] != 0x3B:
                raise RuntimeError("戒指沒有確認觸控回報")
        elif self.touch_original:
            f = self.touch_original
            await self.send(0x3B, bytes([2, 0, f[3], f[4]]))
            if f[2]:
                await self.send(0x3B, b"\x01\x00\x01\x00")
            after = await self.send(0x3B, b"\x01\x00")
            self.log("ring", "觸控設定已還原" if after is not None and after[1:5] == f[1:5]
                     else "觸控設定還原後讀回的內容與原本不同")
        self.touch_on = enable
        self.state["touch"] = enable

    # ------------------------------------------------------------------ 連線
    async def connect(self):
        if self.task and not self.task.done():
            return
        self.stop.clear()
        self.state.update(busy=True, message="正在尋找戒指…")
        self.task = asyncio.ensure_future(self._run())

    async def disconnect(self):
        self.stop.set()
        if self.task and not self.task.done():
            await self.task

    async def _run(self):
        expected = False
        transport = self.transport_factory()
        self.transport = transport
        try:
            info = await transport.open(self._frame)
            self.state.update(info)
            await asyncio.sleep(0.5)
            await self.send(3)
            self.state.update(connected=True, busy=False, message="已連線")
            self.log("ring", "戒指已連線", hardware=info.get("hardware"), firmware=info.get("firmware"))
            await self._poll()
            expected = self.stop.is_set()
            self.state["message"] = "已中斷" if expected else "戒指連線中斷"
        except Exception as e:
            detail = str(e) or type(e).__name__
            if self.state["connected"]:
                self.state["message"] = "戒指連線中斷（%s）" % detail
            else:
                self.state["message"] = detail
                self.log("ring", "戒指連線失敗：%s" % detail)
        finally:
            was = self.state["connected"]
            try:
                if transport.connected:
                    try:
                        await self.send(0xA1, b"\x00", reply=False)
                        await asyncio.sleep(0.2)
                        if self.touch_on:
                            await self._touch(False)
                    except Exception as e:
                        self.log("ring", "還原戒指設定時出錯：%s" % e)
            finally:
                await transport.close()
                self.transport = None
                self.touch_original = None
                self.touch_on = False
                self.last_sample = 0.0
                self.times.clear()
                self.state.update(connected=False, busy=False, touch=False, raw=None, magnitude=None)
                if was:
                    self.log("ring", self.state["message"])
                    self.on_disconnect(expected)

    async def _poll(self):
        had_accel = False
        touch_failed = False
        next_battery = self.monotonic() + BATTERY_PERIOD_S
        while not self.stop.is_set() and self.transport.connected:
            if self.want_touch != self.touch_on and not (self.want_touch and touch_failed):
                try:
                    await self._touch(self.want_touch)
                except RuntimeError as e:
                    touch_failed = True
                    self.log("ring", "%s；戒指單擊／長按無法使用" % e)
            if self.monotonic() >= next_battery:
                await self.send(3, timeout=1.0)
                next_battery = self.monotonic() + BATTERY_PERIOD_S
            if self.want_accel:
                self.sample.clear()
                await self.send(0xA1, b"\x03", reply=False)
                try:
                    await asyncio.wait_for(self.sample.wait(), SAMPLE_WAIT_S)
                except asyncio.TimeoutError:
                    pass
                had_accel = True
                await self._sleep(self.accel_interval)
            else:
                if had_accel:
                    await self.send(0xA1, b"\x00", reply=False)
                    had_accel = False
                await self._sleep(0.2)

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass
