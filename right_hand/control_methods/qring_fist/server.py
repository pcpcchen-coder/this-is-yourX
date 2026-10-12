"""QRing Hand 的本機服務：只聽 127.0.0.1，給 APP 的視窗與選單列用。"""
import argparse
import asyncio
import hashlib
import json
import os
import signal
from pathlib import Path

from aiohttp import web

from engine import Engine

ROOT = Path(__file__).resolve().parent
ASSETS = ("app.js", "app.css")
ENGINE = web.AppKey("engine", Engine)
SHUTDOWN = web.AppKey("shutdown", asyncio.Event)


async def create_app(folder, port, fake_ring=False, self_test=False):
    site = None
    try:
        site = json.loads((ROOT / "site.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    factory = None
    if fake_ring:
        from sim import FakeRingTransport
        factory = lambda: FakeRingTransport(demo=True, noise=0.004)
    engine = Engine(folder, ring_transport_factory=factory, site=site, check_studio=not (fake_ring or self_test))

    @web.middleware
    async def guard(req, handler):
        if req.host not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            raise web.HTTPForbidden()
        origin = req.headers.get("Origin")
        if origin and origin not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
            raise web.HTTPForbidden()
        if req.method == "POST" and req.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            return await handler(req)
        except (ValueError, KeyError, TypeError, RuntimeError) as e:
            return web.json_response({"error": str(e) or type(e).__name__}, status=400)

    app = web.Application(middlewares=[guard], client_max_size=32768)
    app[ENGINE] = engine
    app[SHUTDOWN] = asyncio.Event()
    closed = []
    no_store = {"Cache-Control": "no-store"}

    async def body(req):
        data = await req.json() if req.can_read_body else {}
        if not isinstance(data, dict):
            raise ValueError("內容要是 JSON 物件")
        return data

    def ok(**extra):
        return web.json_response(dict(ok=True, **extra), headers=no_store)

    async def state(req):
        return web.json_response(engine.snapshot(), headers=no_store)

    async def menu_status(req):
        return web.json_response(engine.menu_status(), headers=no_store)

    async def ring_connect(req):
        await engine.ring_connect()
        return ok()

    async def ring_disconnect(req):
        await engine.ring_disconnect()
        return ok()

    async def hand_link(req):
        data = await body(req)
        await engine.hand_link(data.get("url"))
        return ok()

    async def hand_unlink(req):
        await engine.hand_unlink()
        return ok()

    async def hand_clear_fault(req):
        await engine.hand_clear_fault()
        return ok()

    async def follow_start(req):
        await engine.arm()
        return ok()

    async def follow_stop(req):
        return ok(confirmed=await engine.stop())

    async def cal_start(req):
        engine.cal_start()
        return ok()

    async def cal_capture(req):
        engine.cal_capture((await body(req)).get("pose"))
        return ok()

    async def cal_retake(req):
        engine.cal_retake((await body(req)).get("pose"))
        return ok()

    async def cal_save(req):
        engine.cal_save()
        return ok()

    async def cal_cancel(req):
        engine.cal_cancel()
        return ok()

    async def options(req):
        engine.set_options(await body(req))
        return ok()

    async def polling_reset(req):
        engine.ring.reset_stats()
        return ok()

    async def shutdown(req):
        closed.append(True)
        await engine.close()
        app[SHUTDOWN].set()
        return ok()

    for path, handler in (("/api/state", state), ("/api/menu-status", menu_status)):
        app.router.add_get(path, handler)
    for path, handler in (("/api/ring/connect", ring_connect), ("/api/ring/disconnect", ring_disconnect),
                          ("/api/hand/link", hand_link), ("/api/hand/unlink", hand_unlink),
                          ("/api/hand/clear-fault", hand_clear_fault),
                          ("/api/follow/start", follow_start), ("/api/follow/stop", follow_stop),
                          ("/api/calibration/start", cal_start), ("/api/calibration/capture", cal_capture),
                          ("/api/calibration/retake", cal_retake), ("/api/calibration/save", cal_save),
                          ("/api/calibration/cancel", cal_cancel),
                          ("/api/options", options), ("/api/ring/polling/reset", polling_reset),
                          ("/api/shutdown", shutdown)):
        app.router.add_post(path, handler)

    web_root = ROOT / "web"
    version = hashlib.sha256(b"".join((web_root / name).read_bytes() for name in ASSETS)).hexdigest()[:12]
    page = (web_root / "index.html").read_text(encoding="utf-8")
    for name in ASSETS:
        page = page.replace('"%s"' % name, '"%s?v=%s"' % (name, version))

    async def index(req):
        return web.Response(text=page, content_type="text/html", headers=no_store)

    app.router.add_get("/", index)
    app.router.add_static("/", web_root)

    async def cleanup(app):
        if not closed:
            await engine.close()

    app.on_cleanup.append(cleanup)
    await engine.start(auto_link=not self_test)
    return app


async def main(args):
    app = await create_app(args.data_dir, args.port, fake_ring=args.fake_ring, self_test=args.self_test)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    stop = app[SHUTDOWN]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):        # 被要求結束時也走正常收尾：關手掌扭力、還原戒指設定
        loop.add_signal_handler(sig, stop.set)
    watcher = None
    if args.watch_parent:
        parent = os.getppid()

        async def watch():                             # 視窗那個程序不見了（當掉、被強制結束）就跟著收尾
            while os.getppid() == parent:
                await asyncio.sleep(1.0)
            stop.set()
        watcher = asyncio.ensure_future(watch())
    print("READY http://127.0.0.1:%d" % args.port, flush=True)
    try:
        await stop.wait()
    finally:
        if watcher:
            watcher.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18775)
    parser.add_argument("--data-dir", default=str(Path.home() / "Library" / "Application Support" / "QRing Hand"))
    parser.add_argument("--fake-ring", action="store_true", help="用模擬戒指（示範與開發）")
    parser.add_argument("--self-test", action="store_true", help="封裝自我檢查：不連手掌、不檢查 QRing Studio")
    parser.add_argument("--watch-parent", action="store_true", help="啟動它的程序結束時跟著結束")
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
