#!/usr/bin/env python3
"""估算 Amazing Hand 一根手指在不同彎曲角度下的指尖力（虛功法，只算機構幾何）。

用 Pollen Robotics 官方的 MuJoCo 模型（pollen-robotics/AmazingHand 的
Demo/AHSimulation/AHSimulation/AH_Right/mjcf/scene.xml；模型不收在這個 repo）：
把第一根手指的兩顆伺服機設成彎曲姿勢（motor1 = +φ、motor2 = −φ，+φ 是握），
量指尖每轉 1 rad 走多遠，再由 F = 2τ ÷ (|dx|/dφ) 換算成指尖沿運動方向能出的力。

沒有算摩擦、軟殼變形、零件公差與伺服機效率，Seeed 套件的零件也可能和官方模型略有不同。
這是估算，不是實測；結果寫在 docs/tactile_bom.md §7。

需要：pip install mujoco numpy；另外 clone 官方 repo。不在 requirements.txt 裡。
用法：python tools/finger_force_estimate.py <AmazingHand>/Demo/AHSimulation/AHSimulation/AH_Right/mjcf/scene.xml
"""
import sys

import mujoco
import numpy as np

KGF_CM = 0.0980665               # 1 kg·cm = 0.0980665 N·m
TAU_PEAK = 1.89 * KGF_CM         # Seeed 標示 5 V 下 1.89 kg·cm；和 6 V 堵轉 2.3 kg·cm 按電壓換算相符，當作堵轉上限
TAU_RATED = 0.7 * KGF_CM         # 規格表額定 0.7–0.75 kg·cm（6 V）；5 V 下會再低一些
FLEX_DEG = (-30, 0, 30, 45, 60, 75, 85)   # 面板的彎曲範圍是 −30°–90°；90° 碰到模型的關節上限，不取
STEP_RAD = 0.02                  # 中央差分的半步
SETTLE_STEPS = 6000


def main(path):
    m = mujoco.MjModel.from_xml_path(path)
    m.opt.gravity[:] = 0         # 只看機構傳動，不讓重力影響靜態姿勢
    d = mujoco.MjData(m)
    m1 = m.actuator("finger1_motor1").id
    m2 = m.actuator("finger1_motor2").id
    j1 = m.jnt_qposadr[m.joint("finger1_motor1").id]
    j2 = m.jnt_qposadr[m.joint("finger1_motor2").id]
    tip = m.site("tip1").id

    def settle(phi):
        d.ctrl[:] = 0
        d.ctrl[m1], d.ctrl[m2] = phi, -phi
        for _ in range(SETTLE_STEPS):
            mujoco.mj_step(m, d)
        return d.site_xpos[tip].copy(), np.degrees(d.qpos[j1]), np.degrees(d.qpos[j2])

    print("彎曲(°)  指尖每 rad 走(mm)  堵轉指尖力(N)  額定指尖力(N)  兩顆實際角度(°)")
    for deg in FLEX_DEG:
        phi = np.radians(deg)
        mujoco.mj_resetData(m, d)
        before, _, _ = settle(phi - STEP_RAD)
        after, _, _ = settle(phi + STEP_RAD)
        _, a1, a2 = settle(phi)
        speed = np.linalg.norm(after - before) / (2 * STEP_RAD)   # m/rad
        print("%7d  %17.1f  %13.1f  %13.1f  %+6.1f %+6.1f" % (
            deg, speed * 1000, 2 * TAU_PEAK / speed, 2 * TAU_RATED / speed, a1, a2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
