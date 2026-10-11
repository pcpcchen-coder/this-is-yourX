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
