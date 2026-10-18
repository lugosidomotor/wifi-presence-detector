"""FastAPI UI and API. Writes take JSON bodies: the CORS preflight keeps other websites from arming/disarming."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import mimetypes
from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .runtime import Runtime

STATIC = Path(__file__).parent / "static"
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")


class Arming(BaseModel):
    armed: bool


class Calibration(BaseModel):
    seconds: float | None = None
    cancel: bool = False


class SimAction(BaseModel):
    action: str


def create_app(cfg, runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = runtime or Runtime(cfg)
        app.state.runtime.start()
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.runtime.close)

    app = FastAPI(title="WiFi Sense", lifespan=lifespan, docs_url=None, redoc_url=None)
    # Set on SIGINT/SIGTERM (see main.Server): open event streams end at once instead of holding shutdown.
    app.state.stopping = threading.Event()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=604800"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health(request: Request):
        _, latest = request.app.state.runtime.snapshot()
        state = latest["state"] if latest else "starting"
        return JSONResponse({"status": "error" if state == "offline" else "ok", "state": state},
                            status_code=503 if state == "offline" else 200)

    @app.get("/api/status")
    def status(request: Request):
        return request.app.state.runtime.status()

    @app.get("/api/live")
    def live(request: Request, seconds: float = Query(120, ge=5, le=600)):
        return {"points": request.app.state.runtime.live(seconds)}

    @app.get("/api/stream")
    async def stream(request: Request):
        rt = request.app.state.runtime

        async def events():
            seen, idle = -1, 0.0
            while not request.app.state.stopping.is_set():
                if await request.is_disconnected():
                    return
                seq, latest = rt.snapshot()
                if latest is not None and seq != seen:
                    seen, idle = seq, 0.0
                    yield f"data: {json.dumps(latest, separators=(',', ':'))}\n\n"
                else:
                    idle += 0.2
                    if idle >= 15:  # keep proxies (Tailscale Serve, nginx) from closing an idle stream
                        idle = 0.0
                        yield ": keepalive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/events")
    def events(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        rt = request.app.state.runtime
        return {"events": rt.store.list_events(limit, offset), "limit": limit, "offset": offset}

    @app.post("/api/arm")
    def arm(request: Request, body: Arming):
        try:
            return request.app.state.runtime.arm(body.armed)
        except OSError:
            raise HTTPException(500, "Az élesítés nem menthető") from None

    @app.post("/api/calibrate")
    def calibrate(request: Request, body: Calibration):
        rt = request.app.state.runtime
        if body.cancel:
            rt.cancel_calibration()
            return {"cancelled": True}
        try:
            return rt.start_calibration(body.seconds)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/notify-test")
    def notify_test(request: Request):
        return {"result": request.app.state.runtime.test_notification()}

    @app.post("/api/sim")
    def sim(request: Request, body: SimAction):
        try:
            return request.app.state.runtime.sim_action(body.action)
        except LookupError:
            raise HTTPException(404, "Csak szimulált módban") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    if cfg["web"]["enabled"]:
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/")
        def page():
            return FileResponse(STATIC / "index.html")

    return app
