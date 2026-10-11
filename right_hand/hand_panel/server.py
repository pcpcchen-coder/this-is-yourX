"""操作面板的 HTTP 伺服器（只用標準函式庫）。

  python -m hand_panel.server                    模擬（預設）
  python -m hand_panel.server --adapter scs      接實體伺服機
  python -m hand_panel.server --print-url        印出網址後結束

預設不需要存取碼（2026-10-08 George 決定：只在自己的內網使用）。也就是說，同一個區域網路上任何人
開得了這一頁，就能啟用扭力、移動手指。仍然擋掉的是「別的網站借某台電腦的瀏覽器送請求」：
POST 要同源的 Origin 與 JSON，Host 要是 IP 位址或這台機器的名稱。

要加回存取碼：--require-token。第一次啟動時產生，存在 ~/.this-is-yourx/hand/panel_token（0600）；
瀏覽器第一次用「網址?token=…」開啟，之後靠 cookie，沒有存取碼的請求一律 401。

這是明文 HTTP，只適合自己的工作台網路；伺服機電源上的實體開關才是權威的停止方式。
"""
from __future__ import annotations

import argparse
import hmac
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from hand_api import config as hand_config
from hand_api import motion
from hand_api.adapter import FakeHandAdapter, ScsHandAdapter
from hand_api.daemon import DEFAULT_RUNTIME_DIR, find_port
from hand_api.gateway import AuditLog

from .core import HOLD_TIMEOUT_S, MARKER_NAME, Panel, Rejected
from .poses import PoseStore

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
COOKIE = "hand_panel"
COOKIE_MAX_AGE_S = 30 * 24 * 3600
MAX_BODY_BYTES = 16 * 1024
CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
DEFAULT_HTTP_PORT = 8765
TOKEN_FILE = "panel_token"

# 路徑 → (除了 client 以外允許的欄位, 呼叫)。多出來的欄位一律拒絕。
POST_ROUTES = {
    "/api/enable": ({"speed"}, lambda p, c, b: p.enable(c, b.get("speed", "slow"))),
    "/api/stop": (set(), lambda p, c, b: p.stop(c)),
    "/api/clear_fault": (set(), lambda p, c, b: p.clear_fault(c)),
    "/api/speed": ({"speed"}, lambda p, c, b: p.set_speed(c, b.get("speed"))),
    "/api/fingers": ({"fingers"}, lambda p, c, b: p.set_fingers(c, b.get("fingers"))),
    "/api/open": (set(), lambda p, c, b: p.open_hand(c)),
    "/api/poses/save": ({"name", "label", "note", "overwrite"},
                        lambda p, c, b: p.save_pose(c, b.get("name"), b.get("label", ""), b.get("note", ""),
                                                    b.get("overwrite", False))),
    "/api/poses/play": ({"name"}, lambda p, c, b: p.play_pose(c, b.get("name"))),
    "/api/poses/delete": ({"name"}, lambda p, c, b: p.delete_pose(c, b.get("name"))),
    "/api/poses/reconfirm": ({"name"}, lambda p, c, b: p.reconfirm_pose(c, b.get("name"))),
}

UNAUTHORIZED_PAGE = """<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>右手操作面板</title>
<body style="font-family: system-ui, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.7">
<h1 style="font-size: 1.3rem">需要存取碼</h1>
<p>這一頁可以讓手動起來，所以要用含存取碼的網址開啟。在接著手的那台機器上執行：</p>
<pre style="background: #eef1f0; padding: .8rem; overflow: auto">bash ~/this-is-yourX/right_hand/tools/hand_panel.sh url</pre>
<p>把印出來的網址貼到瀏覽器。開過一次之後，這個瀏覽器 30 天內不用再帶存取碼。</p>
</body></html>
"""


def load_or_create_token(runtime_dir):
    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    path = os.path.join(runtime_dir, TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if len(token) >= 16:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)
    return token


def lan_address():
    """這台機器對外的 IPv4 位址（不會真的送出封包）；找不到就回 None。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _same(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "hand-panel"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, fmt, *args):       # 每 0.25 秒一次的狀態查詢不進紀錄
        pass

    # ------------------------------------------------------------------ 回應
    def _send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code, reason_code, detail):
        self._json(code, {"status": "error", "reason_code": reason_code, "detail": detail})

    # ------------------------------------------------------------------ 檢查
    def _host_ok(self):
        """Host 要是 IP 位址或這台機器的名稱。

        擋的是 DNS rebinding：別的網站把自己的網域指到這台機器，再借區域網路裡某台電腦的瀏覽器
        送「同源」請求。沒有存取碼時，這是那一類請求唯一過不了的檢查。
        """
        try:
            name = urlsplit("//" + self.headers.get("Host", "")).hostname
        except ValueError:
            name = None
        if not name:
            return False
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            return name in self.server.allowed_hosts

    def _authed(self):
        token = self.server.token
        if token is None:                    # 預設：不需要存取碼
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and _same(auth[7:].strip(), token):
            return True
        try:
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        morsel = jar.get(COOKIE)
        return morsel is not None and _same(morsel.value, token)

    def _client(self, value):
        if not isinstance(value, str) or not CLIENT_RE.match(value):
            return None
        return value

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if not self._host_ok():
            self._error(403, "BAD_HOST", "請用這台機器的 IP 位址或主機名稱開啟")
            return
        if u.path == "/" and "token" in q:
            if self.server.token is None:        # 不需要存取碼：舊書籤上的 token 直接丟掉
                self._send(303, b"", "text/plain; charset=utf-8", [("Location", "/")])
            elif _same(q["token"][0], self.server.token):
                cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
                    COOKIE, self.server.token, COOKIE_MAX_AGE_S)
                self._send(303, b"", "text/plain; charset=utf-8", [("Location", "/"), ("Set-Cookie", cookie)])
            else:
                self._send(401, UNAUTHORIZED_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not self._authed():
            if u.path.startswith("/api/"):
                self._error(401, "UNAUTHORIZED", "沒有存取碼")
            else:
                self._send(401, UNAUTHORIZED_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        panel = self.server.panel
        if u.path in STATIC:
            name, ctype = STATIC[u.path]
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    body = f.read()
            except OSError:
                self._error(500, "INTERNAL_ERROR", "找不到 %s" % name)
                return
            self._send(200, body, ctype)
        elif u.path == "/api/status":
            client = self._client(q.get("client", [None])[0])
            if client:
                panel.beat(client, self.client_address[0])
            self._json(200, panel.status(client))
        elif u.path == "/api/poses":
            self._json(200, panel.list_poses())
        elif u.path == "/api/log":
            self._json(200, panel.log_tail(q.get("n", ["20"])[0]))
        else:
            self._error(404, "NOT_FOUND", "沒有這個路徑")

    do_HEAD = do_GET

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        u = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._error(413, "REQUEST_TOO_LARGE", "請求內容太大")
            return
        raw = self.rfile.read(length) if length else b""
        if not self._host_ok():
            self._error(403, "BAD_HOST", "請用這台機器的 IP 位址或主機名稱開啟")
            return
        if not self._authed():
            self._error(401, "UNAUTHORIZED", "沒有存取碼")
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host", ""):
            self._error(403, "CROSS_ORIGIN", "這個請求不是從面板自己的頁面送出的")
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._error(415, "BAD_REQUEST", "Content-Type 要是 application/json")
            return
        route = POST_ROUTES.get(u.path)
        if route is None:
            self._error(404, "NOT_FOUND", "沒有這個路徑")
            return
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._error(400, "BAD_REQUEST", "內容不是 JSON")
            return
        if not isinstance(body, dict):
            self._error(400, "BAD_REQUEST", "內容要是 JSON 物件")
            return
        client = self._client(body.get("client"))
        if client is None:
            self._error(400, "BAD_REQUEST", "client 要是 8–40 個英數字元")
            return
        allowed, fn = route
        extra = set(body) - allowed - {"client"}
        panel = self.server.panel
        try:
            if extra:
                raise Rejected("PARAMETER_NOT_ALLOWED", "不接受這些欄位：%s" % "、".join(sorted(extra)))
            if u.path != "/api/stop":
                panel.beat(client, self.client_address[0])
            self._json(200, fn(panel, client, body))
        except Rejected as e:
            self._json(200, {"status": "rejected", "reason_code": e.reason_code, "detail": e.detail})
        except Exception as e:               # 不讓一個壞請求弄掉連線以外的東西
            self._error(500, "INTERNAL_ERROR", "%s: %s" % (type(e).__name__, e))


class PanelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, panel, token=None, allowed_hosts=()):
        super().__init__(address, Handler)
        self.panel = panel
        self.token = token                   # None：不需要存取碼
        name = socket.gethostname().lower()
        self.allowed_hosts = {"localhost", name, name + ".local"} | {h.lower() for h in allowed_hosts}


def build_adapter(kind, port, cfg):
    if kind == "fake":
        return FakeHandAdapter(positions=motion.open_targets(cfg.middle_offsets, hand_config.SERVO_IDS)), None
    if not port:
        ports = find_port()
        if len(ports) != 1:
            return None, "找到 %d 個序列埠：%s。請用 --port 指定。" % (len(ports), ports)
        port = ports[0]
    return ScsHandAdapter(port), None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hand_panel", description="Amazing Hand 右手的網頁操作面板（人工操作）")
    ap.add_argument("--adapter", choices=("fake", "scs"), default="fake",
                    help="fake：模擬（預設）；scs：接實體伺服機")
    ap.add_argument("--port", help="序列埠；不給就自動找唯一的那個")
    ap.add_argument("--host", default="0.0.0.0", help="監聽位址；只想本機用就給 127.0.0.1")
    ap.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    ap.add_argument("--runtime-dir", default=DEFAULT_RUNTIME_DIR)
    ap.add_argument("--log-dir", default=os.path.join(hand_config.RIGHT_HAND_DIR, "logs"))
    ap.add_argument("--poses", default=os.path.join(hand_config.CONFIG_DIR, "poses.yaml"))
    ap.add_argument("--hold-timeout", type=float, default=HOLD_TIMEOUT_S,
                    help="有扭力但沒有新目標這麼多秒就關扭力（預設 %(default)s）")
    ap.add_argument("--require-token", action="store_true",
                    help="要存取碼才能用（預設不用：同一個區域網路上的人都能操作）")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="除了 IP 位址與這台機器的主機名稱以外，還接受用這個名稱開啟（可重複）")
    ap.add_argument("--print-url", action="store_true", help="印出網址後結束")
    args = ap.parse_args(argv)

    token = load_or_create_token(args.runtime_dir) if args.require_token else None
    if args.print_url:
        host = args.host if args.host not in ("0.0.0.0", "::") else (lan_address() or "127.0.0.1")
        print("http://%s:%d/%s" % (host, args.http_port, "?token=" + token if token else ""))
        return 0
    if not (5.0 <= args.hold_timeout <= 600.0):
        print("--hold-timeout 要在 5 到 600 秒之間")
        return 1

    cfg = hand_config.load()
    adapter, problem = build_adapter(args.adapter, args.port, cfg)
    if adapter is None:
        print(problem)
        return 1
    os.makedirs(args.log_dir, exist_ok=True)
    audit = AuditLog(os.path.join(args.log_dir, "panel_events.jsonl"))
    panel = Panel(cfg, adapter, audit, PoseStore(args.poses), hold_timeout_s=args.hold_timeout,
                  marker_path=os.path.join(args.runtime_dir, MARKER_NAME))
    try:
        httpd = PanelHTTPServer((args.host, args.http_port), panel, token, args.allow_host)
    except OSError as e:
        print("開不了 %s:%d：%s" % (args.host, args.http_port, e))
        return 1
    panel.start()
    audit.write("panel.started", "panel", None, adapter=adapter.kind, host=args.host, http_port=args.http_port,
                calibration_revision=cfg.calibration_revision, hold_timeout_s=args.hold_timeout,
                token_required=token is not None)
    print("面板已啟動：adapter=%s、模擬=%s、監聽 %s:%d" % (adapter.kind, not adapter.real_hardware,
                                                   args.host, args.http_port))
    print("啟動時沒有對伺服機寫入任何東西；有頁面開著才會佔用匯流排。")
    if token is None:
        print("不需要存取碼：同一個區域網路上開得了這一頁的人都能操作。網址：bash right_hand/tools/hand_panel.sh url")
    else:
        print("需要存取碼。網址：bash right_hand/tools/hand_panel.sh url --require-token")
    print("稽核紀錄：%s" % audit.path)
    sys.stdout.flush()

    def _stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        print("結束中：有扭力就關…")
        panel.shutdown()
        httpd.server_close()
        audit.write("panel.stopped", "panel", None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
