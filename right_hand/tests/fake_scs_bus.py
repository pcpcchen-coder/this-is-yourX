"""假的 Feetech SCS 匯流排：在 pty 上模擬一到多顆 SCS0009。

只實作 bring-up 工具用得到的部分：PING、READ、WRITE，以及「EPROM 區在 lock=1
時寫入不會保存」這件事。它不是伺服機的物理模型：目標位置一寫入就當成已到位，
除非該顆被設成 `stuck`。
"""
import os
import pty
import select
import threading
import tty

EPROM_END = 40          # 位址 < 40 是 EPROM 區
ADDR_ID = 5
ADDR_TORQUE = 40
ADDR_GOAL = 42
ADDR_LOCK = 48
ADDR_POS = 56
ADDR_VOLT = 62
ADDR_TEMP = 63


class FakeServo:
    def __init__(self, sid, volt_dv=50, temp_c=31, stuck=False):
        m = bytearray(128)
        m[3], m[4] = 0x05, 0x04             # model number 1284，big-endian
        m[ADDR_ID] = sid
        m[ADDR_LOCK] = 1
        m[ADDR_VOLT] = volt_dv              # 單位 0.1 V
        m[ADDR_TEMP] = temp_c
        m[ADDR_POS], m[ADDR_POS + 1] = 0x01, 0xFF   # 511 = 中位
        self.mem = m
        self.saved = bytes(m[:EPROM_END])   # 斷電後會留下來的內容
        self.stuck = stuck
        self.torque_history = []

    @property
    def sid(self):
        return self.mem[ADDR_ID]

    def write(self, addr, data):
        for j, b in enumerate(data):
            a = addr + j
            self.mem[a] = b
            if a < EPROM_END and self.mem[ADDR_LOCK] == 0:
                saved = bytearray(self.saved)
                saved[a] = b
                self.saved = bytes(saved)
            if a == ADDR_TORQUE:
                self.torque_history.append(b)
        if addr == ADDR_GOAL and not self.stuck:
            self.mem[ADDR_POS] = self.mem[ADDR_GOAL]
            self.mem[ADDR_POS + 1] = self.mem[ADDR_GOAL + 1]

    def power_cycle(self):
        self.mem[:EPROM_END] = self.saved
        self.mem[ADDR_TORQUE] = 0
        self.mem[ADDR_LOCK] = 1


class FakeBus:
    """用法：`with FakeBus([FakeServo(1)]) as bus: ... bus.port ...`"""

    def __init__(self, servos, echo=False):
        self.servos = list(servos)
        self.echo = echo            # 模擬半雙工板子把送出的位元組回傳
        self.writes = []            # (當下的 id, addr, [bytes])，依時間順序
        self._stop = threading.Event()

    def __enter__(self):
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        self.port = os.ttyname(self._slave)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1)
        os.close(self._master)
        os.close(self._slave)

    def _find(self, sid):
        for s in self.servos:
            if s.sid == sid:
                return s
        return None

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            ready, _, _ = select.select([self._master], [], [], 0.05)
            if ready:
                try:
                    buf += os.read(self._master, 256)
                except OSError:
                    return
            while True:
                i = buf.find(b"\xff\xff")
                if i < 0 or len(buf) < i + 4:
                    break
                sid, length = buf[i + 2], buf[i + 3]
                if len(buf) < i + 4 + length:
                    break
                pkt, buf = buf[i:i + 4 + length], buf[i + 4 + length:]
                inst, params = pkt[4], pkt[5:-1]
                if self.echo:
                    os.write(self._master, pkt)
                if sid == 0xFE and inst == 0x01:      # 廣播 PING：每顆都回
                    for sv in self.servos:
                        body = bytes([sv.sid, 2, 0])
                        os.write(self._master, b"\xff\xff" + body + bytes([(~sum(body)) & 0xFF]))
                    continue
                servo = self._find(sid)
                if servo is None:
                    continue
                data = b""
                if inst == 0x02:                      # READ
                    addr, n = params[0], params[1]
                    data = bytes(servo.mem[addr:addr + n])
                elif inst == 0x03:                    # WRITE
                    self.writes.append((sid, params[0], list(params[1:])))
                    servo.write(params[0], params[1:])
                elif inst != 0x01:                    # 其他指令不回應
                    continue
                body = bytes([sid, len(data) + 2, 0]) + data
                os.write(self._master, b"\xff\xff" + body + bytes([(~sum(body)) & 0xFF]))
