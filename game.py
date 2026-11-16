"""The live game engine.

While a game is being played, its state lives in memory here (fast and
consistent for everyone). Every join, answer and final result is also written
to MySQL so you keep a full history.

Run uvicorn with ONE worker: live games are held in this process's memory.
"""
import asyncio
import logging
import random
import secrets
import time

from fastapi import WebSocket

import config
import db
from db import run_db

log = logging.getLogger("quiz")

# code -> Game
GAMES: dict[str, "Game"] = {}
_BACKGROUND: set[asyncio.Task] = set()


def background(coro):
    """Start a task and keep a reference so it is not garbage-collected mid-run."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


class Player:
    def __init__(self, pid: int, name: str, token: str):
        self.id = pid
        self.name = name
        self.token = token
        self.score = 0
        self.correct_count = 0
        self.correct_ms_total = 0      # tie-break: faster overall wins
        self.rank = None
        self.prev_rank = None
        self.ws: WebSocket | None = None

    @property
    def connected(self):
        return self.ws is not None


class Game:
    def __init__(self, game_id, code, host_name, host_token, questions, time_limit):
        self.id = game_id
        self.code = code
        self.host_name = host_name
        self.host_token = host_token
        self.questions = questions
        self.time_limit = time_limit
        self.players: dict[int, Player] = {}
        self.hosts: set[WebSocket] = set()
        self.phase = "lobby"           # lobby | get_ready | question | reveal | finished
        self.current = -1              # index into self.questions
        self.question_started = 0.0    # time.monotonic() when the current question opened
        self.answers: dict[int, dict] = {}   # player_id -> answer for the current question
        self.fastest_id = None
        self.ready_until = 0.0
        self.last_reveal = None
        self.timer_task: asyncio.Task | None = None
        self.flow_task: asyncio.Task | None = None
        self.join_lock = asyncio.Lock()
        self.created_at = time.time()
        self.finished_at = None

    # ------------------------------------------------------------ sending

    @staticmethod
    async def _send(ws: WebSocket, message: dict):
        try:
            await ws.send_json(message)
        except Exception:
            pass  # socket already closed; the disconnect handler cleans up

    async def to_hosts(self, message):
        await asyncio.gather(*(self._send(ws, message) for ws in list(self.hosts)))

    async def to_players(self, message):
        await asyncio.gather(*(self._send(p.ws, message) for p in self.players.values() if p.ws))

    async def to_all(self, message):
        await asyncio.gather(self.to_hosts(message), self.to_players(message))

    # ------------------------------------------------------------ views

    def player_list(self):
        return [{"id": p.id, "name": p.name, "connected": p.connected}
                for p in sorted(self.players.values(), key=lambda p: p.id)]

    def lobby_message(self):
        return {
            "type": "lobby",
            "code": self.code,
            "host_name": self.host_name,
            "players": self.player_list(),
            "count": len(self.players),
            "min_players": config.MIN_PLAYERS,
            "max_players": config.MAX_PLAYERS,
            "num_questions": len(self.questions),
            "time_limit": self.time_limit,
        }

    def ranked(self):
        """Players best-first: highest score, then fastest total time on correct answers."""
        return sorted(self.players.values(),
                      key=lambda p: (-p.score, p.correct_ms_total, p.name.lower()))

    def _assign_ranks(self):
        """Rank players; players with the same score and same total time share a rank."""
        prev_key, prev_rank = None, 0
        for position, p in enumerate(self.ranked(), start=1):
            p.prev_rank = p.rank
            key = (p.score, p.correct_ms_total)
            p.rank = prev_rank if key == prev_key else position
            prev_key, prev_rank = key, p.rank

    def leaderboard(self):
        rows = []
        for p in self.ranked():
            ans = self.answers.get(p.id)
            change = 0 if p.prev_rank is None or p.rank is None else p.prev_rank - p.rank
            rows.append({
                "id": p.id, "name": p.name, "score": p.score, "rank": p.rank,
                "rank_change": change, "correct_count": p.correct_count,
                "last_points": ans["points"] if ans else 0,
                "last_correct": bool(ans and ans["correct"]),
                "connected": p.connected,
            })
        return rows

    def question_message(self):
        q = self.questions[self.current]
        elapsed_ms = int((time.monotonic() - self.question_started) * 1000)
        return {
            "type": "question",
            "index": self.current + 1,
            "total": len(self.questions),
            "category": q["category"],
            "text": q["text"],
            "options": q["options"],
            "time_limit": self.time_limit,
            "remaining_ms": max(0, self.time_limit * 1000 - elapsed_ms),
            "answered": len(self.answers),
            "player_count": len(self.players),
        }

    def personal(self, p: Player):
        ans = self.answers.get(p.id)
        change = 0 if p.prev_rank is None or p.rank is None else p.prev_rank - p.rank
        return {
            "id": p.id, "name": p.name,
            "answered": ans is not None,
            "option": ans["option"] if ans else None,
            "correct": bool(ans and ans["correct"]),
            "points": ans["points"] if ans else 0,
            "fastest_bonus": self.fastest_id == p.id,
            "response_ms": ans["ms"] if ans else None,
            "score": p.score, "rank": p.rank, "rank_change": change,
            "player_count": len(self.players),
        }

    def final_message(self):
        board = self.leaderboard()
        return {"type": "finished", "leaderboard": board, "podium": board[:3],
                "total_questions": len(self.questions)}

    # ------------------------------------------------------------ connections

    async def connect_host(self, ws: WebSocket):
        self.hosts.add(ws)
        await self._send(ws, await self.snapshot_for_host())

    async def disconnect_host(self, ws: WebSocket):
        self.hosts.discard(ws)

    async def connect_player(self, p: Player, ws: WebSocket):
        old = p.ws
        p.ws = ws
        if old is not None and old is not ws:
            await self._send(old, {"type": "replaced", "message": "You opened the game in another tab."})
            try:
                await old.close()
            except Exception:
                pass
        await self._send(ws, self.snapshot_for_player(p))
        if self.phase == "lobby":
            await self.to_all(self.lobby_message())
        else:
            await self.to_hosts({"type": "players", "players": self.player_list()})

    async def disconnect_player(self, p: Player, ws: WebSocket):
        if p.ws is not ws:
            return  # an old socket closing after a reconnect
        p.ws = None
        if self.phase == "lobby":
            await self.to_all(self.lobby_message())
        else:
            await self.to_hosts({"type": "players", "players": self.player_list()})
            await self._maybe_end_early()

    async def snapshot_for_host(self):
        if self.phase == "lobby":
            return self.lobby_message()
        if self.phase == "get_ready":
            return {"type": "get_ready", "seconds": max(0, round(self.ready_until - time.monotonic())),
                    "total": len(self.questions)}
        if self.phase == "question":
            return self.question_message()
        if self.phase == "reveal":
            return self.last_reveal
        return self.final_message()

    def snapshot_for_player(self, p: Player):
        base = {"you": self.personal(p)}
        if self.phase == "lobby":
            return {**self.lobby_message(), **base}
        if self.phase == "get_ready":
            return {"type": "get_ready", "seconds": max(0, round(self.ready_until - time.monotonic())),
                    "total": len(self.questions), **base}
        if self.phase == "question":
            return {**self.question_message(), **base}
        if self.phase == "reveal":
            return {**self.last_reveal, **base}
        return {**self.final_message(), **base}

    # ------------------------------------------------------------ host actions

    async def start(self):
        if self.phase != "lobby":
            return "The game has already started."
        if len(self.players) < config.MIN_PLAYERS:
            return f"You need at least {config.MIN_PLAYERS} players to start."
        self.phase = "get_ready"
        self.ready_until = time.monotonic() + config.GET_READY_SECONDS
        await run_db(db.set_game_status, self.id, "in_progress")
        await self.to_all({"type": "get_ready", "seconds": config.GET_READY_SECONDS,
                           "total": len(self.questions)})
        self.flow_task = background(self._open_first_question())
        return None

    async def _open_first_question(self):
        await asyncio.sleep(config.GET_READY_SECONDS)
        await self.next_question()

    async def next_question(self):
        if self.phase not in ("get_ready", "reveal"):
            return
        if self.current + 1 >= len(self.questions):
            await self.finish()
            return
        self.current += 1
        self.answers = {}
        self.fastest_id = None
        self.phase = "question"
        self.question_started = time.monotonic()
        msg = self.question_message()
        await self.to_all(msg)
        self.timer_task = background(self._question_timer(self.current))

    async def _question_timer(self, index):
        await asyncio.sleep(self.time_limit)
        if self.phase == "question" and self.current == index:
            await self.end_question()

    async def kick(self, player_id):
        if self.phase != "lobby":
            return "Players can only be removed before the game starts."
        p = self.players.pop(player_id, None)
        if not p:
            return None
        if p.ws:
            await self._send(p.ws, {"type": "kicked", "message": "The host removed you from the game."})
            try:
                await p.ws.close()
            except Exception:
                pass
        await run_db(db.remove_player, p.id)
        await self.to_all(self.lobby_message())
        return None

    # ------------------------------------------------------------ answering

    async def submit_answer(self, p: Player, option: str):
        option = (option or "").upper()
        if self.phase != "question" or option not in ("A", "B", "C", "D"):
            return
        if p.id in self.answers:
            return  # one answer per question, no changing your mind
        ms = int((time.monotonic() - self.question_started) * 1000)
        limit_ms = self.time_limit * 1000
        if ms > limit_ms + 300:
            return  # arrived after time was up

        q = self.questions[self.current]
        correct = option == q["correct"]
        points = 0
        if correct:
            # Faster = more points: 1000 for an instant answer, down to 500 at the buzzer.
            fraction = min(ms / limit_ms, 1.0)
            points = round(config.MAX_POINTS - (config.MAX_POINTS - config.MIN_POINTS) * fraction)
            if self.fastest_id is None:
                self.fastest_id = p.id
                points += config.FASTEST_BONUS
            p.correct_count += 1
            p.correct_ms_total += ms
        p.score += points
        self.answers[p.id] = {"option": option, "ms": ms, "correct": correct, "points": points}

        await self._send(p.ws, {"type": "answer_locked", "option": option})
        await self.to_all({"type": "answer_count", "answered": len(self.answers),
                           "player_count": len(self.players)})
        background(run_db(
            db.save_answer, self.id, q["game_question_id"], p.id, option,
            correct, ms, points, p.score, p.correct_count,
        ))
        await self._maybe_end_early()

    async def _maybe_end_early(self):
        """Close the question as soon as every connected player has answered."""
        if self.phase != "question":
            return
        online = [p for p in self.players.values() if p.connected]
        if online and all(p.id in self.answers for p in online):
            await asyncio.sleep(0.6)  # tiny pause so the last answer feels registered
            if self.phase == "question":
                await self.end_question()

    async def end_question(self):
        if self.phase != "question":
            return
        self.phase = "reveal"
        if self.timer_task and self.timer_task is not asyncio.current_task():
            self.timer_task.cancel()

        q = self.questions[self.current]
        self._assign_ranks()

        distribution = {k: 0 for k in "ABCD"}
        for a in self.answers.values():
            distribution[a["option"]] += 1

        fastest = None
        if self.fastest_id in self.players:
            fp = self.players[self.fastest_id]
            fastest = {"name": fp.name, "ms": self.answers[fp.id]["ms"]}

        self.last_reveal = {
            "type": "reveal",
            "index": self.current + 1,
            "total": len(self.questions),
            "text": q["text"],
            "options": q["options"],
            "correct": q["correct"],
            "correct_text": q["options"][q["correct"]],
            "distribution": distribution,
            "answered": len(self.answers),
            "player_count": len(self.players),
            "fastest": fastest,
            "leaderboard": self.leaderboard(),
            "is_last": self.current + 1 >= len(self.questions),
        }
        await self.to_hosts(self.last_reveal)
        await asyncio.gather(*(
            self._send(p.ws, {**self.last_reveal, "you": self.personal(p)})
            for p in self.players.values() if p.ws
        ))

    async def finish(self):
        if self.phase == "finished":
            return
        if self.timer_task:
            self.timer_task.cancel()
        self.phase = "finished"
        self.finished_at = time.time()
        self.answers = {}
        self._assign_ranks()
        await run_db(db.save_final_ranks, [(p.id, p.rank, p.score) for p in self.players.values()])
        await run_db(db.set_game_status, self.id, "finished")
        final = self.final_message()
        await self.to_hosts(final)
        await asyncio.gather(*(
            self._send(p.ws, {**final, "you": self.personal(p)})
            for p in self.players.values() if p.ws
        ))


# ---------------------------------------------------------------- registry

def _new_code():
    return "".join(random.choices("0123456789", k=6))


def cleanup_old_games():
    now = time.time()
    for code, g in list(GAMES.items()):
        finished_long_ago = g.finished_at and now - g.finished_at > 2 * 3600
        abandoned = now - g.created_at > 12 * 3600
        if finished_long_ago or abandoned:
            GAMES.pop(code, None)


async def create_game(host_name, category, num_questions, time_limit):
    cleanup_old_games()
    for _ in range(20):
        code = _new_code()
        if code not in GAMES and not await run_db(db.code_exists, code):
            break
    else:
        raise RuntimeError("Could not generate a unique game code")
    host_token = secrets.token_hex(16)
    game_id, questions = await run_db(
        db.create_game, code, host_name, host_token, category, num_questions, time_limit)
    game = Game(game_id, code, host_name, host_token, questions, time_limit)
    GAMES[code] = game
    log.info("Game %s created by %s with %d questions", code, host_name, len(questions))
    return game


async def join_game(game: Game, name: str):
    """Returns (player, error_message)."""
    async with game.join_lock:
        if game.phase != "lobby":
            return None, "This game has already started. Ask the host to start a new one."
        if len(game.players) >= config.MAX_PLAYERS:
            return None, f"This game is full ({config.MAX_PLAYERS} players max)."
        if any(p.name.lower() == name.lower() for p in game.players.values()):
            return None, "Someone already has that name. Try another one."
        token = secrets.token_hex(16)
        pid = await run_db(db.add_player, game.id, name, token)
        player = Player(pid, name, token)
        game.players[pid] = player
    await game.to_all(game.lobby_message())
    return player, None
