"""FastAPI app: serves the dashboard and exposes engine state + controls."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.strategy.engine import Engine

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="0DTE Long-Only Auto-Trader")
engine = Engine()


@app.on_event("startup")
def _startup() -> None:
    engine.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    engine.stop()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/api/state")
def state() -> JSONResponse:
    return JSONResponse(engine.snapshot())


@app.get("/api/trades.json")
def trades_json() -> JSONResponse:
    return JSONResponse({"trades": engine.trades.all(), "summary": engine.trades.summary()})


@app.get("/api/trades.csv")
def trades_csv() -> Response:
    return Response(
        content=engine.trades.to_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=0dte_trades.csv"},
    )


@app.get("/api/config")
def config() -> dict:
    return {
        "mode": settings.data_mode, "dry_run": settings.dry_run,
        "tickers": settings.tickers, "contracts": settings.contracts,
        "proximity": settings.proximity,
        "gamma_base_url": settings.gamma_base_url, "mm_base_url": settings.mm_base_url,
    }


@app.post("/api/control/{action}")
def control(action: str) -> dict:
    if action == "start":
        engine.start()
    elif action == "stop":
        engine.stop()
    elif action == "kill":
        engine.kill_switch()
    elif action == "auto_on":
        engine.auto_trade = True
    elif action == "auto_off":
        engine.auto_trade = False
    elif action == "clear_trades":
        removed = engine.trades.clear()
        return {"ok": True, "cleared": removed}
    else:
        return {"ok": False, "error": f"unknown action '{action}'"}
    return {"ok": True, "running": engine.running, "auto_trade": engine.auto_trade}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn
    uvicorn.run("app.server:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
