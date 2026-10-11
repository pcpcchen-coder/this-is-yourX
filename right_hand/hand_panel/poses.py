"""操作面板存下的姿勢：一個 YAML 檔（預設 right_hand/config/poses.yaml）。

格式刻意和 config/gestures.yaml 的 pose 相同：key 是每根手指的奇數 ID（1 食指、3 中指、5 無名指、7 拇指），
值是 (奇數 ID 角度, 偶數 ID 角度)，單位度，不含中位修正。要把某個姿勢升級成對 AI 開放的手勢，
仍然照 docs/hand_api_design.md §5 的流程由人登記到 gestures.yaml；這個檔不會被 hand_api 讀取。
"""
from __future__ import annotations

import math
import os
import re
import tempfile
import threading

import yaml

from hand_api.config import FINGER_KEYS, POSE_LIMIT_DEG

NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_POSES = 200
LABEL_MAX_CHARS = 40
NOTE_MAX_CHARS = 200
SOURCES = ("target", "measured")
SCHEMA_VERSION = 1
HEADER = (
    "# 操作面板（right_hand/hand_panel）存下的姿勢。由面板寫入；也可以手改，格式錯了面板會拒絕讀寫並指出原因。\n"
    "# pose 的 key 是每根手指的奇數 ID：1 食指、3 中指、5 無名指、7 拇指；值是 (奇數 ID 角度, 偶數 ID 角度)，\n"
    "# 單位度，不含中位修正。source：target = 存的是當時下的目標；measured = 扭力關著時讀到的位置。\n"
    "# 這些是操作者的工作台姿勢，沒有對 AI 開放；對 AI 開放的手勢表是 gestures.yaml。\n"
)


class StoreError(Exception):
    pass


class StoreFull(StoreError):
    pass


def _clean_pair(v, where):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise StoreError("%s 要是兩個角度" % where)
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise StoreError("%s 的角度不是有限數字：%r" % (where, x))
        out.append(round(float(x), 1))
    return out


def clean_pose(raw, where="pose", limit=POSE_LIMIT_DEG):
    """回傳 {奇數 ID: [a, b]}；格式或範圍不對就丟 StoreError。"""
    if not isinstance(raw, dict):
        raise StoreError("%s 要是對照表" % where)
    try:
        pose = {int(k): _clean_pair(v, "%s[%s]" % (where, k)) for k, v in raw.items()}
    except (TypeError, ValueError):
        raise StoreError("%s 的 key 要是 1、3、5、7" % where)
    if set(pose) != set(FINGER_KEYS):
        raise StoreError("%s 要四根手指各一筆（1、3、5、7）" % where)
    if limit is not None and any(abs(x) > limit for pair in pose.values() for x in pair):
        raise StoreError("%s 的角度超過 ±%d°" % (where, limit))
    return {k: pose[k] for k in sorted(pose)}


def _clean_record(name, rec):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise StoreError("姿勢名稱只能是小寫英數與底線、最多 32 個字：%r" % (name,))
    if not isinstance(rec, dict):
        raise StoreError("姿勢 %s 的內容要是對照表" % name)
    source = rec.get("source", "target")
    if source not in SOURCES:
        raise StoreError("姿勢 %s 的 source 要是 %s" % (name, "、".join(SOURCES)))
    out = {
        "label": str(rec.get("label") or "")[:LABEL_MAX_CHARS],
        "note": str(rec.get("note") or "")[:NOTE_MAX_CHARS],
        "saved_at": str(rec.get("saved_at") or ""),
        "calibration_revision": str(rec.get("calibration_revision") or ""),
        "source": source,
        "pose": clean_pose(rec.get("pose"), "姿勢 %s 的 pose" % name),
    }
    if rec.get("measured") is not None:
        out["measured"] = clean_pose(rec["measured"], "姿勢 %s 的 measured" % name, limit=None)
    return out


class PoseStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def load(self):
        """回傳 {名稱: 紀錄}。檔案不存在是空表；讀不懂就丟 StoreError（不會自動覆蓋）。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except FileNotFoundError:
            return {}
        except (OSError, yaml.YAMLError) as e:
            raise StoreError("讀不了 %s：%s" % (self.path, e))
        if doc is None:
            return {}
        if not isinstance(doc, dict) or not isinstance(doc.get("poses") or {}, dict):
            raise StoreError("%s 的格式不對：最上層要有 poses 對照表" % self.path)
        if doc.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise StoreError("%s 的 schema_version 不是 %d" % (self.path, SCHEMA_VERSION))
        return {name: _clean_record(name, rec) for name, rec in (doc.get("poses") or {}).items()}

    def _write(self, poses):
        body = yaml.safe_dump({"schema_version": SCHEMA_VERSION, "poses": poses}, allow_unicode=True,
                              sort_keys=False, default_flow_style=None, width=120)
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".poses-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(HEADER)
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)       # 同一個資料夾內改名：不會留下寫到一半的檔
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def save(self, name, record, overwrite=False):
        with self._lock:
            poses = self.load()
            record = _clean_record(name, record)
            if name in poses and not overwrite:
                raise KeyError(name)
            if name not in poses and len(poses) >= MAX_POSES:
                raise StoreFull("最多存 %d 個姿勢，先刪掉用不到的" % MAX_POSES)
            poses[name] = record
            self._write(poses)
            return record

    def update(self, name, **fields):
        with self._lock:
            poses = self.load()
            if name not in poses:
                raise KeyError(name)
            poses[name] = _clean_record(name, dict(poses[name], **fields))
            self._write(poses)
            return poses[name]

    def delete(self, name):
        with self._lock:
            poses = self.load()
            if name not in poses:
                raise KeyError(name)
            removed = poses.pop(name)
            self._write(poses)
            return removed
