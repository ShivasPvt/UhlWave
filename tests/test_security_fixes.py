"""Fixes from the security review: login lockout, flood and @ai limits, no default
secret, real visitor address, presence vs blocks, group history before joining,
group photos, logout, socket origin, browser headers, PIN lockout persistence."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from tests.test_chat import receive, start_chat

ROOT = Path(__file__).resolve().parents[1]
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d763f8ffff3f0005fe02fea7d6a4b70000000049454e44ae426082"
)


def raw(sql, **params):
    from app.db import engine
    with engine.begin() as conn:
        return conn.execute(text(sql), params).all() if sql.lstrip().upper().startswith("SELECT") else conn.execute(text(sql), params)


def login(client, username, password="secret123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    client.cookies.clear()
    return r


@pytest.fixture
def tight(monkeypatch):
    """Shrink one of the in-memory limiters for a test."""
    from app import security

    def _tight(name, limit):
        limiter = security.Limiter(limit=limit, window=60)
        monkeypatch.setattr(security, name, limiter)
        return limiter
    return _tight


# ---------- 1. password guessing ----------

def test_login_pauses_after_too_many_wrong_passwords(client, make_user):
    u = make_user("guessme")
    for _ in range(10):
        assert login(client, u["username"], "wrong-pass-1").status_code == 401
    r = login(client, u["username"])            # even the right password waits now
    assert r.status_code == 429 and "Too many wrong passwords" in r.json()["detail"]
    assert int(r.headers["retry-after"]) > 0
    # Once the pause is over, the right password works and the count resets.
    raw("UPDATE users SET login_failed_at = :t WHERE username = :u", t="2000-01-01 00:00:00", u=u["username"])
    assert login(client, u["username"]).status_code == 200
    assert raw("SELECT login_failures FROM users WHERE username = :u", u=u["username"])[0][0] == 0


def test_login_limit_per_visitor_across_accounts(client, make_user, monkeypatch):
    from app import auth, security
    limiter = security.Limiter(limit=3, window=60)
    monkeypatch.setattr(auth, "login_ip_limiter", limiter)
    for i in range(3):
        assert login(client, f"nobody_{i}", "x").status_code == 401
    assert login(client, "nobody_9", "x").status_code == 429


# ---------- 2. flooding and the AI key ----------

def test_message_flood_is_refused(client, make_user, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "message_limiter", security.Limiter(limit=3, window=60))
    a, b = make_user("flood_a"), make_user("flood_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        for i in range(3):
            ws.send_json({"type": "message", "conversation_id": cid, "content": f"m{i}", "client_id": f"c{i}"})
            receive(ws, "message")
        ws.send_json({"type": "message", "conversation_id": cid, "content": "one too many", "client_id": "c9"})
        err = receive(ws, "error")
        assert "very fast" in err["detail"] and err["client_id"] == "c9"
    r = client.post(f"/api/conversations/{cid}/sticker", json={"kind": "sticker", "emoji": "🔥"}, headers=a["headers"])
    assert r.status_code == 429


def test_ai_answers_are_limited_per_person(client, make_user, monkeypatch):
    from app import security
    monkeypatch.setattr(security, "ai_limiter", security.Limiter(limit=1, window=3600))
    a, b = make_user("ai_lim_a"), make_user("ai_lim_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": cid, "content": "@ai hello", "client_id": "q1"})
        receive(ws, "message")                       # my message
        bot = None
        for _ in range(20):                          # the bot answers (no key in tests: a reminder)
            e = ws.receive_json()
            if e["type"] == "message" and e["message"]["sender_is_bot"]:
                bot = e
                break
        assert bot is not None
        ws.send_json({"type": "message", "conversation_id": cid, "content": "@ai again", "client_id": "q2"})
        receive(ws, "message")
        err = receive(ws, "error")
        assert "times this hour" in err["detail"]


# ---------- 3. no built-in login secret ----------

def test_no_default_jwt_secret():
    from app import config
    assert config.JWT_SECRET != "dev-only-secret-change-me-before-deploying" and len(config.JWT_SECRET) >= 32
    env = {**os.environ, "COOKIE_SECURE": "true", "JWT_SECRET": "replace-me-with-a-long-random-string"}
    r = subprocess.run([sys.executable, "-c", "import app.config"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "JWT_SECRET is missing or too short" in r.stderr


def test_forged_token_with_old_default_secret_is_refused(client, make_user):
    import jwt
    u = make_user("forgery")
    forged = jwt.encode({"sub": str(u["id"]), "v": 0}, "dev-only-secret-change-me-before-deploying", algorithm="HS256")
    assert client.get("/api/auth/me", headers={"cookie": f"access_token={forged}"}).status_code == 401


# ---------- 4. the real visitor address ----------

def test_client_ip_uses_the_proxys_entry_only(monkeypatch):
    from types import SimpleNamespace
    from app import security
    conn = SimpleNamespace(headers={"x-forwarded-for": "6.6.6.6, 1.2.3.4"}, client=SimpleNamespace(host="10.0.0.9"))
    monkeypatch.setattr(security, "TRUST_PROXY", True)
    assert security.client_ip(conn) == "1.2.3.4"     # the made-up first entry is ignored
    monkeypatch.setattr(security, "TRUST_PROXY", False)
    assert security.client_ip(conn) == "10.0.0.9"    # no proxy: the header isn't trusted at all


# ---------- 5. presence vs blocks ----------

def test_blocked_people_cant_see_when_you_are_online(client, make_user):
    a, m = make_user("pres_a"), make_user("pres_m")
    assert client.post("/api/settings/blocked", json={"username": m["username"]}, headers=a["headers"]).status_code == 200
    with client.websocket_connect("/ws", headers=a["headers"]):
        pass
    seen = [u for u in client.get(f"/api/users?q={a['username']}", headers=m["headers"]).json() if u["username"] == a["username"]][0]
    assert seen["last_seen_at"] is None and seen["online"] is False
    prof = client.get(f"/api/users/{a['username']}/profile", headers=m["headers"]).json()
    assert prof["last_seen_at"] is None
    with client.websocket_connect("/ws", headers=m["headers"]) as wm:
        with client.websocket_connect("/ws", headers=a["headers"]):
            pass
        # a's presence events never reach m; the next thing m gets is its own test ping.
        start_chat(client, m, make_user("pres_x")["username"])
        client.post("/api/requests", json={"username": "pres_none_such"}, headers=m["headers"])
        wm.send_json({"type": "typing", "conversation_id": 0, "is_typing": True})
        wm.send_json({"type": "nope"})
        event = wm.receive_json()
        assert event["type"] == "error" and "Unknown event" in event["detail"]


# ---------- 6. group history before joining ----------

def test_people_added_later_dont_see_earlier_group_messages(client, make_user):
    a, b, late = make_user("hist_a"), make_user("hist_b"), make_user("hist_late")
    g = client.post("/api/groups", json={"name": "Wing", "usernames": [b["username"]]}, headers=a["headers"]).json()
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": g["id"], "content": "the wifi password is kamand123", "client_id": "w1"})
        old_id = receive(ws, "message")["message"]["id"]
    up = client.post(f"/api/conversations/{g['id']}/attachments?filename=plan.png", content=PNG,
                     headers={**a["headers"], "content-type": "image/png"}).json()

    assert client.post(f"/api/groups/{g['id']}/members", json={"username": late["username"]}, headers=b["headers"]).status_code == 200
    hist = client.get(f"/api/conversations/{g['id']}/messages", headers=late["headers"]).json()
    assert not any("wifi" in m["content"] for m in hist)
    assert any("added" in m["content"] for m in hist)            # the "b added late" line is visible
    assert client.get(up["attachment"]["url"], headers=late["headers"]).status_code == 404
    assert client.get(f"/api/conversations/{g['id']}/search?q=wifi", headers=late["headers"]).json() == []
    assert client.get("/api/search/messages?q=wifi", headers=late["headers"]).json() == []
    assert client.post(f"/api/messages/{old_id}/reactions", json={"emoji": "👍"}, headers=late["headers"]).status_code == 404
    backup = client.get("/api/backup", headers=late["headers"]).json()
    assert "wifi" not in str(backup)
    # Older members still see everything.
    assert any("wifi" in m["content"] for m in client.get(f"/api/conversations/{g['id']}/messages", headers=b["headers"]).json())


# ---------- 7. group photos ----------

def test_group_photos_are_for_members_or_invite_holders(client, make_user):
    owner, outsider = make_user("gp_owner"), make_user("gp_out")
    g = client.post("/api/groups", json={"name": "Club", "usernames": []}, headers=owner["headers"]).json()
    url = client.post(f"/api/groups/{g['id']}/photo", content=PNG, headers={**owner["headers"], "content-type": "image/png"}).json()["avatar_url"]
    assert client.get(url, headers=owner["headers"]).status_code == 200
    assert client.get(url, headers=outsider["headers"]).status_code == 404
    code = client.post(f"/api/groups/{g['id']}/invite", headers=owner["headers"]).json()["invite_code"]
    preview = client.get(f"/api/invites/{code}", headers=outsider["headers"]).json()
    assert client.get(preview["avatar_url"], headers=outsider["headers"]).status_code == 200
    assert client.get(f"{url}?invite=wrong-code", headers=outsider["headers"]).status_code == 404


# ---------- hardening ----------

def test_logout_cancels_the_cookie_everywhere(client, make_user):
    u = make_user("bye")
    copy = dict(u["headers"])
    assert client.get("/api/auth/me", headers=copy).status_code == 200
    client.post("/api/auth/logout", headers=u["headers"])
    client.cookies.clear()
    assert client.get("/api/auth/me", headers=copy).status_code == 401


def test_unlock_cookie_is_not_a_login(client, make_user):
    from app.db import engine
    from app.folders import unlock_token
    from app.models import User
    from sqlmodel import Session
    u = make_user("unlockonly")
    with Session(engine) as session:
        token = unlock_token(session.get(User, u["id"]))
    assert client.get("/api/auth/me", headers={"cookie": f"access_token={token}"}).status_code == 401


def test_socket_refuses_other_websites(client, make_user):
    u = make_user("origin")
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ws", headers={**u["headers"], "origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert closed.value.code == 4403
    with client.websocket_connect("/ws", headers={**u["headers"], "origin": "http://testserver"}) as ws:
        ws.send_json({"type": "nope"})
        assert ws.receive_json()["type"] == "error"


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "same-origin"


def test_pin_failures_survive_a_restart(client, make_user):
    u = make_user("pinsave")
    assert client.put("/api/locked/pin", json={"pin": "2468"}, headers=u["headers"]).status_code == 200
    for _ in range(2):
        assert client.post("/api/locked/open", json={"pin": "0000"}, headers=u["headers"]).status_code == 403
    assert raw("SELECT pin_failures FROM users WHERE id = :i", i=u["id"])[0][0] == 2   # stored, not in memory
    assert client.post("/api/locked/open", json={"pin": "2468"}, headers=u["headers"]).status_code == 200
    assert raw("SELECT pin_failures FROM users WHERE id = :i", i=u["id"])[0][0] == 0
