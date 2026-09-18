import json
import os
import random
import sqlite3
import threading
import time
import uuid
import chess
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

app = Flask(__name__)
CORS(app)
sock = Sock(app)

GAMES = {}
ROOMS = {}
MATCHMAKING_QUEUE = {}
USERS = {}
SESSIONS = {}
NEXT_USER_ID = 1000001
STATE_LOCK = threading.RLock()
DB_PATH = os.environ.get("CHESS_DB_PATH", os.path.join(os.path.dirname(__file__), "chess.db"))


def _db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _db_init():
    with _db_connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL DEFAULT '', avatar TEXT NOT NULL DEFAULT '', password TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS rooms (
                room_code TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY, payload TEXT NOT NULL, fen TEXT NOT NULL,
                status TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matchmaking_queue (
                player_id TEXT PRIMARY KEY, payload TEXT NOT NULL, queued_at REAL NOT NULL
            );
        """)


def _load_state():
    global NEXT_USER_ID
    _db_init()
    with _db_connect() as db:
        for row in db.execute("SELECT * FROM users"):
            USERS[row["username"].lower()] = dict(row)
        for row in db.execute("SELECT room_code, payload FROM rooms"):
            ROOMS[row["room_code"]] = json.loads(row["payload"])
        for row in db.execute("SELECT game_id, payload, fen FROM games"):
            game = json.loads(row["payload"])
            game["board"] = chess.Board(row["fen"])
            GAMES[row["game_id"]] = game
        for row in db.execute("SELECT player_id, payload FROM matchmaking_queue"):
            MATCHMAKING_QUEUE[row["player_id"]] = json.loads(row["payload"])
    if USERS:
        NEXT_USER_ID = max(int(user["id"]) for user in USERS.values()) + 1


def _json_game(game):
    return {key: value for key, value in game.items() if key != "board"}


def _persist_user(user):
    with _db_connect() as db:
        db.execute("INSERT OR REPLACE INTO users(id, username, email, avatar, password) VALUES (?, ?, ?, ?, ?)",
                   (user["id"], user["username"], user.get("email", ""), user.get("avatar", ""), user.get("password", "")))


def _persist_game(game_id):
    game = GAMES.get(game_id)
    if not game:
        return
    with _db_connect() as db:
        db.execute("INSERT OR REPLACE INTO games(game_id, payload, fen, status, updated_at) VALUES (?, ?, ?, ?, ?)",
                   (game_id, json.dumps(_json_game(game)), game["board"].fen(), game.get("status", "active"), time.time()))


def _persist_room(room):
    with _db_connect() as db:
        db.execute("INSERT OR REPLACE INTO rooms(room_code, payload, created_at) VALUES (?, ?, ?)",
                   (room["room_code"], json.dumps(room), room.get("created_at", time.time())))


def _persist_queue(player_id, player=None):
    with _db_connect() as db:
        if player is None:
            db.execute("DELETE FROM matchmaking_queue WHERE player_id = ?", (player_id,))
        else:
            db.execute("INSERT OR REPLACE INTO matchmaking_queue(player_id, payload, queued_at) VALUES (?, ?, ?)",
                       (player_id, json.dumps(player), player.get("queued_at", time.time())))


_load_state()

FAKE_PLAYERS = [
    {"username": "GrandmasterFlex", "rating": 1540, "country": "US"},
    {"username": "KnightRider99", "rating": 1210, "country": "IN"},
    {"username": "RookAndRoll", "rating": 1380, "country": "DE"},
    {"username": "PawnStar_2026", "rating": 1100, "country": "BR"},
    {"username": "CheckmatePro", "rating": 1650, "country": "FR"},
]

ONLINE_PLAYERS = [
    {"id": "online_1001", "username": "ChessRanger", "rating": 1180, "country": "IN", "status": "online"},
    {"id": "online_1002", "username": "KnightRider99", "rating": 1210, "country": "IN", "status": "online"},
    {"id": "online_1003", "username": "RookAndRoll", "rating": 1380, "country": "DE", "status": "online"},
    {"id": "online_1004", "username": "PawnStar_2026", "rating": 1100, "country": "BR", "status": "online"},
]

# -------------------------------------------------------------
# 1. ROOT & HEALTH CHECK
# -------------------------------------------------------------

@app.route("/", methods=["GET", "HEAD"])
def health_check():
    return jsonify({"status": "online", "mode": "headless_api"}), 200

# -------------------------------------------------------------
# 2. USER AUTHENTICATION & REGISTRATION (Fixes Image 1 Error -1)
# -------------------------------------------------------------

@app.route("/v1/users/validate-username/<username>", methods=["GET"])
def validate_username(username):
    username = username.strip()
    valid = 3 <= len(username) <= 25 and username.replace("_", "").isalnum()
    # Existing local users remain valid for a retried signup flow.  The APK
    # may validate again after a successful request whose response was lost.
    available = valid
    payload = {
        "valid": valid,
        "available": available,
        "username": username,
        "code": 0 if available else -1,
        "message": "Username available" if available else "Username is not available"
    }
    return jsonify({**payload, "data": payload}), 200


def _request_data():
    """Accept JSON, form data, and the data wrapper used by some clients."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    nested = data.get("data")
    return nested if isinstance(nested, dict) else data


def _auth_payload(user, token):
    user_payload = {
        "id": user["id"],
        "user_id": user["id"],
        "uuid": user["id"],
        "username": user["username"],
        "email": user["email"],
        "avatar": user.get("avatar", ""),
        "is_premium": False,
        "enabled": True,
    }
    return {
        "code": 0,
        "status": "success",
        "success": True,
        "message": "Success",
        "token": token,
        "session_id": token,
        "user_id": user["id"],
        "username": user["username"],
        "user": user_payload,
        "data": {
            # Android parser expects the authenticated user id directly at
            # $.data.id, not only nested inside $.data.user.
            "id": user["id"],
            "token": token,
            "session_id": token,
            "user_id": user["id"],
            "username": user["username"],
            "user": user_payload,
        },
    }


def _create_user(data, guest=False):
    global NEXT_USER_ID
    username = str(data.get("username") or data.get("userName") or "").strip()
    if guest:
        username = username or f"Guest_{uuid.uuid4().hex[:6]}"
    if not username:
        username = f"Player_{uuid.uuid4().hex[:6]}"
    key = username.lower()
    if key in USERS and not guest:
        # Signup can be retried when the APK receives a response but fails to
        # parse it.  Treat the retry as idempotent instead of returning
        # "username already taken" and trapping the user on the signup page.
        user = USERS[key]
        token = f"session_{uuid.uuid4().hex}"
        SESSIONS[token] = user["id"]
        return jsonify(_auth_payload(user, token)), 200
    user = {
        # The Android model parses $.data.id as a Java/Kotlin Long. A UUID
        # string here causes: Expected a long but was <uuid> at $.data.id.
        "id": NEXT_USER_ID,
        "username": username,
        "email": str(data.get("email") or f"{username}@example.com"),
        "avatar": str(data.get("avatar") or ""),
        "password": str(data.get("password") or data.get("pass") or ""),
    }
    NEXT_USER_ID += 1
    token = f"session_{uuid.uuid4().hex}"
    USERS[key] = user
    SESSIONS[token] = user["id"]
    _persist_user(user)
    return jsonify(_auth_payload(user, token)), 200


@app.route("/v1/users/register", methods=["POST"])
@app.route("/v1/auth/register", methods=["POST"])
@app.route("/v1/signup", methods=["POST"])
def register_alias():
    return _create_user(_request_data())


@app.route("/v1/users/login", methods=["POST"])
@app.route("/v1/auth/login", methods=["POST"])
@app.route("/v1/login", methods=["POST"])
def login_user():
    data = _request_data()
    username = str(data.get("username") or data.get("userName") or "").strip().lower()
    email = str(data.get("email") or "").strip().lower()
    user = USERS.get(username)
    if not user and email:
        user = next((candidate for candidate in USERS.values()
                     if candidate.get("email", "").lower() == email), None)
    if not user:
        # This is a local test server: create a test account on first login.
        if email and not username:
            data = {**data, "username": email.split("@", 1)[0]}
        return _create_user(data)
    token = f"session_{uuid.uuid4().hex}"
    SESSIONS[token] = user["id"]
    return jsonify(_auth_payload(user, token)), 200


@app.route("/v1/users", methods=["GET", "POST"])
def register_user():
    if request.method == "GET":
        username = request.args.get("username", "").strip().lower()
        user = USERS.get(username) or _current_user()
        if not user:
            # Guest sessions may be recreated by the APK after a process
            # restart. Return a lightweight profile instead of failing the
            # home screen with HTTP 404.
            user = {
                "id": 1000000,
                "username": request.args.get("username", "Guest"),
                "email": "",
                "avatar": "",
                "password": "",
            }
        return jsonify(_profile_payload(user)), 200
    return _create_user(_request_data())


@app.route("/v1/users/guest-login", methods=["POST"])
def guest_login():
    return _create_user(_request_data(), guest=True)


def _current_user():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    token = token or request.headers.get("X-Session-Token", "").strip()
    user_id = SESSIONS.get(token)
    if user_id:
        return next((u for u in USERS.values() if u["id"] == user_id), None)
    return next(iter(USERS.values()), None)


@app.route("/v1/users/me", methods=["GET"])
@app.route("/v1/me", methods=["GET"])
@app.route("/v1/users/profile", methods=["GET"])
@app.route("/v1/profile", methods=["GET"])
@app.route("/v1/account", methods=["GET"])
def current_profile():
    user = _current_user()
    if not user:
        return jsonify({"code": 9, "status": "error", "message": "Resource not found.",
                        "more_info": "https://test-chess-server.onrender.com/codes#9"}), 404
    return jsonify(_profile_payload(user)), 200


def _profile_payload(user):
    return {
        "code": 0,
        "status": "success",
        "success": True,
        "id": user["id"],
        "user_id": user["id"],
        "username": user["username"],
        "user": user,
        "data": {
            "id": user["id"],
            "user_id": user["id"],
            "username": user["username"],
            "user": user,
        },
    }


@app.route("/v1/users/<username>", methods=["GET"])
def user_profile(username):
    lookup = username.strip()
    user = USERS.get(lookup.lower())
    if not user and lookup.isdigit():
        numeric_id = int(lookup)
        user = next((candidate for candidate in USERS.values()
                     if candidate["id"] == numeric_id), None)
    if not user:
        return jsonify({"code": 9, "status": "error", "message": "Resource not found.",
                        "more_info": "https://api.chess.com/codes#9"}), 404
    return jsonify(_profile_payload(user)), 200


@app.route("/v1/friends", methods=["GET"])
@app.route("/v1/friends/list", methods=["GET"])
@app.route("/v1/users/friends", methods=["GET"])
@app.route("/v1/users/friends/list", methods=["GET"])
def friends():
    empty = []
    return jsonify({"code": 0, "status": "success", "success": True,
                    # This Android model expects $.data to be an object and
                    # reads the friends list from $.data.friends.
                    "data": {"friends": empty, "items": empty, "count": 0},
                    "friends": empty, "items": empty, "count": 0}), 200


def _user_search_results():
    query = (request.args.get("q") or request.args.get("query") or
             request.args.get("username") or request.args.get("search") or "").strip().lower()
    users = list(USERS.values())
    if query:
        users = [user for user in users if query in user["username"].lower() or
                 query in user.get("email", "").lower()]
    results = [{"id": user["id"], "user_id": user["id"],
                "username": user["username"], "email": user.get("email", ""),
                "avatar": user.get("avatar", ""), "rating": 1200,
                "online": False} for user in users]
    return jsonify({"code": 0, "status": "success", "success": True,
                    "data": {"users": results, "results": results, "items": results},
                    "users": results, "results": results, "items": results,
                    "count": len(results)}), 200


@app.route("/v1/users/search", methods=["GET", "POST"])
@app.route("/v1/search/users", methods=["GET", "POST"])
@app.route("/v1/friends/search", methods=["GET", "POST"])
@app.route("/v1/contacts/search", methods=["GET", "POST"])
def search_users():
    return _user_search_results()


@app.route("/v1/friends/requests", methods=["GET", "POST"])
@app.route("/v1/friend-requests", methods=["GET", "POST"])
def friend_requests():
    empty = []
    return jsonify({"code": 0, "status": "success", "success": True,
                    "data": {"requests": empty, "incoming": empty, "outgoing": empty},
                    "requests": empty, "incoming": empty, "outgoing": empty}), 200


@app.route("/v1/users/flair-status", methods=["GET"])
def flair_status():
    return jsonify({"code": 0, "status": "success", "data": {"flairs": []},
                    "flairs": []}), 200


@app.route("/v1/games/challenges", methods=["GET", "POST"])
def game_challenges():
    if request.method == "POST":
        return _create_custom_room()
    return jsonify({"code": 0, "status": "success", "data": [],
                    "challenges": [], "items": []}), 200


@app.route("/v1/games/current", methods=["GET"])
def current_games():
    games = []
    for game_id, game in GAMES.items():
        games.append({"id": game_id, "game_id": game_id, "status": game.get("status", "active"),
                      "fen": game["board"].fen(), "opponent": game.get("opponent", {})})
    return jsonify({"code": 0, "status": "success", "data": {"games": games},
                    "games": games}), 200


@app.route("/v1/users/notifications/current", methods=["GET"])
def current_notifications():
    return jsonify({"code": 0, "status": "success", "data": {"notifications": []},
                    "notifications": []}), 200


@app.route("/v1/league/user", methods=["GET"])
def league_user():
    return jsonify({"code": 0, "status": "success",
                    "data": {"league": None, "rank": 0, "points": 0},
                    "league": None, "rank": 0, "points": 0}), 200

# -------------------------------------------------------------
# 3. APP CONFIGURATION & HOME FEED (Fixes Image 3 BEGIN_ARRAY Error)
# -------------------------------------------------------------

@app.route("/v1/config", methods=["GET"])
def app_config():
    scheme = "wss" if request.is_secure else "ws"
    ws_host = request.host
    # Return as list or expected format array wrapper if client queries root endpoint
    return jsonify({
        "status": "ok",
        "endpoints": {
            "websocket": f"{scheme}://{ws_host}/ws/live"
        },
        "features": []
    }), 200

@app.route("/v1/home", methods=["GET"])
@app.route("/v1/feed", methods=["GET"])
def home_feed():
    # The mobile home/feed response is an object.  Returning [] here causes
    # Gson/Moshi clients that deserialize a HomeResponse to fail with:
    # "Expected BEGIN_OBJECT but was BEGIN_ARRAY at path $".
    return jsonify({
        "status": "ok",
        "data": {
            "sections": [],
            "items": []
        }
    }), 200

@app.route("/v1/computer/bot-personalities", methods=["GET"])
def bot_personalities():
    # The home screen deserializes this endpoint as List<BotPersonality>.
    # Do not wrap it in {"data": ...}; that causes BEGIN_ARRAY/BEGIN_OBJECT.
    return jsonify([
        {"id": "bot_easy", "name": "Novice Bot", "rating": 400, "avatarUrl": ""},
        {"id": "bot_medium", "name": "Intermediate Bot", "rating": 1200, "avatarUrl": ""},
        {"id": "bot_hard", "name": "Grandmaster Bot", "rating": 2200, "avatarUrl": ""}
    ]), 200

@app.route("/v1/puzzles/daily/today", methods=["GET"])
def daily_puzzle():
    puzzle = {
        "id": "daily_puzzle_01",
        "puzzleId": "daily_puzzle_01",
        "fen": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
        "rating": 1000,
        "initialFen": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
        "moves": ["c6b4", "f3e5", "b4c2"],
        "solution": ["c6b4", "f3e5", "b4c2"],
        "theme": "opening",
        "status": "ready"
    }
    return jsonify(puzzle), 200


@app.route("/v1/tactics/puzzle-path/friends/stats", methods=["GET"])
def puzzle_path_friend_stats():
    stats = _complete_stats_schema()
    return jsonify({"code": 0, "status": "success", "data": stats, **stats}), 200


def _complete_stats_schema(game_type=90):
    """Return all counters used by different Android stats model versions."""
    now = int(time.time() * 1000)
    return {
        "gameType": game_type,
        "game_type": game_type,
        "rating": 1200,
        "bestRating": 1200,
        "best_rating": 1200,
        "highestRating": 1200,
        "highest_rating": 1200,
        # Android parses game_ratings as an object/map, not a JSON array.
        "game_ratings": {
            "bullet": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "blitz": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "rapid": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "daily": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "tactics": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "puzzles": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
        },
        "gameRatings": {
            "bullet": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "blitz": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "rapid": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "daily": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "tactics": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
            "puzzles": {"rating": 1200, "last_rating": 1200, "previous_rating": 1200, "delta": 0, "rating_delta": 0, "games_count": 0, "game_count": 0, "wins": 0, "losses": 0, "draws": 0},
        },
        "ratings": {},
        "rating_history": [],
        "ratingHistory": [],
        "rating_points": [],
        "game_count": 999,
        "games_count": 999,
        "gamesPlayed": 999,
        "games_played": 999,
        "gamesWon": 999,
        "games_won": 999,
        "gamesLost": 999,
        "games_lost": 999,
        "gamesDrawn": 999,
        "games_drawn": 999,
        "winRate": 999,
        "win_rate": 999,
        "tactics_count": 9999,
        "tacticsCount": 9999,
        "puzzles_count": 999,
        "puzzle_count": 999,
        "puzzlesCount": 999,
        "lesson_count": 999,
        "lessons_count": 999,
        "lessonCount": 999,
        "lessonsCompleted": 0,
        "lessons_completed": 999,
        "solved": 999,
        "solved_count": 999,
        "correct": 0,
        "incorrect": 0,
        "attempts": 0,
        "streak": 0,
        "current_streak": 999,
        "best_streak": 999,
        "maxStreak": 999,
        "points": 999,
        "xp": 999,
        "level": 99,
        "rank": 1,
        "graph": [],
        "ratingGraph": [],
        "rating_graph": [],
        "lastGraphTimestamp": now,
        "last_graph_timestamp": now,
        "lastPlayed": 5,
        "last_played": 0,
        "createdAt": 0,
        "updatedAt": now,
    }


@app.route("/v1/tactics/stats", methods=["GET"])
def tactics_stats():
    stats = _complete_stats_schema()
    return jsonify({"code": 0, "status": "success", "data": stats, **stats}), 200


@app.route("/v1/users/stats2/overview/<int:game_type>", methods=["GET"])
def user_stats_overview(game_type):
    """Stats overview used by the profile/statistics screen.

    The APK requests this endpoint with game type 90 for tactics. Keep all
    numeric fields present so the Android model can deserialize the response.
    """
    overview = _complete_stats_schema(game_type)
    return jsonify({"code": 0, "status": "success", "data": overview, **overview}), 200


@app.route("/v1/users/fcm", methods=["POST", "PUT"])
def register_fcm_token():
    payload = request.get_json(silent=True) or {}
    return jsonify({
        "code": 0,
        "status": "success",
        "success": True,
        "data": {"registered": True, "token": payload.get("token", "")},
    }), 200

@app.route("/v1/tactics-batch", methods=["GET"])
@app.route("/v1/tv/show", methods=["GET"])
@app.route("/v1/watch", methods=["GET"])
def array_endpoints():
    if request.path == "/v1/tactics-batch":
        payload = {"tactics": []}
    else:
        payload = {"live_games": []}
    return jsonify({"status": "ok", "data": payload}), 200

@app.route("/v1/mastery-lessons/<path:subpath>", methods=["GET"])
def mastery_lessons(subpath):
    return jsonify({
        "status": "ok",
        "data": {"courses": [], "levels": []}
    }), 200

# -------------------------------------------------------------
# 4. MATCHMAKING & COMETD EMULATION (Fixes Image 2 Searching Stuck)
# -------------------------------------------------------------

def create_fake_game():
    """Create an immediately matched local test game for the mobile client."""
    game_id = str(uuid.uuid4())[:8]
    opponent = random.choice(ONLINE_PLAYERS)
    player_color = random.choice(["white", "black"])
    GAMES[game_id] = {
        "board": chess.Board(),
        "opponent": opponent,
        "created_at": time.time(),
        "status": "active",
        "color": player_color,
        "time_control": "10+0",
    }
    return game_id, opponent, player_color


def _room_code(value=None):
    """Return a short, human-shareable room code that is safe in URLs."""
    candidate = str(value or "").strip().upper()
    if candidate and len(candidate) <= 12 and candidate.isalnum():
        return candidate
    while True:
        candidate = uuid.uuid4().hex[:6].upper()
        if candidate not in ROOMS:
            return candidate


def _room_payload(room):
    game_id = room["game_id"]
    return {
        "code": 0,
        "status": "success",
        "success": True,
        "message": "Custom room ready",
        "room": room,
        "data": room,
        "room_id": room["room_id"],
        "roomId": room["room_id"],
        "room_code": room["room_code"],
        "roomCode": room["room_code"],
        "game_id": game_id,
        "gameId": game_id,
        "id": room["room_id"],
    }


def _room_request_data():
    data = _request_data()
    # Some clients submit the room code as a query parameter while posting
    # an empty body. Accept both forms to avoid a client-version-specific 404.
    if not data:
        data = request.args.to_dict()
    return data


def _create_custom_room():
    data = _room_request_data()
    requested = (data.get("room_code") or data.get("roomCode") or
                 data.get("code") or data.get("id"))
    room_code = _room_code(requested)
    if room_code in ROOMS:
        return jsonify(_room_payload(ROOMS[room_code])), 200

    game_id, opponent, player_color = create_fake_game()
    room = {
        "room_id": room_code,
        "room_code": room_code,
        "game_id": game_id,
        "gameId": game_id,
        "status": "waiting",
        "type": data.get("type") or data.get("game_type") or "standard",
        "time_control": data.get("time_control") or data.get("timeControl") or "10+0",
        "rated": bool(data.get("rated", data.get("isRated", False))),
        "creator": data.get("username") or data.get("userName") or "Guest",
        "opponent": opponent,
        "color": player_color,
    }
    GAMES[game_id]["room_id"] = room_code
    GAMES[game_id]["status"] = "waiting"
    ROOMS[room_code] = room
    with STATE_LOCK:
        _persist_game(game_id)
        _persist_room(room)
    return jsonify(_room_payload(room)), 200


def _find_room(room_code):
    return ROOMS.get(str(room_code or "").strip().upper())


def _join_custom_room(room_code=None):
    data = _room_request_data()
    room = _find_room(room_code or data.get("room_code") or data.get("roomCode") or
                      data.get("code") or data.get("id"))
    if not room:
        return jsonify({"code": 404, "status": "not_found", "success": False,
                        "message": "Custom room not found"}), 404
    room["status"] = "active"
    game = GAMES.get(room["game_id"])
    if game:
        game["status"] = "active"
        with STATE_LOCK:
            _persist_game(room["game_id"])
            _persist_room(room)
    return jsonify(_room_payload(room)), 200


@app.route("/v1/rooms", methods=["GET", "POST"])
@app.route("/v1/room", methods=["GET", "POST"])
@app.route("/v1/custom-room", methods=["GET", "POST"])
@app.route("/v1/games/rooms", methods=["GET", "POST"])
@app.route("/v1/games/custom", methods=["GET", "POST"])
@app.route("/v1/game/custom", methods=["GET", "POST"])
@app.route("/v1/game/custom-room", methods=["GET", "POST"])
@app.route("/v1/challenges", methods=["GET", "POST"])
def custom_rooms():
    if request.method == "POST":
        return _create_custom_room()
    requested = request.args.get("room_code") or request.args.get("roomCode") or request.args.get("code")
    if requested:
        return _join_custom_room(requested)
    rooms = list(ROOMS.values())
    return jsonify({"code": 0, "status": "success", "success": True,
                    "rooms": rooms, "data": {"rooms": rooms}}), 200


@app.route("/v1/rooms/<room_code>", methods=["GET", "POST"])
@app.route("/v1/room/<room_code>", methods=["GET", "POST"])
@app.route("/v1/custom-room/<room_code>", methods=["GET", "POST"])
@app.route("/v1/games/rooms/<room_code>", methods=["GET", "POST"])
@app.route("/v1/games/custom/<room_code>", methods=["GET", "POST"])
@app.route("/v1/game/custom/<room_code>", methods=["GET", "POST"])
@app.route("/v1/game/custom-room/<room_code>", methods=["GET", "POST"])
@app.route("/v1/challenges/<room_code>", methods=["GET", "POST"])
def custom_room_by_code(room_code):
    return _join_custom_room(room_code)


@app.route("/v1/players/online", methods=["GET"])
@app.route("/v1/users/online", methods=["GET"])
@app.route("/v1/presence", methods=["GET"])
def online_players():
    """Return deterministic fake presence for clients that load online users."""
    return jsonify({
        "status": "ok",
        "data": {"players": ONLINE_PLAYERS, "count": len(ONLINE_PLAYERS)},
        "players": ONLINE_PLAYERS,
        "count": len(ONLINE_PLAYERS),
    }), 200


@app.route("/v1/matchmaking", methods=["POST", "GET"])
@app.route("/v1/matchmaking/start", methods=["POST", "GET"])
@app.route("/v1/matchmaking/quick", methods=["POST", "GET"])
def start_matchmaking():
    return _match_response()

@app.route("/v1/matchmaking/find", methods=["POST", "GET"])
@app.route("/v1/game/quickpair", methods=["POST", "GET"])
def find_match():
    return _match_response()


def _queue_player(data):
    player_id = str(data.get("user_id") or data.get("userId") or
                    data.get("username") or data.get("userName") or
                    request.headers.get("X-User-Id") or "guest").strip()
    return player_id, {
        "id": player_id,
        "username": str(data.get("username") or data.get("userName") or player_id),
        "rating": int(data.get("rating") or 1200),
        "queued_at": time.time(),
        "time_control": data.get("time_control") or data.get("timeControl") or "10+0",
    }


def _queue_match_response(player_id, player):
    # Match the oldest compatible waiting player, never the same player.
    opponent_id = next((candidate_id for candidate_id, candidate in MATCHMAKING_QUEUE.items()
                        if candidate_id != player_id and
                        candidate["time_control"] == player["time_control"]), None)
    if opponent_id is None:
        MATCHMAKING_QUEUE[player_id] = player
        with STATE_LOCK:
            _persist_queue(player_id, player)
        return jsonify({"code": 0, "status": "searching", "success": True,
                        "matched": False, "queue_id": player_id,
                        "data": {"status": "searching", "matched": False,
                                  "queue_id": player_id}}), 200

    opponent = MATCHMAKING_QUEUE.pop(opponent_id)
    with STATE_LOCK:
        _persist_queue(opponent_id)
    game_id = str(uuid.uuid4())[:8]
    player_color = random.choice(["white", "black"])
    GAMES[game_id] = {
        "board": chess.Board(), "created_at": time.time(), "status": "active",
        "color": player_color, "time_control": player["time_control"],
        "opponent": opponent, "players": [player, opponent],
    }
    with STATE_LOCK:
        _persist_game(game_id)
    payload = {"code": 0, "status": "matched", "success": True, "matched": True,
               "game_id": game_id, "gameId": game_id, "id": game_id,
               "opponent": opponent, "player": {"color": player_color},
               "color": player_color, "time_control": player["time_control"],
               "timeControl": player["time_control"], "fen": chess.Board().fen()}
    return jsonify({**payload, "data": payload}), 200


@app.route("/v1/matchmaker/join", methods=["POST"])
@app.route("/v1/matchmaking/queue", methods=["POST"])
@app.route("/v1/play-online", methods=["POST"])
def join_matchmaking_queue():
    data = _request_data()
    player_id, player = _queue_player(data)
    return _queue_match_response(player_id, player)


@app.route("/v1/matchmaker/status/<player_id>", methods=["GET"])
@app.route("/v1/matchmaking/queue/<player_id>", methods=["GET"])
def matchmaking_queue_status(player_id):
    queued = MATCHMAKING_QUEUE.get(str(player_id))
    if queued:
        return jsonify({"code": 0, "status": "searching", "success": True,
                        "matched": False, "data": {"status": "searching",
                        "matched": False, "queue_id": str(player_id)}}), 200
    return jsonify({"code": 0, "status": "not_queued", "success": True,
                    "matched": False, "data": {"status": "not_queued",
                    "matched": False}}), 200


@app.route("/v1/matchmaker/cancel", methods=["POST", "DELETE"])
@app.route("/v1/matchmaking/queue/cancel", methods=["POST", "DELETE"])
def cancel_matchmaking_queue():
    data = _request_data()
    player_id = str(data.get("user_id") or data.get("userId") or
                    data.get("username") or data.get("userName") or
                    request.args.get("user_id") or "guest")
    removed = MATCHMAKING_QUEUE.pop(player_id, None) is not None
    if removed:
        with STATE_LOCK:
            _persist_queue(player_id)
    return jsonify({"code": 0, "status": "cancelled", "success": True,
                    "removed": removed, "data": {"cancelled": True,
                    "removed": removed}}), 200


@app.route("/v1/test/special", methods=["GET", "POST"])
@app.route("/v1/test/matchmaker", methods=["GET", "POST"])
def special_matchmaker_test():
    """Safe diagnostic endpoint for APK/server integration testing."""
    return jsonify({
        "code": 0,
        "status": "ok",
        "success": True,
        "test": "matchmaker_ready",
        "server": {"games": len(GAMES), "rooms": len(ROOMS),
                    "queued_players": len(MATCHMAKING_QUEUE)},
        "endpoints": {
            "friends_search": "/v1/friends/search?q=guest",
            "play_online": "/v1/play-online",
            "queue_status": "/v1/matchmaker/status/<player_id>",
            "queue_cancel": "/v1/matchmaker/cancel",
            "realtime_friends": "/ws/friends",
        },
    }), 200


def _match_response():
    game_id, opponent, player_color = create_fake_game()
    return jsonify({
        "code": 0,
        "status": "matched",
        "success": True,
        "game_id": game_id,
        "gameId": game_id,
        "id": game_id,
        "opponent": {
            "id": opponent["id"],
            "username": opponent["username"],
            "rating": opponent["rating"],
            "country": opponent["country"],
            "avatar": "",
            "online": True,
            "status": "online",
        },
        "player": {"color": player_color},
        "color": player_color,
        "time_control": "10+0",
        "timeControl": "10+0",
        "initial_fen": chess.Board().fen(),
        "fen": chess.Board().fen(),
    }), 200


@app.route("/v1/matchmaking/status/<game_id>", methods=["GET"])
@app.route("/v1/game/<game_id>", methods=["GET"])
def game_status(game_id):
    game = GAMES.get(game_id)
    if not game:
        return jsonify({"code": 404, "status": "not_found", "message": "Game not found"}), 404
    return jsonify({
        "code": 0,
        "status": game.get("status", "active"),
        "game_id": game_id,
        "gameId": game_id,
        "fen": game["board"].fen(),
        "opponent": game["opponent"],
        "color": game["color"],
        "time_control": game["time_control"],
    }), 200


@app.route("/v1/game/<game_id>/move", methods=["POST"])
@app.route("/v1/matchmaking/<game_id>/move", methods=["POST"])
def play_move(game_id):
    game = GAMES.get(game_id)
    if not game:
        return jsonify({"code": 404, "status": "not_found", "message": "Game not found"}), 404

    payload = request.get_json(silent=True) or {}
    move_uci = payload.get("move") or payload.get("uci") or payload.get("move_uci")
    if not move_uci:
        return jsonify({"code": 400, "status": "error", "message": "A UCI move is required"}), 400
    try:
        move = chess.Move.from_uci(move_uci)
    except ValueError:
        return jsonify({"code": 400, "status": "error", "message": "Invalid UCI move"}), 400
    board = game["board"]
    if move not in board.legal_moves:
        return jsonify({"code": 400, "status": "error", "message": "Illegal move"}), 400

    board.push(move)
    with STATE_LOCK:
        _persist_game(game_id)
    return jsonify({
        "code": 0,
        "status": "ok",
        "game_id": game_id,
        "gameId": game_id,
        "move": move_uci,
        "fen": board.fen(),
        "last_move": move_uci,
    }), 200


@app.route("/v1/matchmaking/cancel", methods=["POST", "DELETE"])
def cancel_matchmaking():
    return jsonify({"code": 0, "status": "cancelled", "success": True}), 200

@app.route("/cometd", methods=["POST"])
@app.route("/cometd/", methods=["POST"])
@app.route("/cometd/handshake", methods=["POST"])
def cometd_engine():
    data = request.get_json(silent=True) or []
    if isinstance(data, dict):
        data = [data]

    response = []
    for msg in data:
        channel = msg.get("channel")
        msg_id = msg.get("id", "1")
        client_id = f"client_{uuid.uuid4().hex[:6]}"

        if channel == "/meta/handshake":
            response.append({
                "id": msg_id,
                "channel": "/meta/handshake",
                "successful": True,
                "clientId": client_id,
                "supportedConnectionTypes": ["websocket", "long-polling"],
                "version": "1.0"
            })
        elif channel == "/meta/connect":
            game_id = str(uuid.uuid4())[:8]
            opponent = random.choice(FAKE_PLAYERS)
            response.append({
                "id": msg_id,
                "channel": "/meta/connect",
                "successful": True,
                "advice": {"reconnect": "retry", "interval": 0},
                "data": {
                    "event": "game_start",
                    "game_id": game_id,
                    "opponent": opponent["username"]
                }
            })
        elif channel in ["/meta/subscribe", "/meta/unsubscribe"]:
            response.append({
                "id": msg_id,
                "channel": channel,
                "successful": True
            })
        else:
            response.append({
                "id": msg_id,
                "channel": channel,
                "successful": True,
                "data": {}
            })

    return jsonify(response)

# -------------------------------------------------------------
# 5. WEBSOCKET ENGINE
# -------------------------------------------------------------

def _friends_search_payload(query=""):
    query = str(query or "").strip().lower()
    results = list(USERS.values())
    if query:
        results = [user for user in results if query in user["username"].lower() or
                   query in user.get("email", "").lower()]
    friends = [{"id": user["id"], "user_id": user["id"],
                "username": user["username"], "email": user.get("email", ""),
                "avatar": user.get("avatar", ""), "rating": 1200,
                "online": False} for user in results]
    return {"event": "friends_updated", "query": query, "friends": friends,
            "results": friends, "count": len(friends)}


@sock.route("/ws/friends")
@sock.route("/ws/friends/search")
def friends_realtime_websocket(ws):
    """Push friend-search results immediately whenever the query changes."""
    ws.send(json.dumps(_friends_search_payload()))
    while True:
        raw_msg = ws.receive()
        if not raw_msg:
            break
        try:
            payload = json.loads(raw_msg) if isinstance(raw_msg, str) else {}
            query = payload.get("query") or payload.get("q") or payload.get("username") or ""
            ws.send(json.dumps(_friends_search_payload(query)))
        except (TypeError, ValueError):
            ws.send(json.dumps({"event": "error", "message": "Invalid JSON query"}))


@sock.route("/ws/live/<game_id>")
def live_websocket(ws, game_id):
    if game_id not in GAMES:
        GAMES[game_id] = {"board": chess.Board()}
    
    board = GAMES[game_id]["board"]
    ws.send(json.dumps({"event": "connected", "fen": board.fen()}))

    while True:
        raw_msg = ws.receive()
        if not raw_msg:
            break
        try:
            payload = json.loads(raw_msg)
            if payload.get("action") == "move":
                move_uci = payload.get("move")
                if move_uci:
                    move = chess.Move.from_uci(move_uci)
                    if move in board.legal_moves:
                        board.push(move)
                        with STATE_LOCK:
                            _persist_game(game_id)
                        ws.send(json.dumps({"event": "move_played", "fen": board.fen()}))
                    else:
                        ws.send(json.dumps({"event": "error", "message": "Illegal move"}))
        except Exception as err:
            ws.send(json.dumps({"event": "error", "message": str(err)}))

# -------------------------------------------------------------
# 6. CATCH-ALL ROUTE
# -------------------------------------------------------------

@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def catch_all(path):
    # Never return an arbitrary array for an unknown endpoint.  The Android
    # client may try to deserialize this response as an object, which turns a
    # harmless 404 into a JSON parsing crash.
    return jsonify({
        "status": "error",
        "code": "not_found",
        "message": f"Endpoint not found: /{path}"
    }), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
