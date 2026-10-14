"""戒指三軸 → 握拳程度 → 手指目標角度。純運算，不碰藍牙、網路或檔案。

做法：戒指的加速度計在手不晃動時量到的是重力方向。手掌不轉動時，握拳會讓戴戒指的那節手指
繞著指根轉，重力方向在戒指座標裡就沿著一條弧線移動。校正時記下「張開」與「握拳」兩個方向，
之後把目前的方向投影到這條弧線上，得到 0（張開）到 1（握拳）的握拳程度。

限制（單顆三軸加速度計的物理限制）：
  - 轉手腕或前臂也會改變重力方向，戒指分不出來。離開校正的弧線太遠（off_limit）時不更新。
  - 握拳的轉軸剛好和重力平行時（手掌直立、手指朝前的握手姿勢），握拳不改變重力方向，量不到。
  - 手在加速（揮動）時量到的不只是重力；大小不在 0.8–1.2 g 之間的取樣不採用。
"""
import math
from collections import deque

G_LSB = 8192.0                 # 名義比例（社群協定文件）；只用來判斷大小是否接近 1 g
MAGNITUDE_RANGE = (0.8, 1.2)
MIN_SPAN_DEG = 20.0            # 張開與握拳的重力方向至少要差這麼多
MAX_SPAN_DEG = 165.0           # 接近反向時弧線的平面不穩定
RANGE_MARGIN_DEG = 35.0        # 超出弧線兩端這麼多就當作手的姿勢變了

FINGERS = ("thumb", "index", "middle", "ring")
FINGER_LABELS = {"thumb": "拇指", "index": "食指", "middle": "中指", "ring": "無名指"}
FLEX_RANGE = (-30.0, 90.0)     # 與手掌面板的滑桿範圍相同
SIDE_RANGE = (-40.0, 40.0)
SERVO_LIMIT = 90.0
OPEN_POSE = {name: (-30.0, 0.0) for name in FINGERS}
BUILTIN_FIST = {name: (85.0, 0.0) for name in FINGERS}


def norm(v):
    return math.sqrt(sum(x * x for x in v))


def unit(v):
    n = norm(v)
    if not math.isfinite(n) or n < 1e-9:
        raise ValueError("向量無效")
    return tuple(x / n for x in v)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def angle_deg(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, dot(unit(a), unit(b))))))


def valid_vector(v):
    return (isinstance(v, (list, tuple)) and len(v) == 3 and
            all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v))


class CurlModel:
    """張開與握拳兩個重力方向定義的弧線。"""

    def __init__(self, open_vec, fist_vec):
        if not valid_vector(open_vec) or not valid_vector(fist_vec):
            raise ValueError("校正資料無效")
        self.open = unit(open_vec)
        self.fist = unit(fist_vec)
        self.span = angle_deg(self.open, self.fist)
        if self.span < MIN_SPAN_DEG:
            raise ValueError("張開與握拳的讀值只差 %.0f°，至少要 %.0f°。手掌換個方向（例如掌心朝下）再校正一次"
                             % (self.span, MIN_SPAN_DEG))
        if self.span > MAX_SPAN_DEG:
            raise ValueError("張開與握拳的讀值幾乎相反（%.0f°），請重新校正" % self.span)
        d = dot(self.open, self.fist)
        self.along = unit(tuple(self.fist[i] - d * self.open[i] for i in range(3)))
        self.normal = cross(self.open, self.along)

    def measure(self, raw, off_limit=35.0):
        """raw 是戒指回報的三軸原始值。回傳 dict：valid、issue、progress（0=張開、1=握拳，未截斷）等。"""
        if not valid_vector(raw):
            return {"valid": False, "issue": "invalid", "magnitude": None}
        magnitude = norm(raw) / G_LSB
        out = {"valid": False, "issue": None, "magnitude": round(magnitude, 3)}
        if not MAGNITUDE_RANGE[0] <= magnitude <= MAGNITUDE_RANGE[1]:
            out["issue"] = "moving"
            return out
        g = unit(raw)
        theta = math.degrees(math.atan2(dot(g, self.along), dot(g, self.open)))
        if theta < -RANGE_MARGIN_DEG and theta + 360.0 <= self.span + RANGE_MARGIN_DEG:
            theta += 360.0
        off = math.degrees(math.asin(max(-1.0, min(1.0, dot(g, self.normal)))))
        out.update(theta=round(theta, 1), off=round(off, 1), progress=theta / self.span)
        if abs(off) > off_limit:
            out["issue"] = "off_axis"
        elif theta < -RANGE_MARGIN_DEG or theta > self.span + RANGE_MARGIN_DEG:
            out["issue"] = "out_of_range"
        else:
            out["valid"] = True
        return out


class PalmModel(CurlModel):
    """握拳座標＋左右側擺；用左右取樣補償側擺伴隨的握拳方向偏移。"""
    def __init__(self, open_vec, fist_vec, left_vec, right_vec, compensate=True):
        super().__init__(open_vec, fist_vec)
        self.compensate = compensate
        self.sides = {}
        self.coupling = {}
        self.geometry = {}
        for name, vec in (("left", left_vec), ("right", right_vec)):
            d = self.check_side(name, vec)
            self.geometry[name] = d
            self.sides[name] = d["side_deg"]
            self.coupling[name] = d["along_deg"]
        if self.sides["left"] * self.sides["right"] >= 0:
            raise ValueError("左右側擺記成同一側；請回到中立再分別向左、向右，兩邊都要離開中立")

    def side_diagnostics(self, vec):
        if not valid_vector(vec):
            raise ValueError("左右側擺校正資料無效")
        g = unit(vec)
        latitude = math.degrees(math.asin(max(-1.0, min(1.0, dot(g, self.normal)))))
        along = math.degrees(math.atan2(dot(g, self.along), dot(g, self.open)))
        separation = math.degrees(math.atan2(abs(latitude), abs(along)))
        return {"side_deg": latitude, "along_deg": along, "separation_deg": separation}

    def check_side(self, name, vec):
        d = PalmModel.side_diagnostics(self, vec)
        side, along, separation = d["side_deg"], d["along_deg"], d["separation_deg"]
        label = "左" if name == "left" else "右"
        if abs(side) < 10:
            raise ValueError("%s側擺可辨識角度 %.1f°，不足 10°；多側傾一些。平行桌面旋轉可能量不到" % (label, abs(side)))
        if abs(side) > 65:
            raise ValueError("%s側擺角度 %.1f°，超過 65°；減少側傾幅度，保持手指自然張開" % (label, abs(side)))
        if abs(along) > 80:
            raise ValueError("%s側擺沿握拳方向偏移 %.1f°，超過 80°；回到中立、保持自然張開再側傾" % (label, abs(along)))
        if separation < 20:
            raise ValueError("%s側擺與握拳方向分離度 %.1f°，需至少 20°（沿握拳偏移 %.1f°）；換個手掌中立方向再校正" %
                             (label, separation, along))
        return d

    def measure(self, raw, off_limit=35.0):
        out = super().measure(raw, 90.0)
        if out.get("issue") in ("invalid", "moving"):
            return out
        g = unit(raw)
        d = self.side_diagnostics(g)
        off, theta = d["side_deg"], d["along_deg"]
        endpoint_name = "left" if off * self.sides["left"] > 0 else "right"
        fraction = off / self.sides[endpoint_name]
        sway = abs(fraction) * (-1 if endpoint_name == "left" else 1)
        correction = fraction * self.coupling[endpoint_name] if self.compensate else 0.0
        # 選擇最接近有效握拳弧線的 360° 分支，避免跨 atan2 的 ±180° 時跳動。
        theta -= correction
        theta = min((theta - 360, theta, theta + 360), key=lambda t: abs(t - self.span / 2))
        out.update(theta=round(theta, 1), off=round(off, 1), progress=theta / self.span,
                   sway_progress=sway, sway_angle=round(off, 1), coupling_correction=round(correction, 1),
                   valid=True, issue=None)
        if theta < -RANGE_MARGIN_DEG or theta > self.span + RANGE_MARGIN_DEG:
            out.update(valid=False, issue="out_of_range")
        margin = min(off_limit, 15.0)
        lo, hi = sorted(self.sides.values())
        if off < lo - margin or off > hi + margin or abs(off) > 75:
            out.update(valid=False, issue="sway_out_of_range")
        return out


class SwayFilter:
    """側擺 -1（左）至 +1（右）；中立區之外重新縮放，再平滑。"""
    def __init__(self):
        self.output = None

    def reset(self):
        self.output = None

    def update(self, progress, neutral=0.1, smoothing=0.5):
        target = math.copysign(max(0.0, min(1.0, (abs(progress) - neutral) / (1 - neutral))), progress)
        if self.output is None or target == 0:
            self.output = target
        else:
            self.output += (1 - smoothing) * (target - self.output)
        if abs(self.output) > .998:
            self.output = math.copysign(1.0, self.output)
        return self.output


class CurlFilter:
    """把未截斷的 progress 整理成送給手掌的握拳程度 0–1。"""

    def __init__(self):
        self.value = None          # 平滑後、0–1
        self.closed = False        # 兩段模式的狀態
        self.output = None

    def reset(self):
        self.value = None
        self.closed = False
        self.output = None

    def update(self, progress, lo=0.12, hi=0.88, smoothing=0.5, mode="continuous"):
        hi = max(hi, lo + 0.05)
        target = max(0.0, min(1.0, (progress - lo) / (hi - lo)))
        if self.value is None:
            self.value = target
        else:
            self.value += (1.0 - max(0.0, min(0.95, smoothing))) * (target - self.value)
        if self.value < 0.002:
            self.value = 0.0
        elif self.value > 0.998:
            self.value = 1.0
        if mode == "binary":
            if self.value >= 0.6:
                self.closed = True
            elif self.value <= 0.4:
                self.closed = False
            self.output = 1.0 if self.closed else 0.0
        else:
            self.output = self.value
        return self.output


class StabilityWindow:
    """最近一小段時間的取樣；校正時用來判斷手有沒有停住，並取平均方向。"""

    def __init__(self, seconds=2.0, min_samples=8, min_span_s=1.2, max_spread_deg=4.0):
        self.seconds = seconds
        self.min_samples = min_samples
        self.min_span_s = min_span_s
        self.max_spread_deg = max_spread_deg
        self.samples = deque()

    def clear(self):
        self.samples.clear()

    def add(self, now, raw):
        self.samples.append((now, tuple(raw)))
        while self.samples and now - self.samples[0][0] > self.seconds:
            self.samples.popleft()

    def summary(self, now):
        rows = [(t, raw) for t, raw in self.samples if now - t <= self.seconds]
        out = {"stable": False, "samples": len(rows), "spread": None, "mean": None, "issue": None}
        if len(rows) < self.min_samples or rows[-1][0] - rows[0][0] < self.min_span_s:
            out["issue"] = "collecting"
            return out
        if now - rows[-1][0] > 0.6:
            out["issue"] = "stale"
            return out
        if any(not MAGNITUDE_RANGE[0] + 0.1 <= norm(raw) / G_LSB <= MAGNITUDE_RANGE[1] - 0.1 for _, raw in rows):
            out["issue"] = "moving"
            return out
        units = [unit(raw) for _, raw in rows]
        mean = unit(tuple(sum(u[i] for u in units) for i in range(3)))
        spread = max(angle_deg(u, mean) for u in units)
        out.update(spread=round(spread, 1), mean=[round(x, 5) for x in mean])
        if spread > self.max_spread_deg:
            out["issue"] = "moving"
            return out
        out["stable"] = True
        return out


def clamp_finger(flex, side):
    """和手掌面板相同的收斂規則：彎曲與側擺在滑桿範圍內，且兩顆伺服機都在 ±90° 內。"""
    f = min(max(flex, FLEX_RANGE[0]), FLEX_RANGE[1])
    room = SERVO_LIMIT - abs(f)
    s = min(max(side, max(SIDE_RANGE[0], -room)), min(SIDE_RANGE[1], room))
    return f, s


def finger_targets(curl, fist_pose=None, enabled=None, scale=1.0, open_pose=None):
    """握拳程度 0–1 → 每根手指的 (彎曲, 側擺)。沒勾選的手指留在張開姿勢。"""
    curl = max(0.0, min(1.0, curl)) * max(0.0, min(1.0, scale))
    open_pose = open_pose or OPEN_POSE
    fist_pose = fist_pose or BUILTIN_FIST
    out = {}
    for name in FINGERS:
        o = open_pose[name]
        f = fist_pose.get(name, BUILTIN_FIST[name])
        c = curl if (enabled is None or enabled.get(name, True)) else 0.0
        flex, side = clamp_finger(o[0] + c * (f[0] - o[0]), o[1] + c * (f[1] - o[1]))
        out[name] = {"flex": round(flex, 1), "side": round(side, 1)}
    return out


def direct_targets(curl, sway, enabled=None):
    """校正後的連續雙輸入 → 四指彎曲／側擺，不讀任何存好的姿勢。

    彎曲 -30..85°；側擺 -40..40°，拇指鏡像；最後逐指依成對馬達限位收斂。
    """
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
               for x in (curl, sway)):
        raise ValueError("跟隨輸入無效")
    flex = -30.0 + 115.0 * max(0.0, min(1.0, curl))
    side = 40.0 * max(-1.0, min(1.0, sway))
    out = {}
    for name in FINGERS:
        if enabled is not None and not enabled.get(name, True):
            f, s = -30.0, 0.0
        else:
            f, s = clamp_finger(flex, -side if name == "thumb" else side)
        out[name] = {"flex": round(f, 1), "side": round(s, 1)}
    return out
