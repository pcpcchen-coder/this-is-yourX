"""載入 right_hand/config 下的 manifest、校正值與手勢表，並做不依賴硬體的檢查。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field

import yaml

RIGHT_HAND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(RIGHT_HAND_DIR, "config")

SERVO_IDS = tuple(range(1, 9))
FINGER_KEYS = {1: "index", 3: "middle", 5: "ring", 7: "thumb"}
FINGER_NAMES_ZH = {1: "食指", 3: "中指", 5: "無名指", 7: "拇指"}
HAND_ID = "hand.right"
MID_LIMIT_DEG = 30.0           # 中位修正超過這個值，多半是舵盤裝錯齒
POSE_LIMIT_DEG = 90.0          # 手勢表裡的角度（不含中位修正）
SERVO_LIMIT_DEG = 95.0         # 任何一顆的目標（含中位修正）
GESTURE_STATUSES = ("confirmed", "draft", "needs_recheck")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Gesture:
    name: str
    label: str
    description: str
    status: str
    calibration_revision: str
    evidence: str
    order: tuple
    pose: dict  # {奇數 ID: (奇數角度, 偶數角度)}


@dataclass(frozen=True)
class HandConfig:
    manifest: dict
    calibration_revision: str
    middle_offsets: dict          # {servo ID: 度}
    gestures: dict                # {名稱: Gesture}
    graph_revision: str           # 三份設定的 canonical JSON 雜湊
    components: dict = field(default_factory=dict)   # {component id: 定義}
    skills: dict = field(default_factory=dict)       # {skill 名稱: 定義}

    def servo_component(self, sid):
        """servo ID → component ID（從 binding.device_identity 反查）。"""
        for cid, comp in self.components.items():
            if comp.get("binding", {}).get("device_identity") == "scs:%d" % sid:
                return cid
        raise KeyError(sid)


def _read_yaml(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError("找不到 %s" % path)


def _revision(*docs):
    blob = json.dumps(docs, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load(config_dir=CONFIG_DIR):
    manifest = _read_yaml(os.path.join(config_dir, "body.yaml"))
    calibration = _read_yaml(os.path.join(config_dir, "calibration.yaml"))
    gestures_doc = _read_yaml(os.path.join(config_dir, "gestures.yaml"))

    components = {c["id"]: c for c in manifest.get("components", [])}
    if HAND_ID not in components:
        raise ConfigError("manifest 裡沒有 %s" % HAND_ID)
    skills = {s["name"]: s for s in manifest.get("skills", [])}

    rev = str(calibration.get("revision", "")).strip()
    if not rev:
        raise ConfigError("calibration.yaml 沒有 revision")
    raw_mids = calibration.get("middle_offset_deg") or {}
    mids = {int(k): float(v) for k, v in raw_mids.items()}
    if sorted(mids) != list(SERVO_IDS):
        raise ConfigError("middle_offset_deg 要恰好有 ID 1 到 8")
    if any(not math.isfinite(v) or abs(v) > MID_LIMIT_DEG for v in mids.values()):
        raise ConfigError("中位修正要在 ±%d° 以內" % MID_LIMIT_DEG)

    gestures = {}
    for name, g in (gestures_doc.get("gestures") or {}).items():
        if not name.replace("_", "").isalnum() or name != name.lower():
            raise ConfigError("手勢名稱只能是小寫英數與底線：%r" % name)
        pose = {int(k): (float(v[0]), float(v[1])) for k, v in (g.get("pose") or {}).items()}
        order = tuple(int(x) for x in g.get("order") or ())
        if set(pose) != set(FINGER_KEYS) or sorted(order) != sorted(FINGER_KEYS):
            raise ConfigError("手勢 %s 要四根手指各一筆（1、3、5、7），order 也是" % name)
        if any(not math.isfinite(x) or abs(x) > POSE_LIMIT_DEG for pair in pose.values() for x in pair):
            raise ConfigError("手勢 %s 的角度超過 ±%d°" % (name, POSE_LIMIT_DEG))
        status = g.get("status")
        if status not in GESTURE_STATUSES:
            raise ConfigError("手勢 %s 的 status 要是 %s" % (name, "、".join(GESTURE_STATUSES)))
        for a, (da, db) in pose.items():
            if abs(da + mids[a]) > SERVO_LIMIT_DEG or abs(db + mids[a + 1]) > SERVO_LIMIT_DEG:
                raise ConfigError("手勢 %s 加上中位修正後超出 ±%d°" % (name, SERVO_LIMIT_DEG))
        gestures[name] = Gesture(
            name=name, label=str(g.get("label", name)), description=str(g.get("description", "")),
            status=status, calibration_revision=str(g.get("calibration_revision", "")),
            evidence=str(g.get("evidence", "")), order=order, pose=pose)

    return HandConfig(
        manifest=manifest, calibration_revision=rev, middle_offsets=mids, gestures=gestures,
        graph_revision=_revision(manifest, calibration, gestures_doc),
        components=components, skills=skills)
