"""Encrypted storage, secret chats, password rules, replies and polls."""
import json

from sqlalchemy import text
from sqlmodel import Session

from tests.test_chat import receive, start_chat
from tests.test_features import PNG, history, make_group, send

PUBLIC = json.dumps({"kty": "EC", "crv": "P-256", "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU", "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"})
PUBLIC2 = json.dumps({"kty": "EC", "crv": "P-256", "x": "gI0GAILBdu7T53akrFmMyGcsF3n5dO7MmwNBHKW5SV0", "y": "SLW_xSffzlPWrHEVI30DHM_4egVwt3NQqeUD7nMFpps"})
SEALED = json.dumps({"salt": "c2FsdA==", "iv": "aXY=", "ct": "Y2lwaGVydGV4dA=="})


def raw(sql, **params):
    from app.db import engine
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


# ---------- encrypted storage ----------

def test_messages_and_files_are_encrypted_in_the_database(client, make_user):
    a, b = make_user("enc_a"), make_user("enc_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        sent = send(ws, cid, "the vault code is 4821")
    up = client.post(f"/api/conversations/{cid}/attachments?filename=plans.png", content=PNG, headers={**a["headers"], "Content-Type": "image/png"}).json()

    stored = raw("SELECT content FROM messages WHERE id = :id", id=sent["id"])[0][0]
    assert stored.startswith("enc1:") and "vault" not in stored
    name, data = raw("SELECT filename, data FROM attachments WHERE id = :id", id=up["attachment"]["id"])[0]
    assert name.startswith("enc1:") and "plans" not in name
    assert bytes(data).startswith(b"enc1") and PNG not in bytes(data)

    # The app still reads everything normally.
    assert history(client, b, cid)[0]["content"] == "the vault code is 4821"
    assert client.get(up["attachment"]["url"], headers=b["headers"]).content == PNG
    assert client.get("/api/search/messages?q=VAULT", headers=b["headers"]).json()[0]["id"] == sent["id"]


def test_old_plain_rows_get_encrypted(client, make_user):
    from app.db import encrypt_old_rows, engine

    a, b = make_user("old_a"), make_user("old_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        mid = send(ws, cid, "placeholder")["id"]
    with engine.begin() as conn:  # pretend this row was saved by the old version
        conn.execute(text("UPDATE messages SET content = 'written before encryption' WHERE id = :id"), {"id": mid})
    assert history(client, b, cid)[0]["content"] == "written before encryption"  # plain rows still read fine
    assert encrypt_old_rows() >= 1
    assert raw("SELECT content FROM messages WHERE id = :id", id=mid)[0][0].startswith("enc1:")
    assert history(client, b, cid)[0]["content"] == "written before encryption"
    assert encrypt_old_rows() == 0  # nothing left to do


# ---------- password rules ----------

def test_password_rules(client):
    def register(name, pw):
        r = client.post("/api/auth/register", json={"username": name, "password": pw})
        client.cookies.clear()
        return r

    assert register("pw_short", "abc12").status_code == 422
    assert "letters and numbers" in register("pw_letters", "onlyletters").json()["detail"]
    assert "username" in register("pw_riya", "pw_riya2024x").json()["detail"]
    assert "too easy" in register("pw_common", "password123").json()["detail"]
    assert "too easy" in register("pw_repeat", "1111111a").json()["detail"]
    assert register("pw_good", "Hostel4-chess").status_code == 201


# ---------- replies ----------

def test_replies(client, make_user):
    a, b = make_user("rep_a"), make_user("rep_b")
    cid = start_chat(client, a, b["username"])["id"]
    other = start_chat(client, a, "ai")["id"]
    with client.websocket_connect("/ws", headers=b["headers"]) as ws_b, client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
        first = send(ws_a, cid, "lunch at 1?")
        receive(ws_b, "message")  # b gets a's message first
        ws_b.send_json({"type": "message", "conversation_id": cid, "content": "yes!", "reply_to_id": first["id"]})
        reply = receive(ws_b, "message")["message"]
        assert reply["reply_to"]["id"] == first["id"] and reply["reply_to"]["content"] == "lunch at 1?"
        # A reply can't point into a different chat.
        ws_a.send_json({"type": "message", "conversation_id": other, "content": "x", "reply_to_id": first["id"]})
        receive(ws_a, "message")  # a sees b's reply first
        stray = receive(ws_a, "message")["message"]
        assert stray["conversation_id"] == other and stray["reply_to"] is None
    # Deleting the original leaves the reply with a "deleted" quote.
    client.delete(f"/api/messages/{first['id']}", headers=a["headers"])
    quoted = [m for m in history(client, b, cid) if m["content"] == "yes!"][0]["reply_to"]
    assert quoted["deleted"] is True and quoted["content"] == ""


# ---------- polls ----------

def test_polls(client, make_user):
    a, b, c = make_user("poll_a"), make_user("poll_b"), make_user("poll_c")
    gid = make_group(client, a, "Trek", b, c)["id"]
    url = f"/api/conversations/{gid}/polls"
    assert client.post(url, json={"question": "When?", "options": ["Sat"]}, headers=a["headers"]).status_code == 422
    assert client.post(url, json={"question": "When?", "options": ["Sat", "sat"]}, headers=a["headers"]).status_code == 422

    with client.websocket_connect("/ws", headers=c["headers"]) as ws_c:
        poll_msg = client.post(url, json={"question": "When shall we go?", "options": ["Saturday", "Sunday", "Both"]}, headers=a["headers"]).json()
        assert receive(ws_c, "message")["message"]["type"] == "poll"
        poll = poll_msg["poll"]
        sat, sun, both = [o["id"] for o in poll["options"]]
        assert raw("SELECT text FROM poll_options WHERE id = :id", id=sat)[0][0].startswith("enc1:")

        assert client.post(f"/api/polls/{poll['id']}/vote", json={"option_ids": [sat, sun]}, headers=b["headers"]).status_code == 400
        out = client.post(f"/api/polls/{poll['id']}/vote", json={"option_ids": [sun]}, headers=b["headers"]).json()
        assert out["poll"]["options"][1]["voters"] == [b["id"]]
        live = receive(ws_c, "message_updated")["message"]
        assert live["poll"]["voter_count"] == 1
        client.post(f"/api/polls/{poll['id']}/vote", json={"option_ids": [sat]}, headers=b["headers"])  # change vote
        out = client.post(f"/api/polls/{poll['id']}/vote", json={"option_ids": [sat]}, headers=c["headers"]).json()
        assert [len(o["voters"]) for o in out["poll"]["options"]] == [2, 0, 0]
        out = client.post(f"/api/polls/{poll['id']}/vote", json={"option_ids": []}, headers=c["headers"]).json()  # take it back
        assert out["poll"]["voter_count"] == 1

    assert client.post(f"/api/polls/{poll['id']}/close", headers=b["headers"]).status_code == 403
    assert client.post(f"/api/polls/{poll['id']}/close", headers=a["headers"]).json()["poll"]["closed"] is True
    assert client.post(f"/api/polls/{poll['id']}/vote", json={"option_ids": [both]}, headers=c["headers"]).status_code == 400

    multi = client.post(url, json={"question": "Bring?", "options": ["Tea", "Snacks"], "multiple": True}, headers=a["headers"]).json()["poll"]
    out = client.post(f"/api/polls/{multi['id']}/vote", json={"option_ids": [o["id"] for o in multi["options"]]}, headers=c["headers"]).json()
    assert [len(o["voters"]) for o in out["poll"]["options"]] == [1, 1]
    outsider = make_user("poll_d")
    assert client.post(f"/api/polls/{multi['id']}/vote", json={"option_ids": []}, headers=outsider["headers"]).status_code == 404

    # Deleting the account that made the polls cleans them up.
    assert client.post("/api/settings/delete-account", json={"confirm_username": a["username"]}, headers=a["headers"]).status_code == 200
    client.cookies.clear()
    assert all(m["type"] != "poll" for m in history(client, b, gid))


# ---------- secret chats ----------

def setup_keys(client, user, public=PUBLIC):
    r = client.put("/api/e2ee", json={"public_key": public, "private_wrapped": SEALED}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def test_e2ee_keys_validation(client, make_user):
    a = make_user("key_a")
    assert client.get("/api/e2ee", headers=a["headers"]).json()["public_key"] is None
    leaked = json.dumps({**json.loads(PUBLIC), "d": "secret"})
    assert client.put("/api/e2ee", json={"public_key": leaked, "private_wrapped": SEALED}, headers=a["headers"]).status_code == 422
    assert client.put("/api/e2ee", json={"public_key": PUBLIC, "private_wrapped": '{"salt": "x"}'}, headers=a["headers"]).status_code == 422
    setup_keys(client, a)
    assert client.get("/api/e2ee", headers=a["headers"]).json()["private_wrapped"] == SEALED


def test_secret_chats(client, make_user):
    a, b, c = make_user("sec_a"), make_user("sec_b"), make_user("sec_c")
    url = "/api/secret-chats"
    assert client.post(url, json={"username": b["username"]}, headers=a["headers"]).status_code == 409  # a has no keys
    setup_keys(client, a)
    r = client.post(url, json={"username": b["username"]}, headers=a["headers"])
    assert r.status_code == 409 and "hasn't set up" in r.json()["detail"]
    setup_keys(client, b, PUBLIC2)
    chat = client.post(url, json={"username": b["username"]}, headers=a["headers"]).json()
    assert chat["type"] == "secret"
    assert {m["username"]: m["public_key"] for m in chat["members"]} == {a["username"]: PUBLIC, b["username"]: PUBLIC2}
    assert client.post(url, json={"username": a["username"]}, headers=b["headers"]).json()["id"] == chat["id"]
    assert client.post(url, json={"username": "ai"}, headers=a["headers"]).status_code == 400
    cid = chat["id"]

    ciphertext = "e2e1:" + "QUJD" * 40
    with client.websocket_connect("/ws", headers=b["headers"]) as ws_b, client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
        ws_a.send_json({"type": "message", "conversation_id": cid, "content": "plain text @ai hello", "client_id": "t1"})
        assert "end-to-end encrypted" in receive(ws_a, "error")["detail"]
        ws_a.send_json({"type": "message", "conversation_id": cid, "content": ciphertext + " @ai"})
        got = receive(ws_b, "message")["message"]
        assert got["content"].startswith("e2e1:")
        ws_a.send_json({"type": "message", "conversation_id": cid, "content": "/imagine a cat"})
        receive(ws_a, "error")
    # No AI reply, no imagine.
    assert all(not m["sender_is_bot"] for m in history(client, a, cid))

    # Anything the server would have to read is refused.
    hdr = {**a["headers"], "Content-Type": "image/png"}
    assert client.post(f"/api/conversations/{cid}/attachments?filename=x.png", content=PNG, headers=hdr).status_code == 400
    assert client.post(f"/api/conversations/{cid}/location", json={"latitude": 1, "longitude": 2}, headers=a["headers"]).status_code == 400
    assert client.post(f"/api/conversations/{cid}/sticker", json={"kind": "sticker", "emoji": "🔥"}, headers=a["headers"]).status_code == 400
    assert client.post(f"/api/conversations/{cid}/polls", json={"question": "q", "options": ["a", "b"]}, headers=a["headers"]).status_code == 400
    assert client.get(f"/api/conversations/{cid}/search?q=abc", headers=a["headers"]).status_code == 400
    mid = got["id"]
    assert client.patch(f"/api/messages/{mid}", json={"content": "plain edit"}, headers=a["headers"]).status_code == 400
    assert client.patch(f"/api/messages/{mid}", json={"content": "e2e1:RURJVA=="}, headers=a["headers"]).status_code == 200
    # Outsiders can't see it; backups don't pretend to read it.
    assert client.get(f"/api/conversations/{cid}/messages", headers=c["headers"]).status_code == 404
    backup = client.get("/api/backup", headers=a["headers"]).json()
    secret = next(x for x in backup["chats"] if x["id"] == cid)
    assert any("end-to-end encrypted" in m["text"] for m in secret["messages"])

    # Resetting keys warns the partner in the chat.
    setup_keys(client, a, PUBLIC2.replace("gI0G", "hI0G"))
    assert history(client, b, cid)[-1]["content"] == f"{a['username']}'s security code changed"


def test_secret_chat_calls_are_allowed(client, make_user):
    a, b = make_user("secc_a"), make_user("secc_b")
    setup_keys(client, a)
    setup_keys(client, b, PUBLIC2)
    cid = client.post("/api/secret-chats", json={"username": b["username"]}, headers=a["headers"]).json()["id"]
    with client.websocket_connect("/ws", headers=b["headers"]) as ws_b, client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
        ws_a.send_json({"type": "call_invite", "call_id": "call-secret-01", "conversation_id": cid, "video": False})
        assert receive(ws_b, "call_incoming")["conversation_id"] == cid
        ws_a.send_json({"type": "call_end", "call_id": "call-secret-01"})
        receive(ws_b, "call_ended")
