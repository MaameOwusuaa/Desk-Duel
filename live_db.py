"""MySQL-backed live game state for stateless/serverless deployments."""
import math

import config
import db


def _dict_cursor(conn):
    return conn.cursor(dictionary=True)


def _game(cur, code, lock=False):
    cur.execute("SELECT * FROM games WHERE code = %s" + (" FOR UPDATE" if lock else ""), (code,))
    return cur.fetchone()


def _bump(cur, game_id, event, player_id=None, option=None, phase=None, position=None,
          status=None, started=False, ended=False):
    fields = ["last_event_type = %s", "last_event_player_id = %s", "last_event_option = %s"]
    values = [event, player_id, option]
    if phase is not None:
        fields.extend(["game_phase = %s", "phase_started_at = NOW(6)", "phase_version = state_version + 1"])
        values.append(phase)
    if position is not None:
        fields.append("current_position = %s")
        values.append(position)
    if status is not None:
        fields.append("status = %s")
        values.append(status)
    if started:
        fields.append("started_at = NOW()")
    if ended:
        fields.append("ended_at = NOW()")
    fields.append("state_version = state_version + 1")
    values.append(game_id)
    cur.execute(f"UPDATE games SET {', '.join(fields)} WHERE id = %s", values)


def create_game(code, host_name, host_token, category, num_questions, time_limit):
    conn = db.get_pool().get_connection()
    try:
        conn.start_transaction()
        cur = _dict_cursor(conn)
        if category:
            cur.execute(
                "SELECT * FROM questions WHERE is_active = 1 AND category = %s ORDER BY RAND() LIMIT %s",
                (category, num_questions),
            )
        else:
            cur.execute(
                "SELECT * FROM questions WHERE is_active = 1 ORDER BY RAND() LIMIT %s",
                (num_questions,),
            )
        picked = cur.fetchall()
        cur.execute(
            "INSERT INTO games (code, host_name, host_token, category, num_questions, time_limit, "
            "game_phase, current_position, phase_started_at, state_version, phase_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'lobby', 0, NOW(6), 1, 1)",
            (code, host_name, host_token, category, len(picked), time_limit),
        )
        game_id = cur.lastrowid
        for position, question in enumerate(picked, start=1):
            cur.execute(
                "INSERT INTO game_questions (game_id, question_id, position) VALUES (%s, %s, %s)",
                (game_id, question["id"], position),
            )
        conn.commit()
        return {"id": game_id, "code": code, "host_token": host_token,
                "num_questions": len(picked), "time_limit": time_limit}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def join_game(code, name, token):
    conn = db.get_pool().get_connection()
    try:
        conn.start_transaction()
        cur = _dict_cursor(conn)
        game = _game(cur, code, lock=True)
        if not game:
            conn.rollback()
            return None, "No game found with that code. Check the code on the big screen."
        if game["game_phase"] != "lobby":
            conn.rollback()
            return None, "This game has already started. Ask the host to start a new one."
        cur.execute("SELECT COUNT(*) AS n FROM players WHERE game_id = %s", (game["id"],))
        count = cur.fetchone()["n"]
        if count >= config.MAX_PLAYERS:
            conn.rollback()
            return None, f"This game is full ({config.MAX_PLAYERS} players max)."
        cur.execute("SELECT id FROM players WHERE game_id = %s AND LOWER(name) = LOWER(%s)",
                    (game["id"], name))
        if cur.fetchone():
            conn.rollback()
            return None, "Someone already has that name. Try another one."
        cur.execute(
            "INSERT INTO players (game_id, name, token, is_online, last_seen_at) "
            "VALUES (%s, %s, %s, 1, NOW(6))",
            (game["id"], name, token),
        )
        player_id = cur.lastrowid
        _bump(cur, game["id"], "join", player_id=player_id)
        conn.commit()
        return {"id": player_id, "token": token, "name": name, "code": code}, None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _refresh_game(cur, game_id):
    cur.execute("SELECT * FROM games WHERE id = %s", (game_id,))
    return cur.fetchone()


def _assign_ranks(cur, game_id):
    cur.execute(
        "SELECT id, score, correct_ms_total FROM players WHERE game_id = %s "
        "ORDER BY score DESC, correct_ms_total, name COLLATE utf8mb4_unicode_ci",
        (game_id,),
    )
    players = cur.fetchall()
    previous_key = None
    previous_rank = 0
    updates = []
    for place, player in enumerate(players, start=1):
        key = (player["score"], player["correct_ms_total"])
        rank = previous_rank if key == previous_key else place
        updates.append((rank, rank, player["id"]))
        previous_key, previous_rank = key, rank
    if updates:
        cur.executemany(
            "UPDATE players SET previous_rank = rank, rank = %s WHERE id = %s",
            [(rank, pid) for rank, _unused, pid in updates],
        )


def _reveal(cur, game):
    _assign_ranks(cur, game["id"])
    _bump(cur, game["id"], "reveal", phase="reveal")


def _advance_due(cur, game):
    """Advance expired phases under the game row lock; safe across instances."""
    if game["game_phase"] not in ("get_ready", "question"):
        return game
    cur.execute(
        "SELECT GREATEST(0, TIMESTAMPDIFF(MICROSECOND, phase_started_at, NOW(6)) DIV 1000) AS elapsed_ms "
        "FROM games WHERE id = %s",
        (game["id"],),
    )
    elapsed = cur.fetchone()["elapsed_ms"]
    if game["game_phase"] == "get_ready" and elapsed >= config.GET_READY_SECONDS * 1000:
        _bump(cur, game["id"], "question", phase="question", position=1)
        return _refresh_game(cur, game["id"])
    if game["game_phase"] == "question":
        cur.execute(
            "SELECT gq.id FROM game_questions gq WHERE gq.game_id = %s AND gq.position = %s",
            (game["id"], game["current_position"]),
        )
        game_question = cur.fetchone()
        if elapsed >= game["time_limit"] * 1000:
            _reveal(cur, game)
            return _refresh_game(cur, game["id"])
        cur.execute(
            "SELECT COUNT(*) AS n FROM players WHERE game_id = %s AND is_online = 1",
            (game["id"],),
        )
        online_count = cur.fetchone()["n"]
        if online_count and game_question:
            cur.execute(
                "SELECT COUNT(*) AS n FROM answers WHERE game_question_id = %s "
                "AND player_id IN (SELECT id FROM players WHERE game_id = %s AND is_online = 1)",
                (game_question["id"], game["id"]),
            )
            if cur.fetchone()["n"] >= online_count:
                _reveal(cur, game)
                return _refresh_game(cur, game["id"])
    return game


def _leaderboard(cur, game, game_question_id=None):
    cur.execute(
        "SELECT p.id, p.name, p.score, p.correct_count, p.correct_ms_total, p.rank, p.previous_rank, "
        "p.is_online AS connected, a.points AS last_points, a.is_correct AS last_correct "
        "FROM players p LEFT JOIN answers a ON a.player_id = p.id AND a.game_question_id = %s "
        "WHERE p.game_id = %s ORDER BY p.score DESC, p.correct_ms_total, p.name COLLATE utf8mb4_unicode_ci",
        (game_question_id, game["id"]),
    )
    rows = cur.fetchall()
    result = []
    position = 0
    previous_key = None
    rank = 0
    for row in rows:
        position += 1
        key = (row["score"], row["correct_ms_total"])
        if key != previous_key:
            rank = position
        previous_key = key
        result.append({
            "id": row["id"], "name": row["name"], "score": row["score"], "rank": rank,
            "rank_change": (row["previous_rank"] or rank) - rank,
            "correct_count": row["correct_count"], "last_points": row["last_points"] or 0,
            "last_correct": bool(row["last_correct"]), "connected": bool(row["connected"]),
        })
    return result


def _player_info(cur, game, player_id, game_question_id=None, board=None):
    cur.execute("SELECT id, name, score, correct_count, rank, previous_rank FROM players "
                "WHERE id = %s AND game_id = %s", (player_id, game["id"]))
    player = cur.fetchone()
    if not player:
        return None
    answer = None
    if game_question_id:
        cur.execute("SELECT chosen_option, is_correct, response_ms, points, id FROM answers "
                    "WHERE game_question_id = %s AND player_id = %s",
                    (game_question_id, player_id))
        answer = cur.fetchone()
    fastest = False
    if answer and answer["is_correct"]:
        cur.execute("SELECT id FROM answers WHERE game_question_id = %s AND is_correct = 1 ORDER BY id LIMIT 1",
                    (game_question_id,))
        first = cur.fetchone()
        fastest = bool(first and first["id"] == answer["id"])
    rank = player["rank"]
    if board:
        rank = next((r["rank"] for r in board if r["id"] == player_id), rank)
    return {
        "id": player["id"], "name": player["name"], "answered": answer is not None,
        "option": answer["chosen_option"] if answer else None,
        "correct": bool(answer and answer["is_correct"]), "points": answer["points"] if answer else 0,
        "fastest_bonus": fastest, "response_ms": answer["response_ms"] if answer else None,
        "score": player["score"], "rank": rank,
        "rank_change": (player["previous_rank"] or rank or 0) - (rank or 0),
        "player_count": _player_count(cur, game["id"]),
    }


def _player_count(cur, game_id):
    cur.execute("SELECT COUNT(*) AS n FROM players WHERE game_id = %s", (game_id,))
    return cur.fetchone()["n"]


def _snapshot(cur, game, role, player_id, since):
    phase = game["game_phase"]
    version = game["state_version"]
    phase_changed = since < game["phase_version"]
    payload = {"version": version}
    if phase == "lobby":
        cur.execute("SELECT id, name, is_online FROM players WHERE game_id = %s ORDER BY id", (game["id"],))
        players = [{"id": p["id"], "name": p["name"], "connected": bool(p["is_online"])}
                   for p in cur.fetchall()]
        payload.update({"type": "lobby", "code": game["code"], "host_name": game["host_name"],
                        "players": players, "count": len(players), "min_players": config.MIN_PLAYERS,
                        "max_players": config.MAX_PLAYERS, "num_questions": game["num_questions"],
                        "time_limit": game["time_limit"]})
    elif phase == "get_ready":
        cur.execute("SELECT TIMESTAMPDIFF(MICROSECOND, phase_started_at, NOW(6)) DIV 1000 AS elapsed_ms "
                    "FROM games WHERE id = %s", (game["id"],))
        elapsed = cur.fetchone()["elapsed_ms"]
        payload.update({"type": "get_ready", "seconds": max(0, math.ceil(config.GET_READY_SECONDS - elapsed / 1000)),
                        "total": game["num_questions"]})
    else:
        game_question_id = None
        question = None
        if game["current_position"]:
            cur.execute(
                "SELECT gq.id AS game_question_id, q.category, q.question_text, q.option_a, q.option_b, "
                "q.option_c, q.option_d, q.correct_option FROM game_questions gq "
                "JOIN questions q ON q.id = gq.question_id WHERE gq.game_id = %s AND gq.position = %s",
                (game["id"], game["current_position"]),
            )
            question = cur.fetchone()
            game_question_id = question["game_question_id"] if question else None
        board = _leaderboard(cur, game, game_question_id)
        you = _player_info(cur, game, player_id, game_question_id, board) if role == "player" else None
        if phase == "question":
            cur.execute("SELECT TIMESTAMPDIFF(MICROSECOND, phase_started_at, NOW(6)) DIV 1000 AS elapsed_ms "
                        "FROM games WHERE id = %s", (game["id"],))
            elapsed = cur.fetchone()["elapsed_ms"]
            cur.execute("SELECT COUNT(*) AS n FROM answers WHERE game_question_id = %s", (game_question_id,))
            answered = cur.fetchone()["n"]
            event_type = "question"
            if not phase_changed and since >= 0 and game["last_event_type"] == "answer":
                event_type = ("answer_locked" if role == "player" and
                              game["last_event_player_id"] == player_id else "answer_count")
            payload.update({"type": event_type, "index": game["current_position"],
                            "total": game["num_questions"], "category": question["category"],
                            "text": question["question_text"],
                            "options": {"A": question["option_a"], "B": question["option_b"],
                                        "C": question["option_c"], "D": question["option_d"]},
                            "time_limit": game["time_limit"],
                            "remaining_ms": max(0, game["time_limit"] * 1000 - elapsed),
                            "answered": answered, "player_count": _player_count(cur, game["id"])})
            if event_type == "answer_locked":
                payload["option"] = you["option"] if you else game["last_event_option"]
            if event_type == "answer_count":
                payload["you"] = you
        elif phase == "reveal":
            cur.execute("SELECT chosen_option, COUNT(*) AS n FROM answers WHERE game_question_id = %s "
                        "GROUP BY chosen_option", (game_question_id,))
            distribution = {key: 0 for key in "ABCD"}
            for row in cur.fetchall():
                distribution[row["chosen_option"]] = row["n"]
            cur.execute("SELECT p.name, a.response_ms FROM answers a JOIN players p ON p.id = a.player_id "
                        "WHERE a.game_question_id = %s AND a.is_correct = 1 ORDER BY a.id LIMIT 1",
                        (game_question_id,))
            fastest_row = cur.fetchone()
            correct_count = sum(1 for row in board if row["last_correct"])
            payload.update({"type": "reveal", "index": game["current_position"],
                            "total": game["num_questions"], "text": question["question_text"],
                            "options": {"A": question["option_a"], "B": question["option_b"],
                                        "C": question["option_c"], "D": question["option_d"]},
                            "correct": question["correct_option"],
                            "correct_text": question["option_" + question["correct_option"].lower()],
                            "distribution": distribution, "answered": sum(distribution.values()),
                            "player_count": _player_count(cur, game["id"]),
                            "fastest": ({"name": fastest_row["name"], "ms": fastest_row["response_ms"]}
                                        if fastest_row else None),
                            "leaderboard": board, "is_last": game["current_position"] >= game["num_questions"],
                            "right_count": correct_count})
            if you is not None:
                payload["you"] = you
        else:
            payload.update({"type": "finished", "leaderboard": board, "podium": board[:3],
                            "total_questions": game["num_questions"]})
            if you is not None:
                payload["you"] = you
    return payload


def stream_state(code, role, token, player_id=None, since=-1):
    conn = db.get_pool().get_connection()
    try:
        conn.start_transaction()
        cur = _dict_cursor(conn)
        game = _game(cur, code, lock=True)
        if not game:
            conn.rollback()
            return {"type": "fatal", "message": "This game no longer exists."}
        if role == "host":
            if not token or token != game["host_token"]:
                conn.rollback()
                return {"type": "fatal", "message": "This game no longer exists, or you are not its host."}
        elif role == "player":
            cur.execute("SELECT id, is_online FROM players WHERE id = %s AND game_id = %s AND token = %s",
                        (player_id, game["id"], token))
            player = cur.fetchone()
            if not player:
                conn.rollback()
                return {"type": "fatal", "message": "We couldn't find you in this game. Please join again."}
            cur.execute("UPDATE players SET last_seen_at = NOW(6), is_online = 1 WHERE id = %s", (player_id,))
            if not player["is_online"]:
                _bump(cur, game["id"], "presence", player_id=player_id)
                game = _refresh_game(cur, game["id"])
        else:
            conn.rollback()
            return {"type": "fatal", "message": "Invalid game connection role."}

        cur.execute("UPDATE players SET is_online = 0 WHERE game_id = %s AND is_online = 1 "
                    "AND last_seen_at < DATE_SUB(NOW(6), INTERVAL 8 SECOND)", (game["id"],))
        if cur.rowcount:
            _bump(cur, game["id"], "presence")
            game = _refresh_game(cur, game["id"])
        game = _advance_due(cur, game)
        if since >= game["state_version"]:
            conn.commit()
            return {"type": "noop", "version": game["state_version"]}
        payload = _snapshot(cur, game, role, player_id, since)
        conn.commit()
        return payload
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish(cur, game):
    _assign_ranks(cur, game["id"])
    cur.execute("UPDATE players SET final_rank = rank WHERE game_id = %s", (game["id"],))
    _bump(cur, game["id"], "finished", phase="finished", status="finished", ended=True)


def host_action(code, token, action, player_id=None):
    conn = db.get_pool().get_connection()
    try:
        conn.start_transaction()
        cur = _dict_cursor(conn)
        game = _game(cur, code, lock=True)
        if not game or not token or token != game["host_token"]:
            conn.rollback()
            return "This game no longer exists, or you are not its host."
        game = _advance_due(cur, game)
        if action == "start":
            if game["game_phase"] != "lobby":
                conn.rollback()
                return "The game has already started."
            cur.execute("SELECT COUNT(*) AS n FROM players WHERE game_id = %s", (game["id"],))
            if cur.fetchone()["n"] < config.MIN_PLAYERS:
                conn.rollback()
                return f"You need at least {config.MIN_PLAYERS} players to start."
            _bump(cur, game["id"], "get_ready", phase="get_ready", status="in_progress", started=True)
        elif action == "next":
            if game["game_phase"] != "reveal":
                conn.rollback()
                return None
            if game["current_position"] >= game["num_questions"]:
                _finish(cur, game)
            else:
                _bump(cur, game["id"], "question", phase="question", position=game["current_position"] + 1)
        elif action == "reveal_now":
            if game["game_phase"] == "question":
                _reveal(cur, game)
        elif action == "end":
            if game["game_phase"] != "finished":
                _finish(cur, game)
        elif action == "kick":
            if game["game_phase"] != "lobby":
                conn.rollback()
                return "Players can only be removed before the game starts."
            cur.execute("DELETE FROM players WHERE id = %s AND game_id = %s", (player_id, game["id"]))
            if cur.rowcount:
                _bump(cur, game["id"], "kick", player_id=player_id)
        else:
            conn.rollback()
            return "Unknown host action."
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def submit_answer(code, player_id, token, option):
    option = (option or "").upper()
    if option not in "ABCD" or len(option) != 1:
        return "Choose one of the available answers."
    conn = db.get_pool().get_connection()
    try:
        conn.start_transaction()
        cur = _dict_cursor(conn)
        game = _game(cur, code, lock=True)
        if not game:
            conn.rollback()
            return "This game no longer exists."
        cur.execute("SELECT id FROM players WHERE id = %s AND game_id = %s AND token = %s",
                    (player_id, game["id"], token))
        if not cur.fetchone():
            conn.rollback()
            return "We couldn't find you in this game. Please join again."
        game = _advance_due(cur, game)
        if game["game_phase"] != "question":
            conn.rollback()
            return "This question is no longer accepting answers."
        cur.execute(
            "SELECT gq.id AS game_question_id, q.correct_option FROM game_questions gq "
            "JOIN questions q ON q.id = gq.question_id WHERE gq.game_id = %s AND gq.position = %s",
            (game["id"], game["current_position"]),
        )
        question = cur.fetchone()
        cur.execute("SELECT TIMESTAMPDIFF(MICROSECOND, phase_started_at, NOW(6)) DIV 1000 AS elapsed_ms "
                    "FROM games WHERE id = %s", (game["id"],))
        elapsed_ms = cur.fetchone()["elapsed_ms"]
        if elapsed_ms > game["time_limit"] * 1000 + 300:
            conn.rollback()
            return "Time is up."
        cur.execute("SELECT id FROM answers WHERE game_question_id = %s AND player_id = %s",
                    (question["game_question_id"], player_id))
        if cur.fetchone():
            conn.rollback()
            return None
        correct = option == question["correct_option"]
        points = 0
        correct_ms = 0
        if correct:
            fraction = min(elapsed_ms / (game["time_limit"] * 1000), 1.0)
            points = round(config.MAX_POINTS - (config.MAX_POINTS - config.MIN_POINTS) * fraction)
            cur.execute("SELECT COUNT(*) AS n FROM answers WHERE game_question_id = %s AND is_correct = 1",
                        (question["game_question_id"],))
            if cur.fetchone()["n"] == 0:
                points += config.FASTEST_BONUS
            correct_ms = elapsed_ms
        cur.execute("INSERT INTO answers (game_id, game_question_id, player_id, chosen_option, is_correct, "
                    "response_ms, points) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (game["id"], question["game_question_id"], player_id, option, int(correct), elapsed_ms, points))
        cur.execute("UPDATE players SET score = score + %s, correct_count = correct_count + %s, "
                    "correct_ms_total = correct_ms_total + %s, last_seen_at = NOW(6), is_online = 1 "
                    "WHERE id = %s", (points, int(correct), correct_ms, player_id))
        _bump(cur, game["id"], "answer", player_id=player_id, option=option)
        game = _refresh_game(cur, game["id"])
        game = _advance_due(cur, game)
        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
