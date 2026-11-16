"""Database access with mysql-connector-python.

All functions here are ordinary (blocking) functions. main.py / game.py call them
through `run_db(...)`, which runs them on a worker thread so a slow query never
freezes the live game for everyone else.
"""
import asyncio
import csv
import io

import mysql.connector
from mysql.connector import pooling

import config

_pool = None


def get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="desk_duel_pool",
            pool_size=config.DB_POOL_SIZE,
            **config.DB_CONNECT_OPTIONS,
            autocommit=True,
        )
    return _pool


async def run_db(func, *args):
    """Run a blocking DB function on a worker thread."""
    return await asyncio.to_thread(func, *args)


def _query(sql, params=(), fetch="all"):
    conn = get_pool().get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        if fetch == "all":
            result = cur.fetchall()
        elif fetch == "one":
            result = cur.fetchone()
        else:  # write
            result = cur.lastrowid
        cur.close()
        return result
    finally:
        conn.close()  # returns the connection to the pool


# ---------------------------------------------------------------- questions

def list_questions(category=None):
    if category:
        return _query(
            "SELECT * FROM questions WHERE is_active = 1 AND category = %s ORDER BY id DESC",
            (category,),
        )
    return _query("SELECT * FROM questions WHERE is_active = 1 ORDER BY category, id DESC")


def list_categories():
    return _query(
        "SELECT category, COUNT(*) AS total FROM questions "
        "WHERE is_active = 1 GROUP BY category ORDER BY category"
    )


def count_questions(category=None):
    if category:
        row = _query(
            "SELECT COUNT(*) AS n FROM questions WHERE is_active = 1 AND category = %s",
            (category,), fetch="one",
        )
    else:
        row = _query("SELECT COUNT(*) AS n FROM questions WHERE is_active = 1", fetch="one")
    return row["n"]


def add_question(q):
    return _query(
        "INSERT INTO questions (category, question_text, option_a, option_b, option_c, option_d, correct_option) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (q["category"], q["question_text"], q["option_a"], q["option_b"],
         q["option_c"], q["option_d"], q["correct_option"]),
        fetch="write",
    )


def update_question(question_id, q):
    _query(
        "UPDATE questions SET category=%s, question_text=%s, option_a=%s, option_b=%s, "
        "option_c=%s, option_d=%s, correct_option=%s WHERE id=%s",
        (q["category"], q["question_text"], q["option_a"], q["option_b"],
         q["option_c"], q["option_d"], q["correct_option"], question_id),
        fetch="write",
    )


def delete_question(question_id):
    # Soft delete: past games still point at this question, so we only hide it.
    _query("UPDATE questions SET is_active = 0 WHERE id = %s", (question_id,), fetch="write")


def import_questions_csv(text):
    """CSV columns: category,question,option_a,option_b,option_c,option_d,correct (A-D).
    A header row is optional. Returns (added, errors)."""
    added, errors = 0, []
    reader = csv.reader(io.StringIO(text.strip()))
    for line_no, row in enumerate(reader, start=1):
        if not row or all(not c.strip() for c in row):
            continue
        if line_no == 1 and row[0].strip().lower() == "category":
            continue  # header
        if len(row) < 7:
            errors.append(f"Line {line_no}: expected 7 columns, found {len(row)}")
            continue
        cat, text_, a, b, c, d, correct = [x.strip() for x in row[:7]]
        correct = correct.upper()
        if correct not in ("A", "B", "C", "D"):
            errors.append(f"Line {line_no}: correct answer must be A, B, C or D")
            continue
        if not all([cat, text_, a, b, c, d]):
            errors.append(f"Line {line_no}: a column is empty")
            continue
        add_question({
            "category": cat[:50], "question_text": text_[:500],
            "option_a": a[:200], "option_b": b[:200], "option_c": c[:200], "option_d": d[:200],
            "correct_option": correct,
        })
        added += 1
    return added, errors


# ---------------------------------------------------------------- games

def code_exists(code):
    return _query("SELECT id FROM games WHERE code = %s", (code,), fetch="one") is not None


def count_live_games():
    row = _query("SELECT COUNT(*) AS n FROM games WHERE game_phase <> 'finished'", fetch="one")
    return row["n"]


def live_game_status(code):
    return _query(
        "SELECT g.code, g.game_phase AS phase, g.host_name, g.num_questions, g.time_limit, "
        "COUNT(p.id) AS players, g.status "
        "FROM games g LEFT JOIN players p ON p.game_id = g.id "
        "WHERE g.code = %s GROUP BY g.id",
        (code,), fetch="one",
    )


def create_game(code, host_name, host_token, category, num_questions, time_limit):
    """Creates the game, picks random questions, returns (game_id, questions)."""
    conn = get_pool().get_connection()
    try:
        conn.start_transaction()
        cur = conn.cursor(dictionary=True)
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
            "INSERT INTO games (code, host_name, host_token, category, num_questions, time_limit) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (code, host_name, host_token, category, len(picked), time_limit),
        )
        game_id = cur.lastrowid

        questions = []
        for position, q in enumerate(picked, start=1):
            cur.execute(
                "INSERT INTO game_questions (game_id, question_id, position) VALUES (%s, %s, %s)",
                (game_id, q["id"], position),
            )
            questions.append({
                "game_question_id": cur.lastrowid,
                "question_id": q["id"],
                "category": q["category"],
                "text": q["question_text"],
                "options": {"A": q["option_a"], "B": q["option_b"],
                            "C": q["option_c"], "D": q["option_d"]},
                "correct": q["correct_option"],
            })
        conn.commit()
        cur.close()
        return game_id, questions
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_game_status(game_id, status):
    if status == "in_progress":
        sql = "UPDATE games SET status = %s, started_at = NOW() WHERE id = %s"
    elif status == "finished":
        sql = "UPDATE games SET status = %s, ended_at = NOW() WHERE id = %s"
    else:
        sql = "UPDATE games SET status = %s WHERE id = %s"
    _query(sql, (status, game_id), fetch="write")


def add_player(game_id, name, token):
    return _query(
        "INSERT INTO players (game_id, name, token) VALUES (%s, %s, %s)",
        (game_id, name, token), fetch="write",
    )


def remove_player(player_id):
    _query("DELETE FROM players WHERE id = %s", (player_id,), fetch="write")


def save_answer(game_id, game_question_id, player_id, option, is_correct, response_ms, points, new_score, correct_count):
    conn = get_pool().get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT IGNORE INTO answers (game_id, game_question_id, player_id, chosen_option, is_correct, response_ms, points) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (game_id, game_question_id, player_id, option, int(is_correct), response_ms, points),
        )
        cur.execute(
            "UPDATE players SET score = %s, correct_count = %s WHERE id = %s",
            (new_score, correct_count, player_id),
        )
        cur.close()
    finally:
        conn.close()


def save_final_ranks(ranks):
    """ranks: list of (player_id, rank, score)"""
    conn = get_pool().get_connection()
    try:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE players SET final_rank = %s, score = %s WHERE id = %s",
            [(rank, score, pid) for pid, rank, score in ranks],
        )
        cur.close()
    finally:
        conn.close()


def recent_games(limit=20):
    return _query(
        "SELECT g.id, g.code, g.host_name, g.category, g.num_questions, g.status, "
        "       g.created_at, g.ended_at, COUNT(p.id) AS players, "
        "       (SELECT p2.name FROM players p2 WHERE p2.game_id = g.id AND p2.final_rank = 1 LIMIT 1) AS winner "
        "FROM games g LEFT JOIN players p ON p.game_id = g.id "
        "GROUP BY g.id ORDER BY g.id DESC LIMIT %s",
        (limit,),
    )


def ping():
    return _query("SELECT 1 AS ok", fetch="one")["ok"] == 1
