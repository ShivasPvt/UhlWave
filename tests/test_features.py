"""Group chats, read receipts, attachments, edit/delete, reactions and search."""
from tests.test_chat import receive, start_chat

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_group(client, owner, name, *people):
    r = client.post("/api/groups", json={"name": name, "usernames": [p["username"] for p in people]}, headers=owner["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def history(client, user, cid):
    r = client.get(f"/api/conversations/{cid}/messages", headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def send(ws, cid, text):
    ws.send_json({"type": "message", "conversation_id": cid, "content": text})
    return receive(ws, "message")["message"]


# ---------- groups ----------

def test_group_lifecycle(client, make_user):
    alice, bob, cara, dan = make_user("alice"), make_user("bob"), make_user("cara"), make_user("dan")
    group = make_group(client, alice, "Study squad", bob, cara)
    gid = group["id"]
    assert group["type"] == "group" and group["title"] == "Study squad"
    assert {m["id"] for m in group["members"]} == {alice["id"], bob["id"], cara["id"]}
    assert group["created_by"] == alice["id"]
    assert "created the group" in history(client, bob, gid)[0]["content"]

    # Everyone in the group gets messages in real time.
    with client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob, \
         client.websocket_connect("/ws", headers=cara["headers"]) as ws_cara, \
         client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
        send(ws_alice, gid, "hello squad")
        assert receive(ws_bob, "message")["message"]["content"] == "hello squad"
        assert receive(ws_cara, "message")["message"]["content"] == "hello squad"

    # Outsiders can't see it.
    assert client.get(f"/api/conversations/{gid}/messages", headers=dan["headers"]).status_code == 404

    # Anyone can add and rename; only the admin can remove others.
    assert client.post(f"/api/groups/{gid}/members", json={"username": dan["username"]}, headers=bob["headers"]).status_code == 200
    assert client.post(f"/api/groups/{gid}/members", json={"username": dan["username"]}, headers=bob["headers"]).status_code == 409
    assert history(client, dan, gid)[-1]["content"] == f"{bob['username']} added {dan['username']}"
    assert client.patch(f"/api/groups/{gid}", json={"name": "DBMS crew"}, headers=cara["headers"]).json()["title"] == "DBMS crew"
    assert client.delete(f"/api/groups/{gid}/members/{dan['id']}", headers=bob["headers"]).status_code == 403
    assert client.delete(f"/api/groups/{gid}/members/{dan['id']}", headers=alice["headers"]).status_code == 200
    assert client.get(f"/api/conversations/{gid}/messages", headers=dan["headers"]).status_code == 404

    # Leaving.
    assert client.delete(f"/api/groups/{gid}/members/{cara['id']}", headers=cara["headers"]).status_code == 200
    assert history(client, alice, gid)[-1]["content"] == f"{cara['username']} left"
    listed = client.get("/api/conversations", headers=cara["headers"]).json()
    assert gid not in [c["id"] for c in listed]


def test_group_validation(client, make_user):
    alice = make_user("alice")
    assert client.post("/api/groups", json={"name": "  ", "usernames": []}, headers=alice["headers"]).status_code == 422
    assert client.post("/api/groups", json={"name": "x", "usernames": ["nobody_here"]}, headers=alice["headers"]).status_code == 404
    solo = make_group(client, alice, "Just me")
    assert len(solo["members"]) == 1


def test_blocking_does_not_silence_groups(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    gid = make_group(client, alice, "Both of us", bob)["id"]
    client.post("/api/settings/blocked", json={"username": bob["username"]}, headers=alice["headers"])
    with client.websocket_connect("/ws", headers=bob["headers"]) as ws:
        assert send(ws, gid, "still here")["content"] == "still here"


# ---------- read receipts ----------

def test_read_receipts_and_unread_counts(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice, \
         client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        first = send(ws_alice, cid, "one")
        second = send(ws_alice, cid, "two")
        receive(ws_bob, "message"); receive(ws_bob, "message")

        listed = client.get("/api/conversations", headers=bob["headers"]).json()
        assert next(c for c in listed if c["id"] == cid)["unread"] == 2

        ws_bob.send_json({"type": "read", "conversation_id": cid, "message_id": second["id"]})
        seen = receive(ws_alice, "read")
        assert seen == {"type": "read", "conversation_id": cid, "user_id": bob["id"], "message_id": second["id"]}
        # Going backwards is ignored.
        ws_bob.send_json({"type": "read", "conversation_id": cid, "message_id": first["id"]})

    listed = client.get("/api/conversations", headers=bob["headers"]).json()
    assert next(c for c in listed if c["id"] == cid)["unread"] == 0
    listed = client.get("/api/conversations", headers=alice["headers"]).json()
    assert next(c for c in listed if c["id"] == cid)["read_state"] == {str(bob["id"]): second["id"]}


def test_read_receipts_off_works_both_ways(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    client.patch("/api/settings", json={"send_read_receipts": False}, headers=bob["headers"])
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
        msg = send(ws_alice, cid, "did you see this?")
        with client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
            ws_bob.send_json({"type": "read", "conversation_id": cid, "message_id": msg["id"]})
            send(ws_bob, cid, "maybe")  # proves the read was processed first
    alice_view = next(c for c in client.get("/api/conversations", headers=alice["headers"]).json() if c["id"] == cid)
    bob_view = next(c for c in client.get("/api/conversations", headers=bob["headers"]).json() if c["id"] == cid)
    assert alice_view["read_state"] == {}  # Bob's receipts are hidden
    assert bob_view["read_state"] == {}    # and Bob doesn't see Alice's
    assert bob_view["unread"] == 0         # but Bob's own unread count still clears


# ---------- attachments ----------

def upload(client, user, cid, data, content_type, filename, kind="auto"):
    return client.post(
        f"/api/conversations/{cid}/attachments?filename={filename}&kind={kind}",
        content=data, headers={**user["headers"], "content-type": content_type},
    )


def test_attachments(client, make_user):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    cid = start_chat(client, alice, bob["username"])["id"]

    image = upload(client, alice, cid, PNG, "image/png", "cat.png")
    assert image.status_code == 201, image.text
    image = image.json()
    assert image["type"] == "image" and image["attachment"]["filename"] == "cat.png"
    got = client.get(image["attachment"]["url"], headers=bob["headers"])
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"
    assert "inline" in got.headers["content-disposition"]
    assert client.get(image["attachment"]["url"], headers=eve["headers"]).status_code == 404

    voice = upload(client, alice, cid, b"OggS" + b"\x00" * 32, "audio/webm;codecs=opus", "voice.webm", kind="voice").json()
    assert voice["type"] == "voice"
    assert upload(client, alice, cid, b"hello", "text/plain", "x.txt", kind="voice").status_code == 400

    # Anything else is a download, never rendered: SVG and HTML can carry scripts.
    svg = upload(client, alice, cid, b"<svg onload=alert(1)>", "image/svg+xml", "../../evil.svg").json()
    assert svg["type"] == "file" and svg["attachment"]["filename"] == "evil.svg"
    got = client.get(svg["attachment"]["url"], headers=bob["headers"])
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["content-disposition"].startswith("attachment")

    assert upload(client, alice, cid, b"x" * (5 * 1024 * 1024 + 1), "application/pdf", "big.pdf").status_code == 413
    assert upload(client, alice, cid, b"", "application/pdf", "empty.pdf").status_code == 400
    assert upload(client, eve, cid, PNG, "image/png", "sneaky.png").status_code == 404

    # Deleting the message deletes the file too.
    assert client.delete(f"/api/messages/{image['id']}", headers=alice["headers"]).status_code == 200
    assert client.get(image["attachment"]["url"], headers=bob["headers"]).status_code == 404


# ---------- edit, delete, react ----------

def test_edit_and_delete(client, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice, \
         client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        msg = send(ws_alice, cid, "helo")
        receive(ws_bob, "message")

        assert client.patch(f"/api/messages/{msg['id']}", json={"content": "hacked"}, headers=bob["headers"]).status_code == 403
        edited = client.patch(f"/api/messages/{msg['id']}", json={"content": "hello"}, headers=alice["headers"]).json()
        assert edited["content"] == "hello" and edited["edited_at"]
        update = receive(ws_bob, "message_updated")["message"]
        assert update["id"] == msg["id"] and update["content"] == "hello"

        assert client.delete(f"/api/messages/{msg['id']}", headers=bob["headers"]).status_code == 403
        gone = client.delete(f"/api/messages/{msg['id']}", headers=alice["headers"]).json()
        assert gone["deleted"] is True and gone["content"] == ""
        assert receive(ws_bob, "message_updated")["message"]["deleted"] is True
    assert history(client, bob, cid)[0]["deleted"] is True
    assert client.patch(f"/api/messages/{msg['id']}", json={"content": "back"}, headers=alice["headers"]).status_code == 400


def test_reactions(client, make_user):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    cid = start_chat(client, alice, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        msg = send(ws, cid, "exam moved to friday")

    url = f"/api/messages/{msg['id']}/reactions"
    r = client.post(url, json={"emoji": "😮"}, headers=bob["headers"]).json()
    assert r["reactions"] == [{"emoji": "😮", "user_ids": [bob["id"]]}]
    r = client.post(url, json={"emoji": "😮"}, headers=alice["headers"]).json()
    assert r["reactions"] == [{"emoji": "😮", "user_ids": [bob["id"], alice["id"]]}]
    r = client.post(url, json={"emoji": "👍"}, headers=bob["headers"]).json()  # switch
    assert sorted((x["emoji"], x["user_ids"]) for x in r["reactions"]) == [("👍", [bob["id"]]), ("😮", [alice["id"]])]
    r = client.post(url, json={"emoji": "👍"}, headers=bob["headers"]).json()  # take back
    assert r["reactions"] == [{"emoji": "😮", "user_ids": [alice["id"]]}]

    assert client.post(url, json={"emoji": "🍕"}, headers=bob["headers"]).status_code == 400
    assert client.post(url, json={"emoji": "👍"}, headers=eve["headers"]).status_code == 404
    assert history(client, bob, cid)[0]["reactions"] == [{"emoji": "😮", "user_ids": [alice["id"]]}]


# ---------- search ----------

def test_search(client, make_user):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    cid = start_chat(client, alice, bob["username"])["id"]
    other = start_chat(client, eve, bob["username"])["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws:
        send(ws, cid, "Library closes at 10pm_zq")
        send(ws, cid, "unrelated")
    with client.websocket_connect("/ws", headers=eve["headers"]) as ws:
        send(ws, other, "eve says library_zq too")

    results = client.get("/api/search/messages?q=LIBRARY", headers=alice["headers"]).json()
    assert [r["content"] for r in results] == ["Library closes at 10pm_zq"]
    assert results[0]["conversation_title"] == bob["username"]
    both = client.get("/api/search/messages?q=library", headers=bob["headers"]).json()
    assert {r["conversation_id"] for r in both} == {cid, other}
    # % and _ are searched literally, not as wildcards.
    assert [r["content"] for r in client.get("/api/search/messages?q=pm_zq", headers=bob["headers"]).json()] == ["Library closes at 10pm_zq"]
    assert client.get("/api/search/messages?q=a", headers=alice["headers"]).status_code == 422
