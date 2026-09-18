# Chess server fixes

The server now persists users, rooms, games, and matchmaking queue entries in SQLite (`chess.db` by default, or `CHESS_DB_PATH`). SQLite WAL mode and a 30-second busy timeout reduce write contention and prevent transient database-lock crashes.

## Deployment

Keep the existing Gunicorn command. For Render, attach a persistent disk if game data must survive redeploys; set `CHESS_DB_PATH` to a file on that disk. Without a persistent disk, the API remains stable but data is lost when the instance is replaced.

## Client endpoints

Use the existing `/v1/play-online`, `/v1/matchmaker/status/<player_id>`, `/v1/matchmaker/cancel`, `/v1/game/<game_id>`, and `/v1/game/<game_id>/move` endpoints. Unknown routes return a JSON 404 payload instead of an HTML page, preventing mobile JSON parsing crashes.

## Verification

`python3 test_custom_rooms.py` and `pytest -q` pass. `python3 smoke_test.py` validates HTTP serving, JSON 404 behavior, matchmaking, legal moves, and restart recovery.
