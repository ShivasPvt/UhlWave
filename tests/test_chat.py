"""End-to-end checks for the mandatory features. Run with:  pytest -q"""
import pytest
from starlette.websockets import WebSocketDisconnect


def receive(ws, wanted_type):
    """Read events until one of the wanted type arrives (skips presence noise)."""
    for _ in range(20):
        event = ws.receive_json()
        if event["type"] == wanted_type:
            return event
    raise AssertionError(f"never received {wanted_type}")


def start_chat(client, user, other_username):
    r = client.post("/api/conversations", json={"username": other_username}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ---------- authentication ----------

def test_register_login_logout(client):
    r = client.post("/api/auth/register", json={"username": "AuthUser1", "password": "secret123"})
    assert r.status_code == 201
    assert r.json()["username"] == "authuser1"
    assert client.get("/api/auth/me").json()["username"] == "authuser1"

    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401

    assert client.post("/api/auth/login", json={"username": "authuser1", "password": "nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "AUTHUSER1", "password": "secret123"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    client.cookies.clear()


def test_duplicate_and_invalid_registration(client):
    client.post("/api/auth/register", json={"username": "dupe_user", "password": "secret123"})
    client.cookies.clear()
    assert client.post("/api/auth/register", json={"username": "dupe_user", "password": "secret123"}).status_code == 409
    assert client.post("/api/auth/register", json={"username": "x", "password": "secret123"}).status_code == 422
    assert client.post("/api/auth/register", json={"username": "bad name!", "password": "secret123"}).status_code == 422
    assert client.post("/api/auth/register", json={"username": "shortpw", "password": "123"}).status_code == 422
    client.cookies.clear()


def test_nobody_can_log_in_as_the_bot(client):
    assert client.post("/api/auth/login", json={"username": "ai", "password": "!"}).status_code == 401
    assert client.post("/api/auth/register", json={"username": "ai", "password": "secret123"}).status_code != 201


def test_pages_redirect_based_on_login(client, make_user):
    alice = make_user("alice")
    assert client.get("/chat", follow_redirects=False).headers["location"] == "/"
    assert client.get("/", headers=alice["headers"], follow_redirects=False).headers["location"] == "/chat"
    assert client.get("/chat", headers=alice["headers"]).status_code == 200


def test_websocket_rejects_logged_out_users(client):
    with client.websocket_connect("/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


# ---------- one-to-one chat ----------

def test_start_chat_is_idempotent(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    first = start_chat(client, alice, bob["username"])
    again = start_chat(client, alice, bob["username"])
    from_bob = start_chat(client, bob, alice["username"])
    assert first["id"] == again["id"] == from_bob["id"]
    assert first["title"] == bob["username"]
    assert client.post("/api/conversations", json={"username": alice["username"]}, headers=alice["headers"]).status_code == 400


def test_real_time_message_delivery_and_persistence(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    conversation = start_chat(client, alice, bob["username"])
    cid = conversation["id"]

    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice, \
         client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        assert receive(ws_alice, "presence") == {"type": "presence", "user_id": bob["id"], "online": True}

        ws_alice.send_json({"type": "message", "conversation_id": cid, "content": "  hi bob  ", "client_id": "tmp-1"})

        to_bob = receive(ws_bob, "message")
        to_alice = receive(ws_alice, "message")
        assert to_bob["message"]["content"] == "hi bob"
        assert to_bob["message"]["sender_id"] == alice["id"]
        assert to_bob["message"]["conversation_id"] == cid
        assert to_alice["client_id"] == "tmp-1"
        assert to_alice["message"]["id"] == to_bob["message"]["id"]

        ws_bob.send_json({"type": "message", "conversation_id": cid, "content": "hey alice"})
        assert receive(ws_alice, "message")["message"]["sender_username"] == bob["username"]

    history = client.get(f"/api/conversations/{cid}/messages", headers=bob["headers"]).json()
    assert [(m["sender_id"], m["content"]) for m in history] == [(alice["id"], "hi bob"), (bob["id"], "hey alice")]

    listed = client.get("/api/conversations", headers=alice["headers"]).json()
    assert listed[0]["id"] == cid and listed[0]["last_message"]["content"] == "hey alice"


def test_outsiders_cannot_read_or_post(client, make_user):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    cid = start_chat(client, alice, bob["username"])["id"]

    assert client.get(f"/api/conversations/{cid}/messages", headers=eve["headers"]).status_code == 404
    with client.websocket_connect("/ws", headers=eve["headers"]) as ws_eve:
        ws_eve.send_json({"type": "message", "conversation_id": cid, "content": "sneaky", "client_id": "x"})
        error = receive(ws_eve, "error")
        assert "not in that conversation" in error["detail"]
    assert client.get(f"/api/conversations/{cid}/messages", headers=alice["headers"]).json() == []


def test_bad_input_does_not_kill_the_socket(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        ws.send_text("not json")
        assert receive(ws, "error")["detail"] == "Send JSON objects only."
        ws.send_json({"type": "dance"})
        assert "Unknown event" in receive(ws, "error")["detail"]
        ws.send_json({"type": "message", "conversation_id": cid, "content": "still alive"})
        assert receive(ws, "message")["message"]["content"] == "still alive"


def test_typing_goes_to_the_other_person_only(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice, \
         client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        ws_alice.send_json({"type": "typing", "conversation_id": cid, "is_typing": True})
        event = receive(ws_bob, "typing")
        assert event["username"] == alice["username"] and event["is_typing"] is True
        # Alice should get her own message back next, not her own typing event.
        ws_alice.send_json({"type": "message", "conversation_id": cid, "content": "done"})
        assert ws_alice.receive_json()["type"] in {"presence", "message"}


def test_history_pagination(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        for i in range(60):
            ws.send_json({"type": "message", "conversation_id": cid, "content": f"m{i}"})
            receive(ws, "message")
    newest = client.get(f"/api/conversations/{cid}/messages", headers=bob["headers"]).json()
    assert len(newest) == 50 and newest[-1]["content"] == "m59" and newest[0]["content"] == "m10"
    older = client.get(f"/api/conversations/{cid}/messages?before={newest[0]['id']}", headers=bob["headers"]).json()
    assert [m["content"] for m in older] == [f"m{i}" for i in range(10)]


# ---------- AI participant ----------

def test_ai_replies_in_a_dm_with_the_bot(client, make_user):
    alice = make_user("alice")
    cid = start_chat(client, alice, "ai")["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": cid, "content": "hello"})
        assert receive(ws, "message")["message"]["content"] == "hello"
        assert receive(ws, "typing")["is_typing"] is True
        reply = receive(ws, "message")["message"]
        assert reply["sender_is_bot"] is True
        assert "LLM_API_KEY" in reply["content"]  # no key configured in tests


def test_ai_joins_when_mentioned(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
            ws_alice.send_json({"type": "message", "conversation_id": cid, "content": "plain message"})
            receive(ws_alice, "message")
            ws_alice.send_json({"type": "message", "conversation_id": cid, "content": "hey @AI explain joins"})
            receive(ws_alice, "message")
            reply = receive(ws_alice, "message")["message"]
            assert reply["sender_is_bot"] is True
        # Bob saw both human messages and the AI's reply.
        contents = [receive(ws_bob, "message")["message"]["content"] for _ in range(3)]
        assert contents[:2] == ["plain message", "hey @AI explain joins"]


def test_bot_prompt_includes_usernames():
    from app.bot import build_messages

    out = build_messages([
        {"sender_is_bot": False, "sender_username": "riya", "content": "hi"},
        {"sender_is_bot": True, "sender_username": "ai", "content": "hello!"},
    ])
    assert out[0]["role"] == "system"
    assert out[1] == {"role": "user", "content": "riya: hi"}
    assert out[2] == {"role": "assistant", "content": "hello!"}
