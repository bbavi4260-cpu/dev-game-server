import json

from server import app, GAMES, ROOMS, MATCHMAKING_QUEUE


def test_custom_room_flow():
    GAMES.clear()
    ROOMS.clear()
    client = app.test_client()

    response = client.post("/v1/custom-room", json={"timeControl": "10+0", "rated": False})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["code"] == 0
    assert payload["success"] is True
    room_code = payload["roomCode"]
    game_id = payload["gameId"]
    assert room_code in ROOMS
    assert game_id in GAMES

    for path in (f"/v1/rooms/{room_code}", f"/v1/room/{room_code}",
                 f"/v1/custom-room/{room_code}",
                 f"/v1/games/rooms/{room_code}"):
        joined = client.post(path, json={"username": "second-player"})
        assert joined.status_code == 200, (path, joined.get_data(as_text=True))
        assert joined.get_json()["game_id"] == game_id

    status = client.get(f"/v1/game/{game_id}")
    assert status.status_code == 200
    assert status.get_json()["status"] == "active"

    missing = client.get("/v1/custom-room/DOESNOTEXIST")
    assert missing.status_code == 404
    assert missing.get_json()["code"] == 404


def test_query_code_alias():
    client = app.test_client()
    created = client.post("/v1/rooms", json={}).get_json()
    response = client.get(f"/v1/rooms?roomCode={created['roomCode']}")
    assert response.status_code == 200
    assert response.get_json()["roomCode"] == created["roomCode"]


def test_friends_and_search_aliases():
    client = app.test_client()
    for path in ("/v1/friends", "/v1/friends/list", "/v1/users/friends",
                 "/v1/users/friends/list"):
        response = client.get(path)
        assert response.status_code == 200, path
        payload = response.get_json()
        assert payload["code"] == 0
        assert isinstance(payload["data"], dict)
        assert payload["data"]["friends"] == []
    for path in ("/v1/users/search?q=guest", "/v1/search/users?query=guest",
                 "/v1/friends/search?username=guest", "/v1/contacts/search?search=guest"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.get_json()["code"] == 0

    for path in ("/v1/games/custom", "/v1/game/custom", "/v1/game/custom-room",
                 "/v1/challenges"):
        response = client.post(path, json={})
        assert response.status_code == 200, path
        assert response.get_json()["code"] == 0


def test_special_matchmaker_and_play_online():
    GAMES.clear()
    MATCHMAKING_QUEUE.clear()
    client = app.test_client()

    special = client.get("/v1/test/special")
    assert special.status_code == 200
    assert special.get_json()["test"] == "matchmaker_ready"

    waiting = client.post("/v1/play-online", json={"userId": "player-a", "timeControl": "10+0"})
    assert waiting.status_code == 200
    assert waiting.get_json()["status"] == "searching"

    matched = client.post("/v1/matchmaker/join", json={"userId": "player-b", "timeControl": "10+0"})
    assert matched.status_code == 200
    assert matched.get_json()["status"] == "matched"
    assert matched.get_json()["gameId"] in GAMES

    status = client.get("/v1/matchmaker/status/player-a")
    assert status.status_code == 200
    cancel = client.post("/v1/matchmaker/cancel", json={"userId": "player-a"})
    assert cancel.status_code == 200


if __name__ == "__main__":
    test_custom_room_flow()
    test_query_code_alias()
    test_friends_and_search_aliases()
    test_special_matchmaker_and_play_online()
    print(json.dumps({"custom_room_tests": "passed"}))
