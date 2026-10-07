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
  python servo_tool.py hand   PORT [MID_1 … MID_8]
                                             全手：8 顆都接上，四根手指輪流開合一次
  python servo_tool.py gesture PORT NAME [MID_1 … MID_8]
                                             全手比一個固定手勢、停住、再張開。NAME 目前只有 ok。
                                             四根手指同時動：每一輪每顆只前進一小段

`test` 只用在還沒裝進手指、出力軸沒有負載的伺服機上。
`center`、`finger` 一次只接一根手指的兩顆：A 是奇數 ID、B 是 A+1。MID 是中位修正，
單位是度，預設 0。`hand` 要 ID 1–8 全部在線；一次只動一根手指，其餘保持張開。
`gesture` 的姿態是寫死在這個檔案裡的固定表，不接受任意角度；8 顆同時動，所以每一輪
都檢查有沒有哪一顆跟不上、電壓有沒有掉。
執行 `hand`、`gesture` 時，人要能隨手切斷伺服機電源（例如變壓器接在有開關的延長線上）。
"""
import glob
import math
import select
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
STILL_DEG = 0.5              # 連續兩次讀值差小於這個值，視為已停下
FINGER_OPEN_DEG = -30        # 奇數 ID 的角度；偶數 ID 取負號
FINGER_CLOSE_STEPS_DEG = (0, 30, 60, 90)
HAND_IDS = tuple(range(1, 9))
FINGER_NAMES = {1: "食指", 3: "中指", 5: "無名指", 7: "拇指"}

# 固定手勢（右手）。key 是每根手指的奇數 ID，值是 (奇數 ID 角度, 偶數 ID 角度)，單位度，
# 不含中位修正。兩顆角度不對稱時手指會同時彎曲並側擺。四根手指是同時動的；order 只決定
# 每一輪裡下指令的先後，以及到位後逐指確認、列印的順序（收回時反過來）。
SERVO_LIMIT_DEG = 95.0       # 任何一顆的目標（含中位修正）都不得超出 ±95°
HOLD_MAX_S = 30              # 手勢最多停這麼久，沒按 Enter 也會自己收回
# 多根手指同時動：把路徑切成很多輪，每一輪每顆只前進一小段，所有顆在最後一輪同時到位。
TOGETHER_STEP_DEG = 3.0      # 走最遠的那顆每一輪前進這麼多，其餘按比例
TOGETHER_PERIOD_S = 0.03     # 每一輪下完指令後等這麼久再讀位置
TOGETHER_LAG_DEG = 12.0      # 途中任何一顆落後它這一輪的目標超過這個值，就當成卡住
GESTURES = {
    # 起點是上游 AmazingHand_Demo.py 的 Perfect()（右手）：食指 (50, -50)、拇指 (65, 12)。
    # 2026-10-07 在這隻手上調了兩輪（一根手指的彎曲 = (奇 - 偶) / 2，側擺 = (奇 + 偶) / 2）：
    #   上游角度：指尖差約 3 cm、拇指在食指前方。
    #   食指彎曲 58°、拇指彎曲 40.5°：剩約 1 cm，兩指尖相對。兩根合計多彎 22° 拉近約 2 cm。
    #   食指彎曲 63°、拇指彎曲 48.5°：剩約 0.5 cm。這一輪合計多彎 13° 只拉近約 0.5 cm。
    #   食指彎曲 74°、拇指彎曲 51.5°：指尖接觸（照片）。拇指奇數那顆在表內上限 90°。
    # 拇指側擺一直維持 38.5°。
    "ok": {"label": "OK",
           "order": (3, 5, 1, 7),
           "pose": {1: (74, -74), 3: (0, 0), 5: (-20, 20), 7: (90, -13)}},
}


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


def wait_enter(msg, timeout=None):
    """等人按 Enter。給了 timeout（秒）就最多等這麼久；回傳是否真的按了。"""
    print(msg, flush=True)
    if timeout is None:
        input()
        return True
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        sys.stdin.readline()
        return True
    return False


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
    return c if _power_ok(c, (a, b)) else None


def _power_ok(c, ids):
    for sid in ids:
        volt = one(c.read_present_voltage(sid)) / 10.0
        temp = one(c.read_present_temperature(sid))
        print("ID %d  電壓 %.1f V  溫度 %d °C" % (sid, volt, temp))
        if not (VOLT_MIN <= volt <= VOLT_MAX):
            print("電壓不在 %.1f–%.1f V 範圍內，不動作。先檢查電源。" % (VOLT_MIN, VOLT_MAX))
            return False
        if temp > TEMP_MAX_C:
            print("溫度超過 %d °C，不動作。" % TEMP_MAX_C)
            return False
    return True


def _engage(c, ids, speed):
    """開扭力前先設速度，並把目標設成目前位置，開扭力的瞬間才不會朝舊目標衝過去。"""
    for sid in ids:
        c.write_goal_speed(sid, speed)
        c.write_goal_position(sid, one(c.read_present_position(sid)))
        c.write_torque_enable(sid, 1)


def _move_pair(c, a, b, deg_a, deg_b, tolerance):
    """兩顆一起走到目標，輪詢到位。回傳 (是否都到位, a 的實際角度, b 的實際角度)。"""
    c.write_goal_position(a, math.radians(deg_a))
    c.write_goal_position(b, math.radians(deg_b))
    now_a = now_b = last_a = last_b = None
    for _ in range(int(FINGER_SETTLE_S / 0.1)):
        time.sleep(0.1)
        now_a = math.degrees(one(c.read_present_position(a)))
        now_b = math.degrees(one(c.read_present_position(b)))
        near = abs(now_a - deg_a) < tolerance and abs(now_b - deg_b) < tolerance
        # 進到容許範圍後還要等它停下來，回報的才是靜止位置，不是途中的讀值。
        still = (last_a is not None
                 and abs(now_a - last_a) < STILL_DEG and abs(now_b - last_b) < STILL_DEG)
        if near and still:
            return True, now_a, now_b
        last_a, last_b = now_a, now_b
    near = abs(now_a - deg_a) < tolerance and abs(now_b - deg_b) < tolerance
    return near, now_a, now_b


def _move_together(c, plan):
    """多根手指同時走到各自的目標。plan 是 [(奇數 ID, 奇數 ID 角度, 偶數 ID 角度, 標籤), …]。

    每一輪依序對每一顆下一小段的目標，然後讀回位置與電壓；有一顆跟不上或電壓過低就
    立刻回傳 False（呼叫端負責關扭力）。最後一輪之後再逐指等它停下來並印出讀值。
    """
    goal = {}
    for a, deg_a, deg_b, _ in plan:
        goal[a], goal[a + 1] = deg_a, deg_b
    start = {sid: math.degrees(one(c.read_present_position(sid))) for sid in goal}
    rounds = max(1, math.ceil(max(abs(goal[sid] - start[sid]) for sid in goal) / TOGETHER_STEP_DEG))
    for k in range(1, rounds + 1):
        sub = {sid: start[sid] + (goal[sid] - start[sid]) * k / rounds for sid in goal}
        for sid in goal:
            c.write_goal_position(sid, math.radians(sub[sid]))
        time.sleep(TOGETHER_PERIOD_S)
        for sid in goal:
            now = math.degrees(one(c.read_present_position(sid)))
            if abs(now - sub[sid]) > TOGETHER_LAG_DEG:
                print("  %s ID %d 途中這一輪的目標 %+6.1f° 實際 %+6.1f°   卡住（第 %d／%d 輪）" % (
                    FINGER_NAMES[sid - (sid + 1) % 2], sid, sub[sid], now, k, rounds))
                return False
            volt = one(c.read_present_voltage(sid)) / 10.0
            if volt < VOLT_MIN:
                print("  同時動作途中 ID %d 電壓掉到 %.1f V，低於 %.1f V，停止（第 %d／%d 輪）。" % (
                    sid, volt, VOLT_MIN, k, rounds))
                return False
    return all(_step_to(c, a, a + 1, deg_a, deg_b, label) for a, deg_a, deg_b, label in plan)


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
        _engage(c, (a, b), CENTER_SPEED)
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
        _engage(c, (a, b), FINGER_SPEED)
        plan = [("張開", FINGER_OPEN_DEG)]
        plan += [("閉合 %d°" % d if d else "中位", d) for d in FINGER_CLOSE_STEPS_DEG]
        plan += [("張開", FINGER_OPEN_DEG), ("中位", 0)]
        for label, d in plan:
            if not _step(c, a, b, mid_a, mid_b, label, d):
                print("沒有到位，立刻關扭力。檢查連桿、舵盤有沒有互卡，或手指有沒有頂到東西。")
                ok = False
                break
            if d == FINGER_CLOSE_STEPS_DEG[-1]:
                wait_enter("手指已閉合。看兩個舵盤的耳朵有沒有對齊伺服機中線，看完按 Enter 張開。")
    finally:
        ok = _torque_off(c, (a, b)) and ok
    print("結果：" + ("完成，扭力已關" if ok else "異常，扭力已關"))
    return 0 if ok else 1


def _step(c, a, b, mid_a, mid_b, label, d):
    """一根手指走到一個對稱姿態（奇數 +d、偶數 -d）並印出結果。回傳是否到位。"""
    return _step_to(c, a, b, mid_a + d, mid_b - d, label)


def _step_to(c, a, b, deg_a, deg_b, label):
    """一根手指的兩顆各走到指定角度並印出結果。回傳是否到位。"""
    reached, now_a, now_b = _move_pair(c, a, b, deg_a, deg_b, FINGER_TOLERANCE_DEG)
    print("  %-8s ID %d 目標 %+6.1f° 實際 %+6.1f°   ID %d 目標 %+6.1f° 實際 %+6.1f°   %s" % (
        label, a, deg_a, now_a, b, deg_b, now_b, "OK" if reached else "卡住"))
    return reached


def cmd_hand(port, mids=None):
    """全手：8 顆都在線，先全部張開，再讓四根手指輪流分段閉合、張開。一次只動一根。"""
    mids = list(mids) if mids else [0.0] * 8
    if len(mids) != 8 or any(abs(m) > MID_LIMIT_DEG for m in mids):
        print("中位修正要給 8 個值（ID 1 到 8），每個在 ±%d° 以內；不給就全部當 0。" % MID_LIMIT_DEG)
        return 1
    c = open_bus(port)
    found = scan(c)
    if sorted(found) != list(HAND_IDS):
        print("匯流排上看到 %s，預期恰好是 ID 1 到 8。" % list(found))
        return 1
    if not _power_ok(c, HAND_IDS):
        return 1
    wait_enter("手的周圍淨空，一隻手放在伺服機電源的開關上。按 Enter 開始，Ctrl-C 隨時中止。")
    ok = True
    try:
        _engage(c, HAND_IDS, FINGER_SPEED)
        print("== 全部張開")
        for a, b in FINGER_PAIRS:
            if not _step(c, a, b, mids[a - 1], mids[b - 1], FINGER_NAMES[a], FINGER_OPEN_DEG):
                ok = False
                break
        for a, b in FINGER_PAIRS if ok else ():
            print("== %s（ID %d、%d）" % (FINGER_NAMES[a], a, b))
            plan = [("閉合 %d°" % d if d else "中位", d) for d in FINGER_CLOSE_STEPS_DEG]
            plan += [("張開", FINGER_OPEN_DEG)]
            for label, d in plan:
                if not _step(c, a, b, mids[a - 1], mids[b - 1], label, d):
                    ok = False
                    break
            if not ok:
                break
            volts = [one(c.read_present_voltage(sid)) / 10.0 for sid in (a, b)]
            if min(volts) < VOLT_MIN:
                print("動作後電壓掉到 %.1f V，低於 %.1f V，停止。檢查電源與線材。" % (min(volts), VOLT_MIN))
                ok = False
                break
        if not ok:
            print("沒有完成，立刻關扭力。檢查卡住的那根手指有沒有碰到隔壁的手指、連桿或線。")
    finally:
        ok = _torque_off(c, HAND_IDS) and ok
    print("結果：" + ("完成，扭力已關" if ok else "異常，扭力已關"))
    return 0 if ok else 1


def cmd_gesture(port, name, mids=None):
    """全手比一個固定手勢：全部張開 → 四指同時擺出 → 停住等人 → 四指同時張開 → 關扭力。"""
    g = GESTURES.get(name)
    if g is None:
        print("沒有「%s」這個手勢。可用的：%s" % (name, "、".join(sorted(GESTURES))))
        return 1
    mids = list(mids) if mids else [0.0] * 8
    if len(mids) != 8 or any(abs(m) > MID_LIMIT_DEG for m in mids):
        print("中位修正要給 8 個值（ID 1 到 8），每個在 ±%d° 以內；不給就全部當 0。" % MID_LIMIT_DEG)
        return 1
    targets = {a: (g["pose"][a][0] + mids[a - 1], g["pose"][a][1] + mids[a]) for a in g["order"]}
    if any(abs(v) > SERVO_LIMIT_DEG for pair in targets.values() for v in pair):
        print("手勢加上中位修正後有目標超出 ±%d°，不動作。" % SERVO_LIMIT_DEG)
        return 1
    c = open_bus(port)
    found = scan(c)
    if sorted(found) != list(HAND_IDS):
        print("匯流排上看到 %s，預期恰好是 ID 1 到 8。" % list(found))
        return 1
    if not _power_ok(c, HAND_IDS):
        return 1
    wait_enter("要比「%s」。手的周圍淨空，一隻手放在伺服機電源的開關上。按 Enter 開始，Ctrl-C 隨時中止。" % g["label"])
    ok = True

    def opened(fingers):
        return [(a, mids[a - 1] + FINGER_OPEN_DEG, mids[a] - FINGER_OPEN_DEG, FINGER_NAMES[a] + "張開")
                for a in fingers]
    try:
        _engage(c, HAND_IDS, FINGER_SPEED)
        print("== 全部張開（四指同時）")
        ok = _move_together(c, opened(a for a, _ in FINGER_PAIRS))
        if ok:
            print("== 擺出「%s」（四指同時）" % g["label"])
            ok = _move_together(c, [(a, targets[a][0], targets[a][1], FINGER_NAMES[a]) for a in g["order"]])
        if ok:
            pressed = wait_enter("已比出「%s」。按 Enter 收回；%d 秒內沒按也會自己收回。" % (g["label"], HOLD_MAX_S),
                                 timeout=HOLD_MAX_S)
            if not pressed:
                print("（%d 秒到，自動收回）" % HOLD_MAX_S)
            print("== 收回（四指同時）")
            ok = _move_together(c, opened(reversed(g["order"])))
        if not ok:
            print("沒有完成，立刻關扭力。如果卡住的是食指或拇指，多半是指尖比預期早碰到；把這段輸出貼回來調整姿態。")
    finally:
        ok = _torque_off(c, HAND_IDS) and ok
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
        if cmd == "hand" and len(argv) in (3, 11):
            return cmd_hand(argv[2], [float(x) for x in argv[3:]])
        if cmd == "gesture" and len(argv) in (4, 12):
            return cmd_gesture(argv[2], argv[3], [float(x) for x in argv[4:]])
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
