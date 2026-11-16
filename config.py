"""Settings, read from environment variables or a .env file in this folder."""
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# MySQL connection. DATABASE_URL takes precedence when supplied; individual
# DB_* variables remain convenient for local development.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_CONNECT_OPTIONS = {"charset": "utf8mb4"}

if DATABASE_URL:
    parsed_url = urlparse(DATABASE_URL)
    if parsed_url.scheme not in ("mysql", "mysql+mysqlconnector"):
        raise ValueError("DATABASE_URL must use the mysql:// scheme")
    if not parsed_url.hostname or not parsed_url.path.strip("/"):
        raise ValueError("DATABASE_URL must include a host and database name")

    DB_HOST = parsed_url.hostname
    DB_PORT = parsed_url.port or 3306
    DB_USER = unquote(parsed_url.username or "")
    DB_PASSWORD = unquote(parsed_url.password or "")
    DB_NAME = unquote(parsed_url.path.lstrip("/"))

    query_options = parse_qs(parsed_url.query)
    for option in ("ssl_ca", "ssl_cert", "ssl_key"):
        value = query_options.get(option, [None])[0] or os.getenv(f"DB_{option.upper()}")
        if value:
            DB_CONNECT_OPTIONS[option] = value
    for option in ("ssl_verify_cert", "ssl_verify_identity"):
        value = query_options.get(option, [os.getenv(f"DB_{option.upper()}", "")])[0]
        if value:
            DB_CONNECT_OPTIONS[option] = value.lower() in ("1", "true", "yes", "required")
    DB_CONNECT_OPTIONS.update({
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
    })
else:
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "desk_duel")
    DB_CONNECT_OPTIONS.update({
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
    })
    ssl_ca = os.getenv("DB_SSL_CA", "")
    if ssl_ca:
        DB_CONNECT_OPTIONS["ssl_ca"] = ssl_ca
    for option in ("ssl_verify_cert", "ssl_verify_identity"):
        value = os.getenv(f"DB_{option.upper()}", "").lower()
        if value:
            DB_CONNECT_OPTIONS[option] = value in ("1", "true", "yes", "required")

DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "3"))

# Game rules
MIN_PLAYERS = 2
MAX_PLAYERS = 20
MAX_POINTS = 1000          # a correct answer given instantly
MIN_POINTS = 500           # a correct answer given at the last second
FASTEST_BONUS = 100        # extra points for the first correct answer on each question
GET_READY_SECONDS = 3      # countdown shown before the first question
DEFAULT_TIME_LIMIT = 20    # seconds per question
DEFAULT_NUM_QUESTIONS = 10

# Optional PIN to protect the Question Bank page. Leave blank to allow anyone.
ADMIN_PIN = os.getenv("ADMIN_PIN", "")

# PIN required to open host setup and create games.
HOST_PIN = os.getenv("HOST_PIN", "2002").strip()

# Optional: the address players should open, e.g. http://192.168.1.20:8000
# Leave blank and the server works out its own office-network address.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# Comma-separated browser origins allowed to call this API (needed when the
# static frontend and API are deployed as separate Vercel projects).
FRONTEND_ORIGINS = [origin.strip().rstrip("/") for origin in
                    os.getenv("FRONTEND_ORIGINS", "").split(",") if origin.strip()]
