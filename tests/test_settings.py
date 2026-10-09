"""Settings: privacy toggles, disappearing messages, blocking, and account controls."""
from datetime import timedelta

import pytest
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from app.db import engine
from app.main import purge_expired_messages
from app.models import Message, utcnow
from tests.test_chat import receive, start_chat


def settings(client, user):
    r = client.get("/api/settings", headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def patch(client, user, **changes):
    return client.patch("/api/settings", json=changes, headers=user["headers"])


def test_defaults(client, make_user):
    alice = make_user("alice")
    s = settings(client, alice)
    assert s["show_presence"] is True and s["send_typing"] is True
    assert s["disappear_seconds"] == 0 and s["blocked"] == []
    assert s["sign_in_method"] == "password" and s["username"] == alice["username"]
    assert client.get("/api/settings").status_code == 401


def test_hidden_presence(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    assert patch(client, bob, show_presence=False).json()["show_presence"] is False

    with client.websocket_connect("/ws", headers=alice["headers"]):
        with client.websocket_connect("/ws", headers=bob["headers"]):
            listed = client.get("/api/conversations", headers=alice["headers"]).json()
            seen_bob = next(m for c in listed if c["id"] == cid for m in c["members"] if m["id"] == bob["id"])
            assert seen_bob["online"] is False and seen_bob["last_seen_at"] is None


def test_typing_indicator_can_be_turned_off(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    patch(client, alice, send_typing=False)
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice, \
         client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        ws_alice.send_json({"type": "typing", "conversation_id": cid, "is_typing": True})
        ws_alice.send_json({"type": "message", "conversation_id": cid, "content": "no typing shown"})
        # Bob's next non-presence event is the message, not a typing event.
        event = ws_bob.receive_json()
        while event["type"] == "presence":
            event = ws_bob.receive_json()
        assert event["type"] == "message"


def test_disappearing_messages(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    assert patch(client, alice, disappear_seconds=123).status_code == 422
    assert patch(client, alice, disappear_seconds=3600).json()["disappear_seconds"] == 3600

    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": cid, "content": "secret"})
        sent = receive(ws, "message")["message"]
    assert sent["expires_at"] is not None

    history = client.get(f"/api/conversations/{cid}/messages", headers=bob["headers"]).json()
    assert [m["content"] for m in history] == ["secret"]

    # Pretend the hour has passed.
    with Session(engine) as session:
        message = session.get(Message, sent["id"])
        message.expires_at = utcnow() - timedelta(seconds=1)
        session.add(message)
        session.commit()

    assert client.get(f"/api/conversations/{cid}/messages", headers=bob["headers"]).json() == []
    listed = client.get("/api/conversations", headers=bob["headers"]).json()
    assert next(c for c in listed if c["id"] == cid)["last_message"] is None
    assert purge_expired_messages() >= 1
    with Session(engine) as session:
        assert session.get(Message, sent["id"]) is None


def test_blocking(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    s = client.post("/api/settings/blocked", json={"username": bob["username"]}, headers=alice["headers"]).json()
    assert [u["username"] for u in s["blocked"]] == [bob["username"]]
    listed = client.get("/api/conversations", headers=alice["headers"]).json()
    assert next(c for c in listed if c["id"] == cid)["blocked_by_me"] is True

    with client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        ws_bob.send_json({"type": "message", "conversation_id": cid, "content": "hello?", "client_id": "b1"})
        assert receive(ws_bob, "error")["detail"] == "You can't message this person."
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
        ws_alice.send_json({"type": "message", "conversation_id": cid, "content": "hi", "client_id": "a1"})
        assert "Unblock" in receive(ws_alice, "error")["detail"]
    assert client.get(f"/api/conversations/{cid}/messages", headers=alice["headers"]).json() == []

    s = client.delete(f"/api/settings/blocked/{bob['id']}", headers=alice["headers"]).json()
    assert s["blocked"] == []
    with client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        ws_bob.send_json({"type": "message", "conversation_id": cid, "content": "back"})
        assert receive(ws_bob, "message")["message"]["content"] == "back"

    assert client.post("/api/settings/blocked", json={"username": alice["username"]}, headers=alice["headers"]).status_code == 400


def test_change_username(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    new_name = f"renamed{alice['id']}"
    r = client.put("/api/settings/username", json={"username": new_name.upper()}, headers=alice["headers"])
    assert r.status_code == 200 and r.json()["username"] == new_name
    assert client.put("/api/settings/username", json={"username": bob["username"]}, headers=alice["headers"]).status_code == 409
    assert client.put("/api/settings/username", json={"username": "ai"}, headers=alice["headers"]).status_code != 200
    assert client.put("/api/settings/username", json={"username": "no spaces"}, headers=alice["headers"]).status_code == 422
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": new_name, "password": "secret123"}).status_code == 200
    client.cookies.clear()


def test_logout_everywhere(client, make_user):
    alice = make_user("alice")
    old = alice["headers"]
    r = client.post("/api/settings/logout-everywhere", headers=old)
    assert r.status_code == 200
    fresh = {"cookie": f"access_token={r.cookies['access_token']}"}
    client.cookies.clear()
    assert client.get("/api/auth/me", headers=old).status_code == 401
    assert client.get("/api/auth/me", headers=fresh).status_code == 200


def test_delete_account(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": cid, "content": "bye"})
        receive(ws, "message")

    wrong = client.post("/api/settings/delete-account", json={"confirm_username": "nope"}, headers=alice["headers"])
    assert wrong.status_code == 400
    r = client.post("/api/settings/delete-account", json={"confirm_username": alice["username"]}, headers=alice["headers"])
    assert r.status_code == 200
    client.cookies.clear()

    assert client.get("/api/auth/me", headers=alice["headers"]).status_code == 401
    assert client.get(f"/api/conversations/{cid}/messages", headers=bob["headers"]).json() == []
    with Session(engine) as session:
        assert session.exec(select(Message).where(Message.sender_id == alice["id"])).first() is None
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
