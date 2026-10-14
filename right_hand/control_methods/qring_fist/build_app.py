"""用 Apple Python 3.9 的 venv 建置 QRing Hand.app（Apple Silicon）。

  cd right_hand/control_methods/qring_fist && .venv/bin/python build_app.py

做法與 qringr08plus 的 mac-app/build_app.py 相同：把執行中 venv 所屬的 Apple Python3.framework 與 site-packages
包進 App，原生容器（native/Hand.swift）固定用內建的 Python 3.9。產物在 .build.noindex/（不進 Git）。
"""
import plistlib
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / ".build.noindex"
APP = OUTPUT / "QRing Hand.app"
FRAMEWORK = Path(sys.base_prefix).resolve().parents[1]
SITE = Path(sysconfig.get_path("purelib"))
VERSION = "0.3.1"
BUILD = "6"
CODE = ("server.py", "engine.py", "follow.py", "ring.py", "hand.py", "sim.py")

if __name__ == "__main__":
    if sys.version_info[:2] != (3, 9):
        raise SystemExit("Build with an Apple Python 3.9 venv; the native launcher requires Python 3.9.")
    if FRAMEWORK.name != "Python3.framework" or not (FRAMEWORK / "Versions/3.9/bin/python3.9").is_file():
        raise SystemExit("An Apple Python3.framework is required (Xcode or Command Line Tools).")
    if sys.prefix == sys.base_prefix or not (SITE / "aiohttp").is_dir() or not (SITE / "bleak").is_dir():
        raise SystemExit("Run with a venv that has requirements.txt installed.")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if APP.exists():
        shutil.rmtree(APP)
    mac = APP / "Contents/MacOS"
    mac.mkdir(parents=True)
    resources = APP / "Contents/Resources"
    resources.mkdir()
    shutil.copytree(FRAMEWORK, APP / "Contents/Frameworks/Python3.framework", symlinks=True)
    shutil.copytree(SITE, resources / "site-packages", ignore=shutil.ignore_patterns(
        "__pycache__", "pip", "pip-*.dist-info", "setuptools", "setuptools-*.dist-info", "pkg_resources",
        "wheel", "wheel-*.dist-info"))
    appcode = resources / "app"
    appcode.mkdir()
    for name in CODE:
        shutil.copy2(ROOT / name, appcode / name)
    if (ROOT / "site.json").is_file():          # 這台機器的預設值（手掌面板位址）；不進 Git
        shutil.copy2(ROOT / "site.json", appcode / "site.json")
    shutil.copytree(ROOT / "web", appcode / "web")
    info = {
        "CFBundleIdentifier": "com.pcpcchen.qringhand", "CFBundleName": "QRing Hand",
        "CFBundleDisplayName": "QRing Hand", "CFBundleExecutable": "QRingHand", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": BUILD, "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSBluetoothAlwaysUsageDescription": "連接你的 QRing 戒指，讀取三軸加速度與觸控事件來控制機器手掌。",
        "NSBluetoothPeripheralUsageDescription": "連接你的 QRing 戒指。",
        "NSLocalNetworkUsageDescription": "連接區域網路上的機器手掌操作面板（Raspberry Pi）。",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    }
    icon = ROOT / "native" / "QRingHand.icns"
    if icon.is_file():
        shutil.copy2(icon, resources / "QRingHand.icns")
        info["CFBundleIconFile"] = "QRingHand"
    (APP / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["swiftc", "-O", "-target", "arm64-apple-macosx13.0", "-framework", "Cocoa", "-framework", "WebKit",
                    str(ROOT / "native/Hand.swift"), "-o", str(mac / "QRingHand")], check=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(APP)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(APP)], check=True)
    print(APP)
