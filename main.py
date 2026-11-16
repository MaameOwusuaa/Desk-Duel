"""Desk Duel - FastAPI server.

Start it from this folder with:
    uvicorn main:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 on the host computer.
"""
import logging
import re
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import config
import db
import game as engine
from db import run_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quiz")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"



@asynccontextmanager
async def lifespan(_app):
    try:
        await run_db(db.ping)
        log.info("Connected to MySQL database '%s' on %s", config.DB_NAME, config.DB_HOST)
    except Exception as exc:
        log.error("Could not connect to MySQL: %s", exc)
        log.error("Check DB_HOST / DB_USER / DB_PASSWORD / DB_NAME in backend/.env")
    yield


app = FastAPI(title="Desk Duel", lifespan=lifespan)


# ---------------------------------------------------------------- models

def clean_name(value: str, max_len: int) -> str:
    value = re.sub(r"\s+", " ", (value or "")).strip()
    if not value:
        raise ValueError("Name cannot be empty")
    return value[:max_len]


class CreateGameIn(BaseModel):
    host_name: str = Field(..., max_length=50)
    category: Optional[str] = None
    num_questions: int = Field(config.DEFAULT_NUM_QUESTIONS, ge=1, le=50)
    time_limit: int = Field(config.DEFAULT_TIME_LIMIT, ge=5, le=120)

    @field_validator("host_name")
    @classmethod
    def _host(cls, v):
        return clean_name(v, 50)


class JoinIn(BaseModel):
    name: str = Field(..., max_length=40)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return clean_name(v, 20)


class QuestionIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=50)
    question_text: str = Field(..., min_length=1, max_length=500)
    option_a: str = Field(..., min_length=1, max_length=200)
    option_b: str = Field(..., min_length=1, max_length=200)
    option_c: str = Field(..., min_length=1, max_length=200)
    option_d: str = Field(..., min_length=1, max_length=200)
    correct_option: str = Field(..., pattern="^[ABCDabcd]$")

    @field_validator("category", "question_text", "option_a", "option_b", "option_c", "option_d")
    @classmethod
    def _strip(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("cannot be blank")
        return v

    @field_validator("correct_option")
    @classmethod
    def _upper(cls, v):
        return v.upper()


class ImportIn(BaseModel):
    csv: str


class PinIn(BaseModel):
    pin: str = ""


def require_admin(x_admin_pin: str = Header(default="")):
    if config.ADMIN_PIN and x_admin_pin != config.ADMIN_PIN:
        raise HTTPException(401, "Wrong admin PIN")


def require_host_pin(x_host_pin: str = Header(default="")):
    if not secrets.compare_digest(x_host_pin, config.HOST_PIN):
        raise HTTPException(401, "Wrong host PIN")


def get_live_game(code: str) -> engine.Game:
    g = engine.GAMES.get(code.strip())
    if not g:
        raise HTTPException(404, "No game found with that code. Check the code on the big screen.")
    return g


# ---------------------------------------------------------------- general

def lan_ip() -> str:
    """The computer's address on the office network (no traffic is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@app.get("/api/health")
async def health():
    try:
        ok = await run_db(db.ping)
    except Exception as exc:
        return {"ok": False, "database": str(exc)}
    return {"ok": ok, "live_games": len(engine.GAMES)}


@app.get("/api/server-info")
async def server_info(request: Request):
    if config.PUBLIC_URL:
        base = config.PUBLIC_URL
    else:
        host = request.url.hostname or "localhost"
        port = request.url.port
        if host in ("localhost", "127.0.0.1", "::1"):
            host = lan_ip()
        base = f"{request.url.scheme}://{host}" + (f":{port}" if port else "")
    return {"join_url": base, "min_players": config.MIN_PLAYERS, "max_players": config.MAX_PLAYERS,
            "admin_pin_required": bool(config.ADMIN_PIN)}


# ---------------------------------------------------------------- question bank

@app.get("/api/categories")
async def categories():
    try:
        rows = await run_db(db.list_categories)
    except Exception as exc:
        log.exception("Unable to load question categories from MySQL")
        raise HTTPException(
            503,
            "Database unavailable. Check that MySQL is running and DB_HOST/DB_PORT in backend/.env are correct.",
        ) from exc
    total = sum(r["total"] for r in rows)
    return {"categories": rows, "total": total}


@app.post("/api/host/check")
async def check_host_pin(body: PinIn):
    if not secrets.compare_digest(body.pin, config.HOST_PIN):
        raise HTTPException(401, "Wrong host PIN")
    return {"ok": True}


@app.get("/api/questions")
async def questions(category: Optional[str] = None):
    return await run_db(db.list_questions, category or None)


@app.post("/api/admin/check")
async def admin_check(body: PinIn):
    if config.ADMIN_PIN and body.pin != config.ADMIN_PIN:
        raise HTTPException(401, "Wrong admin PIN")
    return {"ok": True}


@app.post("/api/questions", dependencies=[Depends(require_admin)])
async def add_question(q: QuestionIn):
    new_id = await run_db(db.add_question, q.model_dump())
    return {"id": new_id}


@app.put("/api/questions/{question_id}", dependencies=[Depends(require_admin)])
async def update_question(question_id: int, q: QuestionIn):
    await run_db(db.update_question, question_id, q.model_dump())
    return {"ok": True}


@app.delete("/api/questions/{question_id}", dependencies=[Depends(require_admin)])
async def delete_question(question_id: int):
    await run_db(db.delete_question, question_id)
    return {"ok": True}


@app.post("/api/questions/import", dependencies=[Depends(require_admin)])
async def import_questions(body: ImportIn):
    added, errors = await run_db(db.import_questions_csv, body.csv)
    return {"added": added, "errors": errors}


# ---------------------------------------------------------------- games

@app.post("/api/games", dependencies=[Depends(require_host_pin)])
async def create_game(body: CreateGameIn):
    category = (body.category or "").strip() or None
    available = await run_db(db.count_questions, category)
    if available == 0:
        raise HTTPException(400, "There are no questions in that category yet. Add some in the Question Bank.")
    g = await engine.create_game(body.host_name, category, min(body.num_questions, available), body.time_limit)
    return {"code": g.code, "host_token": g.host_token, "num_questions": len(g.questions),
            "time_limit": g.time_limit}


@app.get("/api/games/{code}")
async def game_status(code: str):
    g = get_live_game(code)
    return {"code": g.code, "phase": g.phase, "host_name": g.host_name,
            "players": len(g.players), "max_players": config.MAX_PLAYERS,
            "joinable": g.phase == "lobby" and len(g.players) < config.MAX_PLAYERS}


@app.post("/api/games/{code}/join")
async def join(code: str, body: JoinIn):
    g = get_live_game(code)
    player, error = await engine.join_game(g, body.name)
    if error:
        raise HTTPException(400, error)
    return {"player_id": player.id, "token": player.token, "name": player.name, "code": g.code}


@app.get("/api/history")
async def history():
    return await run_db(db.recent_games, 20)


# ---------------------------------------------------------------- websockets

@app.websocket("/ws/host/{code}")
async def host_socket(ws: WebSocket, code: str, token: str = ""):
    await ws.accept()
    g = engine.GAMES.get(code)
    if not g or token != g.host_token:
        await ws.send_json({"type": "fatal", "message": "This game no longer exists, or you are not its host."})
        await ws.close()
        return
    await g.connect_host(ws)
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            error = None
            if action == "start":
                error = await g.start()
            elif action == "next":
                await g.next_question()
            elif action == "reveal_now":
                await g.end_question()
            elif action == "end":
                await g.finish()
            elif action == "kick":
                error = await g.kick(int(msg.get("player_id", 0)))
            elif action == "ping":
                await ws.send_json({"type": "pong"})
            if error:
                await ws.send_json({"type": "error", "message": error})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("Host socket error in game %s: %s", code, exc)
    finally:
        await g.disconnect_host(ws)


@app.websocket("/ws/player/{code}")
async def player_socket(ws: WebSocket, code: str, player_id: int = 0, token: str = ""):
    await ws.accept()
    g = engine.GAMES.get(code)
    p = g.players.get(player_id) if g else None
    if not g or not p or p.token != token:
        await ws.send_json({"type": "fatal", "message": "We couldn't find you in this game. Please join again."})
        await ws.close()
        return
    await g.connect_player(p, ws)
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "answer":
                await g.submit_answer(p, msg.get("option", ""))
            elif action == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("Player socket error in game %s: %s", code, exc)
    finally:
        await g.disconnect_player(p, ws)


# Serve the HTML/CSS/JS pages. Must be registered last.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
