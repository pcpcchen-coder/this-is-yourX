"""連 handd 的 socket，送一個請求、收一個回應。"""
from __future__ import annotations

import itertools
import json
import os
import socket

from .daemon import DEFAULT_RUNTIME_DIR

_ids = itertools.count(1)


class HanddUnavailable(Exception):
    pass


def socket_path(kind, runtime_dir=None):
    runtime_dir = runtime_dir or os.environ.get("HANDD_RUNTIME_DIR") or DEFAULT_RUNTIME_DIR
    return os.path.join(runtime_dir, {"ai": "ai.sock", "operator": "operator.sock"}[kind])


def call(path, method, params=None, timeout_s=60.0):
    req = {"id": next(_ids), "method": method, "params": params or {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(path)
            s.sendall(json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise HanddUnavailable("handd 沒有在執行（%s）" % e)
    except OSError as e:
        raise HanddUnavailable("連不上 handd：%s" % e)
    if not buf:
        raise HanddUnavailable("handd 沒有回應")
    return json.loads(buf.decode("utf-8"))["result"]
