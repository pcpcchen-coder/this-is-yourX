#!/usr/bin/env python3
"""Amazing Hand 右手：SCS0009 單軸 bring-up 工具（人工操作）。

這支工具給「人」在工作台上逐顆檢查伺服機用，取代只有 Windows 版的 Feetech
除錯軟體。它不是 AI 控制路徑的一部分：依本 repo 的架構規則，生成式模型不得
直接下馬達命令，之後的動作一律經 versioned skill 與 Safety Gateway。

用法：
  python servo_tool.py ports                 列出序列埠
  python servo_tool.py scan  PORT            掃描匯流排上的伺服機（只讀）
  python servo_tool.py diag  PORT            掃不到時用：送 ping 並印出收到的原始位元組（只讀）
  python servo_tool.py test  PORT ID         讀狀態並小幅擺動一次（單顆、低速）
  python servo_tool.py setid PORT OLD NEW    改 ID（匯流排上只能接一顆）
  python servo_tool.py center PORT A B [MID_A MID_B]
                                             一根手指的兩顆回中位並保持扭力，用來裝舵盤
  python servo_tool.py finger PORT A B [MID_A MID_B]
                                             一根手指分段開合一次，用來微調中位

`test` 只用在還沒裝進手指、出力軸沒有負載的伺服機上。
`center`、`finger` 一次只接一根手指的兩顆：A 是奇數 ID、B 是 A+1。MID 是中位修正，
單位是度，預設 0。
"""
import glob
import math
import sys
import time

BAUD = 1_000_000
SCAN_IDS = list(range(0, 21))
MODEL_SCS0009 = 1284

# SCS0009 的工作電壓範圍；超出就不動作。
VOLT_MIN, VOLT_MAX = 4.0, 7.4
TEMP_MAX_C = 60
WIGGLE_DEG = (0, 20, -20, 0)
WIGGLE_SPEED = 3
WIGGLE_TOLERANCE_DEG = 5.0
WIGGLE_SETTLE_S = 3.0        # 每一步最多等這麼久讓伺服機到位

# 手指校正。奇數 ID 與偶數 ID 鏡像安裝，所以同一個動作兩顆角度正負相反。
FINGER_PAIRS = ((1, 2), (3, 4), (5, 6), (7, 8))
MID_LIMIT_DEG = 30.0         # 中位修正超過這個值，多半是舵盤裝錯齒，不是該用修正補的
CENTER_SPEED = 3
FINGER_SPEED = 1.5           # 帶著手指動，比無負載測試慢一半
FINGER_TOLERANCE_DEG = 8.0
FINGER_SETTLE_S = 3.0
FINGER_OPEN_DEG = -30        # 奇數 ID 的角度；偶數 ID 取負號
FINGER_CLOSE_STEPS_DEG = (0, 30, 60, 90)


def open_bus(port):
    from rustypot import Scs0009PyController

    return Scs0009PyController(serial_port=port, baudrate=BAUD, timeout=0.5)


def one(v):
    """rustypot 有些版本回傳單值，有些回傳 list。"""
    return v[0] if isinstance(v, (list, tuple)) else v


def scan(c):
    found = c.scan(SCAN_IDS) if hasattr(c, "scan") else {}
    if not found:
        # rustypot 的 scan 用很短的逾時；USB 轉序列埠有延遲時可能漏掉，改用較長逾時逐一 ping。
        if hasattr(c, "set_timeout"):
            c.set_timeout(0.05)
        for i in SCAN_IDS:
            try:
                if c.ping(i):
                    found[i] = one(c.read_model_number(i))
            except Exception:
                pass
        if hasattr(c, "set_timeout"):
            c.set_timeout(0.5)
    return dict(sorted(found.items()))


def cmd_ports():
    patterns = ("/dev/cu.usb*", "/dev/cu.wch*", "/dev/ttyACM*", "/dev/ttyUSB*")
    ports = sorted({p for pat in patterns for p in glob.glob(pat)})
    if not ports:
        print("找不到 USB 序列埠。確認驅動板的 USB-C 已接上電腦。")
        return 1
    for p in ports:
        print(p)
    return 0


def cmd_scan(port):
    c = open_bus(port)
    found = scan(c)
    if not found:
        print("沒有伺服機回應。檢查：變壓器有沒有插、3-pin 線有沒有插到底、板子跳線模式。")
        return 1
    for i, model in found.items():
        tag = "SCS0009" if model == MODEL_SCS0009 else "型號碼 %d" % model
        print("ID %d  %s" % (i, tag))
    return 0


def _ping_packet(sid):
    body = bytes([sid, 0x02, 0x01])                 # ID、長度、PING
    return b"\xff\xff" + body + bytes([(~sum(body)) & 0xFF])


def _hex(b):
    return " ".join("%02X" % x for x in b) or "（無）"


def classify_reply(sent, received):
    """回傳 (有無回音, 回應的伺服機 ID 或 None)。"""
    echo = received.startswith(sent)
    rest = received[len(sent):] if echo else received
    i = rest.find(b"\xff\xff")
    if i >= 0 and len(rest) >= i + 6:
        sid, length = rest[i + 2], rest[i + 3]
        frame = rest[i + 2:i + 4 + length]
        if length == 2 and (~sum(frame[:-1])) & 0xFF == frame[-1]:
            return echo, sid
    return echo, None


def cmd_diag(port):
    """只送 PING（不寫入、不開扭力），把收到的位元組原樣印出來。"""
    import serial

    seen_any = seen_echo = False
    answered = {}
    for baud in (1_000_000, 500_000, 115_200):
        with serial.Serial(port, baud, timeout=0.3) as s:
            for label, sid in (("ID 1", 1), ("廣播", 0xFE)):
                pkt = _ping_packet(sid)
                s.reset_input_buffer()
                s.write(pkt)
                s.flush()
                time.sleep(0.05)
                rx = s.read(64)
                echo, who = classify_reply(pkt, rx)
                seen_any = seen_any or bool(rx)
                seen_echo = seen_echo or echo
                if who is not None:
                    answered[baud] = who
                print("%7d baud  %s  送 %s  收 %s%s%s" % (
                    baud, label, _hex(pkt), _hex(rx),
                    "  ← 含回音" if echo else "",
                    "  ← 伺服機 ID %d 回應" % who if who is not None else ""))
    print()
    if answered:
        for baud, who in answered.items():
            print("結論：伺服機有回應，ID %d，鮑率 %d。" % (who, baud))
        if seen_echo:
            print("另外板子會把送出的資料回傳（回音），掃描工具可能因此誤判，請把這份輸出貼回來。")
        return 0
    if seen_echo:
        print("結論：只收到自己送出的資料，伺服機沒有回應。")
        print("　　　電腦到板子是通的；問題在伺服機這一側：沒供電、3-pin 線沒插到底或插反、伺服機故障。")
    elif seen_any:
        print("結論：收到雜訊但不是有效回應。請把這份輸出貼回來。")
    else:
        print("結論：完全沒有資料回來。")
        print("　　　可能是：變壓器沒插（伺服機沒電）、3-pin 線沒插到底、板子不在 USB 模式、伺服機故障。")
    return 1


def cmd_test(port, sid):
    c = open_bus(port)
    found = scan(c)
    if sid not in found:
        print("ID %d 沒有回應。" % sid)
        return 1
    if len(found) != 1:
        print("匯流排上看到 %d 顆（%s）。單軸測試一次只接一顆。" % (len(found), list(found)))
        return 1
    volt = one(c.read_present_voltage(sid)) / 10.0
    temp = one(c.read_present_temperature(sid))
    pos = math.degrees(one(c.read_present_position(sid)))
    print("ID %d  電壓 %.1f V  溫度 %d °C  目前位置 %.1f°" % (sid, volt, temp, pos))
    if not (VOLT_MIN <= volt <= VOLT_MAX):
        print("電壓不在 %.1f–%.1f V 範圍內，不動作。先檢查電源。" % (VOLT_MIN, VOLT_MAX))
        return 1
    if temp > TEMP_MAX_C:
        print("溫度超過 %d °C，不動作。" % TEMP_MAX_C)
        return 1

    print("開扭力，擺動 0° → +20° → -20° → 0° …")
    ok = True
    try:
        c.write_torque_enable(sid, 1)
        c.write_goal_speed(sid, WIGGLE_SPEED)
        for target in WIGGLE_DEG:
            c.write_goal_position(sid, math.radians(target))
            # 起始位置可能離中位很遠，所以輪詢到位，不用固定等待時間。
            for _ in range(int(WIGGLE_SETTLE_S / 0.1)):
                time.sleep(0.1)
                now = math.degrees(one(c.read_present_position(sid)))
                if abs(now - target) < WIGGLE_TOLERANCE_DEG:
                    break
            err = abs(now - target)
            good = err < WIGGLE_TOLERANCE_DEG
            print("  目標 %+4d°  實際 %+6.1f°  %s" % (target, now, "OK" if good else "偏差大"))
            ok = ok and good
    finally:
        # 不論成功、例外或 Ctrl-C，離開前一定關扭力。
        try:
            c.write_torque_enable(sid, 0)
        except Exception as e:
            print("警告：關扭力失敗（%s）。請直接拔掉變壓器。" % e)
            ok = False
    print("結果：" + ("正常" if ok else "異常，這顆先放旁邊別裝"))
    return 0 if ok else 1


def wait_enter(msg):
    print(msg, flush=True)
    input()


def _pair_ok(a, b, mid_a, mid_b):
    if (a, b) not in FINGER_PAIRS:
        print("ID 要是同一根手指的一對：1 2、3 4、5 6 或 7 8（奇數在前）。")
        return False
    if abs(mid_a) > MID_LIMIT_DEG or abs(mid_b) > MID_LIMIT_DEG:
        print("中位修正要在 ±%d° 以內。超過的話先把舵盤拆下來換一齒重裝。" % MID_LIMIT_DEG)
        return False
    return True


def _open_pair(port, a, b):
    """確認匯流排上恰好是這兩顆，而且電壓、溫度正常。不符合就回傳 None，不開扭力。"""
    c = open_bus(port)
    found = scan(c)
    if sorted(found) != [a, b]:
        print("匯流排上看到 %s，預期恰好是 [%d, %d]。一次只接一根手指的兩顆。" % (list(found), a, b))
        return None
    for sid in (a, b):
        volt = one(c.read_present_voltage(sid)) / 10.0
        temp = one(c.read_present_temperature(sid))
        print("ID %d  電壓 %.1f V  溫度 %d °C" % (sid, volt, temp))
        if not (VOLT_MIN <= volt <= VOLT_MAX):
            print("電壓不在 %.1f–%.1f V 範圍內，不動作。先檢查電源。" % (VOLT_MIN, VOLT_MAX))
            return None
        if temp > TEMP_MAX_C:
            print("溫度超過 %d °C，不動作。" % TEMP_MAX_C)
            return None
    return c


def _move_pair(c, a, b, deg_a, deg_b, tolerance):
    """兩顆一起走到目標，輪詢到位。回傳 (是否都到位, a 的實際角度, b 的實際角度)。"""
    c.write_goal_position(a, math.radians(deg_a))
    c.write_goal_position(b, math.radians(deg_b))
    now_a = now_b = None
    for _ in range(int(FINGER_SETTLE_S / 0.1)):
        time.sleep(0.1)
        now_a = math.degrees(one(c.read_present_position(a)))
        now_b = math.degrees(one(c.read_present_position(b)))
        if abs(now_a - deg_a) < tolerance and abs(now_b - deg_b) < tolerance:
            return True, now_a, now_b
    return False, now_a, now_b


def _torque_off(c, ids):
    ok = True
    for sid in ids:
        try:
            c.write_torque_enable(sid, 0)
        except Exception as e:
            print("警告：ID %d 關扭力失敗（%s）。請直接拔掉變壓器。" % (sid, e))
            ok = False
    return ok


def cmd_center(port, a, b, mid_a=0.0, mid_b=0.0):
    """兩顆回到中位並保持扭力，讓人把舵盤裝上去；按 Enter 後關扭力。"""
    if not _pair_ok(a, b, mid_a, mid_b):
        return 1
    c = _open_pair(port, a, b)
    if c is None:
        return 1
    print("開扭力，ID %d → %+.1f°，ID %d → %+.1f° …" % (a, mid_a, b, mid_b))
    ok = False
    try:
        c.write_torque_enable(a, 1)
        c.write_torque_enable(b, 1)
        c.write_goal_speed(a, CENTER_SPEED)
        c.write_goal_speed(b, CENTER_SPEED)
        ok, now_a, now_b = _move_pair(c, a, b, mid_a, mid_b, WIGGLE_TOLERANCE_DEG)
        print("  ID %d 實際 %+6.1f°   ID %d 實際 %+6.1f°   %s" % (a, now_a, b, now_b, "OK" if ok else "沒到位"))
        if ok:
            wait_enter("兩顆已在中位並保持扭力。現在裝舵盤；裝好後按 Enter 關扭力。")
    finally:
        ok = _torque_off(c, (a, b)) and ok
    print("結果：" + ("完成，扭力已關" if ok else "異常，沒有到中位"))
    return 0 if ok else 1


def cmd_finger(port, a, b, mid_a=0.0, mid_b=0.0):
    """一根手指分段開合一次：張開 → 中位 → 分段閉合（停住等人看）→ 張開 → 中位。"""
    if not _pair_ok(a, b, mid_a, mid_b):
        return 1
    c = _open_pair(port, a, b)
    if c is None:
        return 1
    wait_enter("手指周圍淨空、手不要放在指節之間。按 Enter 開始，Ctrl-C 隨時中止。")
    ok = True
    try:
        c.write_torque_enable(a, 1)
        c.write_torque_enable(b, 1)
        c.write_goal_speed(a, FINGER_SPEED)
        c.write_goal_speed(b, FINGER_SPEED)
        plan = [("張開", FINGER_OPEN_DEG)]
        plan += [("閉合 %d°" % d if d else "中位", d) for d in FINGER_CLOSE_STEPS_DEG]
        plan += [("張開", FINGER_OPEN_DEG), ("中位", 0)]
        for label, d in plan:
            reached, now_a, now_b = _move_pair(c, a, b, mid_a + d, mid_b - d, FINGER_TOLERANCE_DEG)
            print("  %-8s ID %d 目標 %+6.1f° 實際 %+6.1f°   ID %d 目標 %+6.1f° 實際 %+6.1f°   %s" % (
                label, a, mid_a + d, now_a, b, mid_b - d, now_b, "OK" if reached else "卡住"))
            if not reached:
                print("沒有到位，立刻關扭力。檢查連桿、舵盤有沒有互卡，或手指有沒有頂到東西。")
                ok = False
                break
            if d == FINGER_CLOSE_STEPS_DEG[-1]:
                wait_enter("手指已閉合。看兩個舵盤的耳朵有沒有對齊伺服機中線，看完按 Enter 張開。")
    finally:
        ok = _torque_off(c, (a, b)) and ok
    print("結果：" + ("完成，扭力已關" if ok else "異常，扭力已關"))
    return 0 if ok else 1


def cmd_setid(port, old, new):
    if not (1 <= new <= 253):
        print("新 ID 要在 1–253 之間。")
        return 1
    c = open_bus(port)
    found = scan(c)
    if len(found) != 1:
        print("匯流排上看到 %d 顆（%s）。改 ID 時只能接一顆。" % (len(found), list(found)))
        return 1
    if old not in found:
        print("接著的這顆是 ID %d，不是 %d。" % (list(found)[0], old))
        return 1
    if old == new:
        print("已經是 ID %d。" % new)
        return 0
    c.write_lock(old, False)  # 先解鎖 EPROM，改的 ID 才會斷電保存
    time.sleep(0.05)
    try:
        c.write_id(old, new)
    except Exception as e:  # 有些韌體改完會用新 ID 回應，這裡不當成失敗
        print("（寫入回應異常：%s，接著用掃描確認）" % e)
    time.sleep(0.1)
    try:
        c.write_lock(new, True)  # 鎖回去
    except Exception as e:
        print("（鎖定回應異常：%s）" % e)
    time.sleep(0.1)
    after = scan(c)
    if list(after) == [new]:
        print("完成：ID %d → %d。請斷電重插後再 scan 一次，確認 ID 有保存。" % (old, new))
        return 0
    print("失敗：現在掃到 %s。" % list(after))
    return 1


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[1]
    try:
        if cmd == "ports" and len(argv) == 2:
            return cmd_ports()
        if cmd == "scan" and len(argv) == 3:
            return cmd_scan(argv[2])
        if cmd == "diag" and len(argv) == 3:
            return cmd_diag(argv[2])
        if cmd == "test" and len(argv) == 4:
            return cmd_test(argv[2], int(argv[3]))
        if cmd == "setid" and len(argv) == 5:
            return cmd_setid(argv[2], int(argv[3]), int(argv[4]))
        if cmd in ("center", "finger") and len(argv) in (5, 7):
            mids = [float(x) for x in argv[5:7]] if len(argv) == 7 else [0.0, 0.0]
            fn = cmd_center if cmd == "center" else cmd_finger
            return fn(argv[2], int(argv[3]), int(argv[4]), *mids)
    except KeyboardInterrupt:
        print("\n已中止。")
        return 130
    except Exception as e:
        print("錯誤：%s" % e)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
