"""handd：獨占 gateway 與 adapter，開兩個本機 Unix socket。

  ai.sock        給 MCP server（AI 工具）用：只能查詢、提議、執行已核准的提議、停止。
  operator.sock  給 `hand` 指令（操作者）用：多了核准、清除 fault、看稽核紀錄。
兩個 socket 都只開給目前的使用者（目錄 0700、socket 0600）。呼叫者身分由「從哪個 socket 進來」決定，
不看請求內容。協定：每行一個 JSON 請求 {"id", "method", "params"}，回一行 JSON。
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import signal
import socketserver
import sys
import threading

from . import config as hand_config
from . import gateway as gw_mod
from .adapter import AdapterError, FakeHandAdapter, ScsHandAdapter
from . import motion

DEFAULT_RUNTIME_DIR = os.path.join(os.path.expanduser("~"), ".this-is-yourx", "hand")
MAX_REQUEST_BYTES = 64 * 1024
CONNECTION_TIMEOUT_S = 30.0


class AlreadyRunning(Exception):
    pass

AI_METHODS = {
    "hand_status": lambda g, p: g.hand_status(),
    "list_components": lambda g, p: g.list_components(p.get("kind")),
    "get_component": lambda g, p: g.get_component(p.get("component_id"), p.get("include", ["state"])),
    "resolve_reference": lambda g, p: g.resolve_reference(p.get("text"), p.get("locale")),
    "skill_list": lambda g, p: g.skill_list(),
    "skill_propose": lambda g, p: g.propose(p.get("skill"), p.get("target_component"), p.get("parameters", {}),
                                            p.get("reason", ""), caller=gw_mod.AI),
    "skill_execute": lambda g, p: g.execute(p.get("proposal_id"), p.get("idempotency_key"), caller=gw_mod.AI),
    "execution_get": lambda g, p: g.execution_get(p.get("execution_id")),
    "hand_stop": lambda g, p: g.stop(caller=gw_mod.AI),
}
OPERATOR_METHODS = {
    "hand_status": AI_METHODS["hand_status"],
    "execution_get": AI_METHODS["execution_get"],
    "skill_list": AI_METHODS["skill_list"],
    "pending": lambda g, p: g.pending(),
    "approve": lambda g, p: g.approve(p.get("proposal_id"), caller=gw_mod.OPERATOR),
    "stop": lambda g, p: g.stop(caller=gw_mod.OPERATOR),
    "clear_fault": lambda g, p: g.clear_fault(caller=gw_mod.OPERATOR),
    "log": lambda g, p: g.log_tail(p.get("n", 20)),
}


def handle_line(gateway, methods, line):
    """處理一行請求，回傳一行回應（不含換行）。任何錯誤都變成結構化回應，不讓連線崩掉。"""
    req_id = None
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("請求要是 JSON 物件")
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params 要是物件")
        fn = methods.get(method)
        if fn is None:
            result = {"status": "rejected", "reason_code": "METHOD_NOT_ALLOWED",
                      "detail": "這個 socket 不提供 %r" % (method,)}
        else:
            result = fn(gateway, params)
    except Exception as e:
        result = {"status": "error", "reason_code": "BAD_REQUEST" if isinstance(e, ValueError) else "INTERNAL_ERROR",
                  "detail": "%s: %s" % (type(e).__name__, e)}
    return json.dumps({"id": req_id, "result": result}, ensure_ascii=False, default=str)


def _make_handler(gateway, methods):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(CONNECTION_TIMEOUT_S)
            while True:
                try:
                    line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                except OSError:          # 逾時或對方斷線
                    return
                if not line:
                    return
                if len(line) > MAX_REQUEST_BYTES:
                    self.wfile.write(b'{"id": null, "result": {"status": "error", "reason_code": "REQUEST_TOO_LARGE"}}\n')
                    return
                self.wfile.write(handle_line(gateway, methods, line).encode("utf-8") + b"\n")
                self.wfile.flush()
    return Handler


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _bind(path, handler):
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o177)
    try:
        srv = _Server(path, handler)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return srv


class HandDaemon:
    def __init__(self, gateway, runtime_dir):
        self.gateway = gateway
        self.runtime_dir = runtime_dir
        os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        os.chmod(runtime_dir, 0o700)
        self.ai_path = os.path.join(runtime_dir, "ai.sock")
        self.op_path = os.path.join(runtime_dir, "operator.sock")
        self.servers = []
        self.threads = []
        self._lock_fd = None

    def start(self):
        # 同一個 runtime 目錄只能有一個 handd，否則第二個會搶走 socket，hand stop 會送到錯的那個。
        fd = os.open(os.path.join(self.runtime_dir, "handd.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning("已經有一個 handd 在執行（%s）。先在那個終端機按 Ctrl-C。" % self.runtime_dir)
        self._lock_fd = fd
        for path, methods in ((self.ai_path, AI_METHODS), (self.op_path, OPERATOR_METHODS)):
            srv = _bind(path, _make_handler(self.gateway, methods))
            t = threading.Thread(target=srv.serve_forever, name="handd-" + os.path.basename(path), daemon=True)
            t.start()
            self.servers.append(srv)
            self.threads.append(t)

    def shutdown(self):
        try:
            self.gateway.stop(caller="handd")
        finally:
            for srv in self.servers:
                srv.shutdown()
                srv.server_close()
            for path in (self.ai_path, self.op_path):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None


def find_port():
    patterns = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wch*", "/dev/ttyACM*", "/dev/ttyUSB*")
    ports = sorted({p for pat in patterns for p in glob.glob(pat)})
    return ports


def main(argv=None):
    ap = argparse.ArgumentParser(prog="handd", description="Amazing Hand 右手的 skill gateway 常駐程式（ADR-0006）")
    ap.add_argument("--adapter", choices=("fake", "scs"), default="fake",
                    help="fake：模擬（P2 動作只在這裡執行）；scs：接實體伺服機（只讀，動作一律拒絕）")
    ap.add_argument("--port", help="序列埠；不給就自動找唯一的那個")
    ap.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)
    ap.add_argument("--log-dir", default=os.path.join(hand_config.RIGHT_HAND_DIR, "logs"))
    args = ap.parse_args(argv)

    cfg = hand_config.load()
    if args.adapter == "fake":
        adapter = FakeHandAdapter(positions=motion.open_targets(cfg.middle_offsets, hand_config.SERVO_IDS))
    else:
        port = args.port
        if not port:
            ports = find_port()
            if len(ports) != 1:
                print("找到 %d 個序列埠：%s。請用 --port 指定。" % (len(ports), ports))
                return 1
            port = ports[0]
        adapter = ScsHandAdapter(port)
        # 啟動時一律先關 8 顆扭力（ADR-0006 §安全狀態）。拿不到匯流排就照實說，不假裝成功。
        try:
            with adapter.connected():
                failed = motion.torque_off(adapter, hand_config.SERVO_IDS)
            print("啟動：已對 8 顆送出關扭力%s。" % ("" if not failed else "（這些 ID 沒有回應：%s）" % failed))
        except AdapterError as e:
            print("啟動：沒有辦法關扭力（%s）。手如果有扭力，請切斷伺服機電源。" % e)

    os.makedirs(args.log_dir, exist_ok=True)
    audit = gw_mod.AuditLog(os.path.join(args.log_dir, "handd_events.jsonl"))
    gateway = gw_mod.Gateway(cfg, adapter, audit)
    audit.write("handd.started", "handd", None, adapter=adapter.kind, graph_revision=cfg.graph_revision,
                calibration_revision=cfg.calibration_revision, real_motion_enabled=gateway.real_motion_enabled)
    daemon = HandDaemon(gateway, args.runtime_dir)
    try:
        daemon.start()
    except AlreadyRunning as e:
        print(e)
        return 1
    print("handd 已啟動：adapter=%s、模擬=%s、實機動作=%s" % (adapter.kind, not adapter.real_hardware,
                                                       "允許" if gateway.real_motion_enabled else "拒絕"))
    print("AI socket：%s" % daemon.ai_path)
    print("操作者 socket：%s" % daemon.op_path)
    print("稽核紀錄：%s" % audit.path)
    print("Ctrl-C 結束（結束時會取消進行中的動作並關扭力）。")

    done = threading.Event()

    def _stop(*_):
        done.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    done.wait()
    print("\n結束中：取消進行中的動作並關扭力…")
    daemon.shutdown()
    audit.write("handd.stopped", "handd", None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
