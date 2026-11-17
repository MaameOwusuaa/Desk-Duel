"""Desk Duel - FastAPI server.

Start it from this folder with:
    uvicorn main:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 on the host computer.
"""
import logging
import os
import re
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

import config
import db
import live_db
from db import run_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quiz")

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent if (APP_DIR.parent / "frontend").is_dir() else APP_DIR
FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", str(PROJECT_ROOT / "frontend"))).resolve()
INDEX_FILE = Path(os.getenv("INDEX_FILE", str(PROJECT_ROOT / "index.html"))).resolve()



@asynccontextmanager
async def lifespan(_app):
    try:
        await run_db(db.ping)
        log.info("Connected to MySQL database '%s' on %s", config.DB_NAME, config.DB_HOST)
    except Exception as exc:
        log.error("Could not connect to MySQL: %s", exc)
        log.error(
            "Set DATABASE_URL (or DB_HOST / DB_USER / DB_PASSWORD / DB_NAME) in the deployment environment. "
            "backend/.env is only used for local development."
        )
    yield


app = FastAPI(title="Desk Duel", lifespan=lifespan)
if config.FRONTEND_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.FRONTEND_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Admin-Pin", "X-Host-Pin"],
    )


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


class GameActionIn(BaseModel):
    role: str
    token: str
    action: str
    player_id: Optional[int] = None
    option: Optional[str] = None


def require_admin(x_admin_pin: str = Header(default="")):
    if config.ADMIN_PIN and x_admin_pin != config.ADMIN_PIN:
        raise HTTPException(401, "Wrong admin PIN")


def require_host_pin(x_host_pin: str = Header(default="")):
    if not secrets.compare_digest(x_host_pin, config.HOST_PIN):
        raise HTTPException(401, "Wrong host PIN")


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
        live_games = await run_db(db.count_live_games)
    except Exception as exc:
        return {"ok": False, "database": str(exc)}
    return {"ok": ok, "live_games": live_games}


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
            "Database unavailable. Check DATABASE_URL or the DB_* settings in the backend environment.",
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
    for _ in range(20):
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        if await run_db(db.code_exists, code):
            continue
        try:
            result = await run_db(live_db.create_game, code, body.host_name, secrets.token_hex(16),
                                  category, min(body.num_questions, available), body.time_limit)
            return {"code": code, "host_token": result["host_token"],
                    "num_questions": result["num_questions"], "time_limit": body.time_limit}
        except Exception as exc:
            if "Duplicate entry" not in str(exc):
                raise
    raise HTTPException(503, "Could not generate a unique game code. Please try again.")


@app.get("/api/games/{code}")
async def game_status(code: str):
    result = await run_db(db.live_game_status, code)
    if not result:
        raise HTTPException(404, "No game found with that code. Check the code on the big screen.")
    result["max_players"] = config.MAX_PLAYERS
    result["joinable"] = result["phase"] == "lobby" and result["players"] < config.MAX_PLAYERS
    return result


@app.post("/api/games/{code}/join")
async def join(code: str, body: JoinIn):
    player, error = await run_db(live_db.join_game, code, body.name, secrets.token_hex(16))
    if error:
        raise HTTPException(400, error)
    return {"player_id": player["id"], "token": player["token"], "name": player["name"], "code": code}


@app.get("/api/games/{code}/stream")
async def game_stream(code: str, response: Response, role: str, token: str, since: int = -1,
                      player_id: Optional[int] = None):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return await run_db(live_db.stream_state, code, role, token, player_id, since)


@app.post("/api/games/{code}/action")
async def game_action(code: str, body: GameActionIn):
    if body.role == "host":
        error = await run_db(live_db.host_action, code, body.token, body.action, body.player_id)
    elif body.role == "player" and body.action == "answer" and body.player_id is not None:
        error = await run_db(live_db.submit_answer, code, body.player_id, body.token, body.option or "")
    else:
        raise HTTPException(400, "Invalid game action.")
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


@app.get("/api/history")
async def history():
    return await run_db(db.recent_games, 20)


# Serve the root landing page and frontend assets/pages. Keep static mounts last.
@app.get("/", include_in_schema=False)
async def homepage():
    if INDEX_FILE.is_file():
        return FileResponse(INDEX_FILE)
    return {"name": "Desk Duel API", "status": "running", "frontend_available": FRONTEND_DIR.is_dir()}


if FRONTEND_DIR.is_dir():
    @app.get("/frontend/", include_in_schema=False)
    @app.get("/frontend/index.html", include_in_schema=False)
    async def frontend_homepage():
        return RedirectResponse("/")


    @app.get("/host.html", include_in_schema=False)
    @app.get("/play.html", include_in_schema=False)
    @app.get("/questions.html", include_in_schema=False)
    async def legacy_frontend_pages(request: Request):
        return RedirectResponse(f"/frontend{request.url.path}")


    # Keep the static mount last so API and page routes take precedence.
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    log.info(
        "No frontend bundle at '%s'; running API-only mode (frontend is deployed separately).",
        FRONTEND_DIR,
    )
