"""Private accounts, folders and locked chats, pins, in-chat search and media,
group admins/tags/invites, profiles, GIFs and stickers, /imagine,
calls, appearance, backups."""
import asyncio
import json

import pytest

from tests.test_chat import receive, start_chat
from tests.test_features import PNG, history, make_group, send


def patch_user(client, user, **changes):
    r = client.patch("/api/settings", json=changes, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def conv(client, user, cid, cookies=None):
    listed = client.get("/api/conversations", headers=user["headers"]).json()
    return next((c for c in listed if c["id"] == cid), None)


# ---------- private accounts ----------

def test_private_account_needs_a_request(client, make_user):
    riya, arjun, kabir = make_user("riya"), make_user("arjun"), make_user("kabir")
    assert patch_user(client, riya, is_private=True)["is_private"] is True

    # Direct chat is refused; a request is made instead.
    r = client.post("/api/conversations", json={"username": riya["username"]}, headers=arjun["headers"])
    assert r.status_code == 403 and "private account" in r.json()["detail"]
    r = client.post("/api/requests", json={"username": riya["username"], "note": "DBMS notes?"}, headers=arjun["headers"]).json()
    assert r["status"] == "pending" and r["request"]["note"] == "DBMS notes?"
    again = client.post("/api/requests", json={"username": riya["username"]}, headers=arjun["headers"]).json()
    assert again["request"]["id"] == r["request"]["id"]  # no duplicates

    # Private people can't be added to groups by strangers.
    r2 = client.post("/api/groups", json={"name": "x", "usernames": [riya["username"]]}, headers=arjun["headers"])
    assert r2.status_code == 403

    with client.websocket_connect("/ws", headers=arjun["headers"]) as ws_arjun:
        incoming = client.get("/api/requests", headers=riya["headers"]).json()["incoming"]
        assert [i["user"]["username"] for i in incoming] == [arjun["username"]]
        accepted = client.post(f"/api/requests/{incoming[0]['id']}/accept", headers=riya["headers"]).json()
        assert accepted["status"] == "chat"
        assert receive(ws_arjun, "requests_changed")
    # Now they can chat, and the old route works too.
    again = client.post("/api/conversations", json={"username": riya["username"]}, headers=arjun["headers"])
    assert again.status_code == 200 and again.json()["id"] == accepted["conversation"]["id"]
    assert client.get("/api/requests", headers=riya["headers"]).json()["incoming"] == []

    # Decline: request vanishes, and asking again straight away is refused.
    r = client.post("/api/requests", json={"username": riya["username"]}, headers=kabir["headers"]).json()
    assert client.post(f"/api/requests/{r['request']['id']}/decline", headers=riya["headers"]).status_code == 200
    assert client.get("/api/requests", headers=kabir["headers"]).json()["outgoing"] == []
    assert client.post("/api/requests", json={"username": riya["username"]}, headers=kabir["headers"]).status_code == 429


def test_cancel_request_and_mutual_request(client, make_user):
    a, b = make_user("prv_a"), make_user("prv_b")
    patch_user(client, a, is_private=True)
    patch_user(client, b, is_private=True)
    r = client.post("/api/requests", json={"username": b["username"]}, headers=a["headers"]).json()
    assert client.delete(f"/api/requests/{r['request']['id']}", headers=a["headers"]).status_code == 200
    client.post("/api/requests", json={"username": b["username"]}, headers=a["headers"])
    # b asking a back counts as a yes.
    r = client.post("/api/requests", json={"username": a["username"]}, headers=b["headers"]).json()
    assert r["status"] == "chat"


def test_public_accounts_and_ai_skip_requests(client, make_user):
    a, b = make_user("pub_a"), make_user("pub_b")
    assert client.post("/api/requests", json={"username": b["username"]}, headers=a["headers"]).json()["status"] == "chat"
    assert client.post("/api/requests", json={"username": "ai"}, headers=a["headers"]).json()["status"] == "chat"


# ---------- archive, categories, locked chats ----------

def test_archive_and_categories(client, make_user):
    a, b = make_user("fold_a"), make_user("fold_b")
    cid = start_chat(client, a, b["username"])["id"]
    r = client.patch(f"/api/conversations/{cid}/prefs", json={"archived": True}, headers=a["headers"])
    assert r.json()["archived"] is True
    assert conv(client, a, cid)["archived"] is True
    assert conv(client, b, cid)["archived"] is False  # only for me

    cat = client.post("/api/categories", json={"name": "Hostel"}, headers=a["headers"]).json()
    assert client.post("/api/categories", json={"name": "hostel"}, headers=a["headers"]).status_code == 409
    client.patch(f"/api/conversations/{cid}/prefs", json={"category_id": cat["id"]}, headers=a["headers"])
    assert conv(client, a, cid)["category_id"] == cat["id"]
    # Someone else's category can't be used.
    other_cat = client.post("/api/categories", json={"name": "Mine"}, headers=b["headers"]).json()
    assert client.patch(f"/api/conversations/{cid}/prefs", json={"category_id": other_cat["id"]}, headers=a["headers"]).status_code == 404
    assert client.patch(f"/api/categories/{cat['id']}", json={"name": "Hostel 4"}, headers=a["headers"]).json()["name"] == "Hostel 4"
    assert client.delete(f"/api/categories/{cat['id']}", headers=a["headers"]).status_code == 200
    assert conv(client, a, cid)["category_id"] is None


def test_locked_chats(client, make_user):
    a, b = make_user("lock_a"), make_user("lock_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        send(ws, cid, "secret plans")

    # Need a PIN first.
    assert client.post(f"/api/conversations/{cid}/lock", json={"pin": "1234"}, headers=a["headers"]).status_code == 400
    assert client.put("/api/locked/pin", json={"pin": "12"}, headers=a["headers"]).status_code == 422
    r = client.put("/api/locked/pin", json={"pin": "4821"}, headers=a["headers"])
    assert r.status_code == 200
    client.cookies.clear()

    assert client.post(f"/api/conversations/{cid}/lock", json={"pin": "0000"}, headers=a["headers"]).status_code == 403
    r = client.post(f"/api/conversations/{cid}/lock", json={"pin": "4821"}, headers=a["headers"])
    assert r.status_code == 200
    assert not r.cookies.get("chat_unlock")  # locking never leaves the folder open
    client.cookies.clear()

    # Without the unlock cookie: hidden from the list, history refused, search skips it.
    assert conv(client, a, cid) is None
    assert client.get(f"/api/conversations/{cid}/messages", headers=a["headers"]).status_code == 423
    assert client.get("/api/search/messages?q=secret", headers=a["headers"]).json() == []
    assert client.get("/api/locked", headers=a["headers"]).json() == {"has_pin": True, "count": 1, "open": False}
    # The other person isn't affected.
    assert conv(client, b, cid) is not None

    opened = client.post("/api/locked/open", json={"pin": "4821"}, headers=a["headers"])
    unlock_cookie = opened.cookies.get("chat_unlock")
    client.cookies.clear()
    assert unlock_cookie
    with_cookie = {"cookie": a["headers"]["cookie"] + f"; chat_unlock={unlock_cookie}"}
    assert conv(client, a | {"headers": with_cookie}, cid)["locked"] is True
    assert client.get(f"/api/conversations/{cid}/messages", headers=with_cookie).status_code == 200
    assert len(client.get("/api/search/messages?q=secret", headers=with_cookie).json()) == 1

    # Someone else's unlock cookie doesn't work for you.
    b_cookie = {"cookie": b["headers"]["cookie"] + f"; chat_unlock={unlock_cookie}"}
    assert client.get("/api/locked", headers=b_cookie).json()["open"] is False

    # Removing the PIN unlocks everything.
    assert client.request("DELETE", "/api/locked/pin", json={"pin": "4821"}, headers=a["headers"]).status_code == 200
    assert conv(client, a, cid)["locked"] is False


def test_wrong_pin_lockout(client, make_user):
    a = make_user("pin_a")
    client.put("/api/locked/pin", json={"pin": "5555"}, headers=a["headers"])
    client.cookies.clear()
    codes = [client.post("/api/locked/open", json={"pin": "1111"}, headers=a["headers"]).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429
    assert client.post("/api/locked/open", json={"pin": "5555"}, headers=a["headers"]).status_code == 429


# ---------- pins, search in chat, media ----------

def test_pins(client, make_user):
    a, b = make_user("pin_x"), make_user("pin_y")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=b["headers"]) as ws_b, \
         client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
        ids = [send(ws_a, cid, f"note {i}")["id"] for i in range(4)]
        for mid in ids:
            assert client.post(f"/api/messages/{mid}/pin", headers=a["headers"]).status_code == 200
        event = receive(ws_b, "pins_changed")
        assert event["conversation_id"] == cid
    pinned = conv(client, b, cid)["pinned"]
    assert [p["content"] for p in pinned] == ["note 3", "note 2", "note 1"]  # max 3, newest first
    client.delete(f"/api/messages/{ids[3]}/pin", headers=b["headers"])
    assert [p["content"] for p in conv(client, a, cid)["pinned"]] == ["note 2", "note 1"]
    assert any("pinned a message" in m["content"] for m in history(client, a, cid))


def test_search_and_media_in_chat(client, make_user):
    a, b, c = make_user("med_a"), make_user("med_b"), make_user("med_c")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        send(ws, cid, "the quiz is on friday")
        send(ws, cid, "slides at https://example.com/dbms.pdf and http://x.org")
        send(ws, cid, "no links here")
    client.post(f"/api/conversations/{cid}/attachments?filename=pic.png", content=PNG, headers={**a["headers"], "Content-Type": "image/png"})
    client.post(f"/api/conversations/{cid}/attachments?filename=notes.txt", content=b"hello", headers={**a["headers"], "Content-Type": "text/plain"})

    found = client.get(f"/api/conversations/{cid}/search?q=QUIZ", headers=b["headers"]).json()
    assert [m["content"] for m in found] == ["the quiz is on friday"]
    photos = client.get(f"/api/conversations/{cid}/media?kind=photos", headers=b["headers"]).json()
    assert [m["type"] for m in photos] == ["image"]
    docs = client.get(f"/api/conversations/{cid}/media?kind=docs", headers=b["headers"]).json()
    assert docs[0]["attachment"]["filename"] == "notes.txt"
    links = client.get(f"/api/conversations/{cid}/media?kind=links", headers=b["headers"]).json()
    assert links[0]["links"] == ["https://example.com/dbms.pdf", "http://x.org"]
    assert client.get(f"/api/conversations/{cid}/search?q=quiz", headers=c["headers"]).status_code == 404


# ---------- groups: description, photo, admins, tags, invite links ----------

def test_group_admins_tags_and_description(client, make_user):
    a, b, c = make_user("grp_a"), make_user("grp_b"), make_user("grp_c")
    g = client.post("/api/groups", json={"name": "Hike club", "description": "Weekend treks", "usernames": [b["username"], c["username"]]}, headers=a["headers"]).json()
    gid = g["id"]
    assert g["description"] == "Weekend treks" and g["i_am_admin"] is True
    roles = {m["username"]: m["role"] for m in g["members"]}
    assert roles == {a["username"]: "admin", b["username"]: "member", c["username"]: "member"}

    # Members can't promote; admins can. The last admin can't step down.
    assert client.put(f"/api/groups/{gid}/members/{c['id']}/role", json={"role": "admin"}, headers=b["headers"]).status_code == 403
    assert client.put(f"/api/groups/{gid}/members/{a['id']}/role", json={"role": "member"}, headers=a["headers"]).status_code == 400
    out = client.put(f"/api/groups/{gid}/members/{b['id']}/role", json={"role": "admin"}, headers=a["headers"]).json()
    assert {m["username"]: m["role"] for m in out["members"]}[b["username"]] == "admin"
    # The new admin can remove people.
    assert client.delete(f"/api/groups/{gid}/members/{c['id']}", headers=b["headers"]).status_code == 200

    # Tags: your own, or anyone's if you're admin.
    client.post(f"/api/groups/{gid}/members", json={"username": c["username"]}, headers=a["headers"])
    assert client.put(f"/api/groups/{gid}/members/{c['id']}/tag", json={"tag": "Driver"}, headers=c["headers"]).status_code == 200
    assert client.put(f"/api/groups/{gid}/members/{a['id']}/tag", json={"tag": "x"}, headers=c["headers"]).status_code == 403
    tags = {m["username"]: m["tag"] for m in conv(client, a, gid)["members"]}
    assert tags[c["username"]] == "Driver"

    # Description changes post a system line.
    out = client.patch(f"/api/groups/{gid}", json={"description": "Treks every Sunday"}, headers=c["headers"]).json()
    assert out["description"] == "Treks every Sunday"
    assert "changed the group description" in history(client, a, gid)[-1]["content"]


def test_last_admin_leaving_promotes_someone(client, make_user):
    a, b, c = make_user("lead_a"), make_user("lead_b"), make_user("lead_c")
    gid = make_group(client, a, "Relay", b, c)["id"]
    client.delete(f"/api/groups/{gid}/members/{a['id']}", headers=a["headers"])
    roles = {m["username"]: m["role"] for m in conv(client, b, gid)["members"]}
    assert "admin" in roles.values()
    assert any("is now an admin" in m["content"] for m in history(client, b, gid))


def test_invite_links(client, make_user):
    a, b, c = make_user("inv_a"), make_user("inv_b"), make_user("inv_c")
    gid = make_group(client, a, "Robotics", b)["id"]
    assert client.post(f"/api/groups/{gid}/invite", headers=b["headers"]).status_code == 403
    code = client.post(f"/api/groups/{gid}/invite", headers=a["headers"]).json()["invite_code"]
    assert conv(client, a, gid)["invite_code"] == code
    assert conv(client, b, gid)["invite_code"] is None  # only admins see it

    preview = client.get(f"/api/invites/{code}", headers=c["headers"]).json()
    assert preview["title"] == "Robotics" and preview["member_count"] == 2 and preview["already_member"] is False
    joined = client.post(f"/api/invites/{code}/join", headers=c["headers"]).json()
    assert joined["id"] == gid
    assert history(client, c, gid)[-1]["content"] == f"{c['username']} joined using the invite link"

    # Resetting makes the old link stop working.
    new_code = client.post(f"/api/groups/{gid}/invite", headers=a["headers"]).json()["invite_code"]
    assert new_code != code
    assert client.get(f"/api/invites/{code}", headers=c["headers"]).status_code == 404
    client.delete(f"/api/groups/{gid}/invite", headers=a["headers"])
    assert client.get(f"/api/invites/{new_code}", headers=c["headers"]).status_code == 404

    # The /join page sends logged-out people to log in first.
    r = client.get(f"/join/{new_code}", follow_redirects=False)
    assert r.headers["location"] == f"/?next=/join/{new_code}"
    r = client.get(f"/join/{new_code}", headers=a["headers"], follow_redirects=False)
    assert r.headers["location"] == f"/chat#join={new_code}"


def test_group_photo(client, make_user):
    a, b = make_user("gph_a"), make_user("gph_b")
    gid = make_group(client, a, "Photo club", b)["id"]
    url = client.post(f"/api/groups/{gid}/photo", content=PNG, headers={**b["headers"], "Content-Type": "image/png"}).json()["avatar_url"]
    assert conv(client, a, gid)["avatar_url"] == url
    assert client.get(url, headers=a["headers"]).content == PNG


# ---------- profiles ----------

def test_profile_photo_and_bio(client, make_user):
    a, b = make_user("prof_a"), make_user("prof_b")
    assert client.put("/api/settings/profile", json={"bio": "  Hostel 4, CSE  "}, headers=a["headers"]).json()["bio"] == "Hostel 4, CSE"
    assert client.post("/api/settings/avatar", content=b"<svg/>", headers={**a["headers"], "Content-Type": "image/svg+xml"}).status_code == 400
    me = client.post("/api/settings/avatar", content=PNG, headers={**a["headers"], "Content-Type": "image/png"}).json()
    assert me["avatar_url"].startswith("/api/avatars/")
    profile = client.get(f"/api/users/{a['username']}/profile", headers=b["headers"]).json()
    assert profile["bio"] == "Hostel 4, CSE" and profile["avatar_url"] == me["avatar_url"]
    assert client.get(me["avatar_url"], headers=b["headers"]).content == PNG

    # The avatar route never serves ordinary chat files.
    cid = start_chat(client, a, b["username"])["id"]
    sent = client.post(f"/api/conversations/{cid}/attachments?filename=x.png", content=PNG, headers={**a["headers"], "Content-Type": "image/png"}).json()
    assert client.get(f"/api/avatars/{sent['attachment']['id']}", headers=b["headers"]).status_code == 404

    client.delete("/api/settings/avatar", headers=a["headers"])
    assert client.get(me["avatar_url"], headers=b["headers"]).status_code == 404


# ---------- GIFs and stickers ----------

def test_reporting_is_switched_off(client, make_user):
    a, b = make_user("norep_a"), make_user("norep_b")
    cid = start_chat(client, a, b["username"])["id"]
    assert client.post(f"/api/conversations/{cid}/report", json={"reason": "spam"}, headers=a["headers"]).status_code in (404, 405)


def test_gif_search_needs_a_key(client, make_user):
    a = make_user("gif_a")
    r = client.get("/api/gifs?q=cat", headers=a["headers"])
    assert r.status_code == 503 and "GIPHY_API_KEY" in r.json()["detail"]


def test_gif_search_with_key(client, make_user, monkeypatch):
    from app import media_search

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "abc", "title": "Cat GIF", "images": {
                "fixed_height": {"url": "https://media1.giphy.com/media/abc/200.gif", "width": "300", "height": "200"},
                "fixed_height_small": {"url": "https://media1.giphy.com/media/abc/100.gif"},
            }}]}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params):
            assert params["api_key"] == "test-key" and url.endswith("/gifs/search")
            return FakeResponse()

    monkeypatch.setattr(media_search, "GIPHY_API_KEY", "test-key")
    monkeypatch.setattr(media_search.httpx, "AsyncClient", FakeClient)
    a = make_user("gif_b")
    out = client.get("/api/gifs?q=cat", headers=a["headers"]).json()
    assert out == [{"id": "abc", "title": "Cat GIF", "url": "https://media1.giphy.com/media/abc/200.gif",
                    "preview": "https://media1.giphy.com/media/abc/100.gif", "width": 300, "height": 200}]


def test_send_gifs_and_stickers(client, make_user):
    a, b = make_user("stk_a"), make_user("stk_b")
    cid = start_chat(client, a, b["username"])["id"]
    url = f"/api/conversations/{cid}/sticker"
    gif = client.post(url, json={"kind": "gif", "url": "https://media2.giphy.com/media/x/200.gif", "title": "Dancing cat"}, headers=a["headers"])
    assert gif.status_code == 201 and gif.json()["type"] == "gif" and gif.json()["media_url"].endswith("200.gif")
    assert client.post(url, json={"kind": "gif", "url": "https://evil.example/x.gif"}, headers=a["headers"]).status_code == 400
    assert client.post(url, json={"kind": "gif", "url": "http://media.giphy.com/x.gif"}, headers=a["headers"]).status_code == 400
    sticker = client.post(url, json={"kind": "sticker", "emoji": "🔥"}, headers=a["headers"]).json()
    assert sticker["type"] == "sticker" and sticker["content"] == "🔥"
    assert client.post(url, json={"kind": "sticker", "emoji": "💩"}, headers=a["headers"]).status_code == 400
    assert "🔥" in client.get("/api/stickers", headers=a["headers"]).json()


# ---------- image generation ----------

def test_imagine_command_posts_a_picture(client, make_user, monkeypatch):
    from app import ws as ws_module

    async def fake_generate(prompt):
        assert prompt == "a red panda in the library"
        return PNG, "image/png"

    monkeypatch.setattr(ws_module, "generate_image", fake_generate)
    a, b = make_user("img_a"), make_user("img_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": cid, "content": "/imagine a red panda in the library"})
        assert receive(ws, "message")["message"]["content"].startswith("/imagine")
        typing = receive(ws, "typing")
        assert typing["drawing"] is True
        picture = receive(ws, "message")["message"]
    assert picture["sender_is_bot"] and picture["type"] == "image" and picture["content"] == "a red panda in the library"
    assert client.get(picture["attachment"]["url"], headers=b["headers"]).content == PNG


def test_imagine_failure_is_explained(client, make_user, monkeypatch):
    from app import ws as ws_module
    from app.imagegen import ImageGenError

    async def broken(prompt):
        raise ImageGenError("I couldn't make that picture (pollinations is busy). Try again in a minute.")

    monkeypatch.setattr(ws_module, "generate_image", broken)
    a = make_user("img_c")
    cid = start_chat(client, a, "ai")["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        ws.send_json({"type": "message", "conversation_id": cid, "content": "/imagine a cat"})
        receive(ws, "message")
        reply = receive(ws, "message")["message"]
    assert reply["sender_is_bot"] and "pollinations is busy" in reply["content"]


def test_ai_draw_tool_collects_pictures(monkeypatch):
    from app import tools
    from app.imagegen import made_images

    async def fake_make(prompt):
        return PNG, "image/png"

    monkeypatch.setattr(tools, "make_picture", fake_make)

    async def run():
        bucket = []
        made_images.set(bucket)
        result = json.loads(await tools.run_tool("generate_image", json.dumps({"prompt": "a yak on a hill"})))
        return result, bucket

    result, bucket = asyncio.run(run())
    assert result["ok"] is True and bucket[0]["prompt"] == "a yak on a hill"
    assert any(t["function"]["name"] == "generate_image" for t in tools.TOOLS)


def test_image_quota(monkeypatch):
    from app import imagegen

    monkeypatch.setattr(imagegen, "IMAGES_PER_HOUR", 2)
    imagegen._made.pop("user:quota-test", None)
    imagegen.check_quota("user:quota-test")
    imagegen.check_quota("user:quota-test")
    with pytest.raises(imagegen.ImageGenError):
        imagegen.check_quota("user:quota-test")


# ---------- calls ----------

def test_voice_call_flow(client, make_user):
    a, b = make_user("call_a"), make_user("call_b")
    cid = start_chat(client, a, b["username"])["id"]
    assert client.get("/api/calls/config", headers=a["headers"]).json()["iceServers"][0]["urls"]
    with client.websocket_connect("/ws", headers=b["headers"]) as ws_b, \
         client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
        ws_a.send_json({"type": "call_invite", "call_id": "call-0001-abc", "conversation_id": cid, "video": False})
        assert receive(ws_a, "call_ringing")["call_id"] == "call-0001-abc"
        incoming = receive(ws_b, "call_incoming")
        assert incoming["from"]["username"] == a["username"] and incoming["video"] is False

        ws_b.send_json({"type": "call_accept", "call_id": "call-0001-abc"})
        receive(ws_a, "call_accepted")
        ws_a.send_json({"type": "call_signal", "call_id": "call-0001-abc", "data": {"sdp": {"type": "offer", "sdp": "v=0"}}})
        assert receive(ws_b, "call_signal")["data"]["sdp"]["type"] == "offer"
        ws_b.send_json({"type": "call_signal", "call_id": "call-0001-abc", "data": {"candidate": {"candidate": "c"}}})
        assert receive(ws_a, "call_signal")["data"]["candidate"]["candidate"] == "c"

        ws_b.send_json({"type": "call_end", "call_id": "call-0001-abc"})
        assert receive(ws_a, "call_ended")["reason"] == "hangup"
        log = receive(ws_a, "message")["message"]
        assert log["type"] == "call" and log["content"].startswith("Voice call · 0:0")


def test_declined_offline_and_group_calls(client, make_user):
    a, b, c = make_user("call_c"), make_user("call_d"), make_user("call_e")
    cid = start_chat(client, a, b["username"])["id"]
    gid = make_group(client, a, "No calls", b)["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
        # Offline: ends at once and logs a missed call.
        ws_a.send_json({"type": "call_invite", "call_id": "call-0002-abc", "conversation_id": cid, "video": True})
        assert receive(ws_a, "call_ended")["reason"] == "offline"
        assert receive(ws_a, "message")["message"]["content"] == "Missed video call"
        # Groups can't call.
        ws_a.send_json({"type": "call_invite", "call_id": "call-0003-abc", "conversation_id": gid, "video": False})
        assert receive(ws_a, "call_ended")["reason"] == "not_allowed"
        with client.websocket_connect("/ws", headers=b["headers"]) as ws_b:
            ws_a.send_json({"type": "call_invite", "call_id": "call-0004-abc", "conversation_id": cid, "video": False})
            receive(ws_b, "call_incoming")
            ws_b.send_json({"type": "call_decline", "call_id": "call-0004-abc"})
            assert receive(ws_a, "call_ended")["reason"] == "declined"
            assert receive(ws_a, "message")["message"]["content"] == "Missed voice call"


def test_caller_hanging_up_while_ringing(client, make_user):
    a, b = make_user("call_f"), make_user("call_g")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=b["headers"]) as ws_b:
        with client.websocket_connect("/ws", headers=a["headers"]) as ws_a:
            ws_a.send_json({"type": "call_invite", "call_id": "call-0005-abc", "conversation_id": cid, "video": False})
            receive(ws_b, "call_incoming")
        # The caller's tab closed: the callee stops ringing.
        assert receive(ws_b, "call_ended")["reason"] == "hangup"


# ---------- appearance ----------

def test_appearance(client, make_user):
    a, b = make_user("look_a"), make_user("look_b")
    out = client.get("/api/settings/appearance", headers=a["headers"]).json()
    assert out["font"] == "Atkinson Hyperlegible" and out["wallpaper"] == "default"
    out = client.put("/api/settings/appearance", json={"bubble_mine": "#7c3aed", "bubble_shape": "square", "font": "Lexend", "font_size": 17}, headers=a["headers"]).json()
    assert (out["bubble_mine"], out["bubble_shape"], out["font"], out["font_size"]) == ("#7c3aed", "square", "Lexend", 17)
    assert client.put("/api/settings/appearance", json={"font": "Papyrus"}, headers=a["headers"]).status_code == 422
    assert client.put("/api/settings/appearance", json={"bubble_mine": "red"}, headers=a["headers"]).status_code == 422
    assert client.put("/api/settings/appearance", json={"wallpaper": "pattern", "wallpaper_value": "dots"}, headers=a["headers"]).json()["wallpaper_value"] == "dots"
    assert client.put("/api/settings/appearance", json={"wallpaper": "pattern", "wallpaper_value": "lava"}, headers=a["headers"]).status_code == 422

    out = client.post("/api/settings/wallpaper", content=PNG, headers={**a["headers"], "Content-Type": "image/png"}).json()
    assert out["wallpaper"] == "image" and out["font"] == "Lexend"  # other choices kept
    assert client.get(out["wallpaper_url"], headers=a["headers"]).content == PNG
    assert client.get(out["wallpaper_url"], headers=b["headers"]).status_code == 404
    client.put("/api/settings/appearance", json={"wallpaper": "color", "wallpaper_value": "#112233"}, headers=a["headers"])
    assert client.get(out["wallpaper_url"], headers=a["headers"]).status_code == 404  # old photo removed


# ---------- backups ----------

def test_backup_and_export(client, make_user):
    a, b = make_user("bak_a"), make_user("bak_b")
    cid = start_chat(client, a, b["username"])["id"]
    with client.websocket_connect("/ws", headers=a["headers"]) as ws:
        send(ws, cid, "remember the milk")
    client.post(f"/api/conversations/{cid}/attachments?filename=pic.png", content=PNG, headers={**a["headers"], "Content-Type": "image/png"})

    r = client.get("/api/backup", headers=a["headers"])
    assert r.headers["content-disposition"].startswith("attachment;")
    data = r.json()
    chat = next(c for c in data["chats"] if c["id"] == cid)
    assert chat["title"] == b["username"] and chat["messages"][0]["text"] == "remember the milk"
    assert "data_base64" not in chat["messages"][1]["file"]
    with_media = client.get("/api/backup?media=true", headers=a["headers"]).json()
    file = next(c for c in with_media["chats"] if c["id"] == cid)["messages"][1]["file"]
    import base64
    assert base64.b64decode(file["data_base64"]) == PNG

    text = client.get(f"/api/conversations/{cid}/export", headers=a["headers"]).text
    assert f"{a['username']}: remember the milk" in text and "<image: pic.png>" in text

    # Locked chats are left out unless open.
    client.put("/api/locked/pin", json={"pin": "2468"}, headers=a["headers"])
    client.post(f"/api/conversations/{cid}/lock", json={"pin": "2468"}, headers=a["headers"])
    client.cookies.clear()
    data = client.get("/api/backup", headers=a["headers"]).json()
    assert cid not in [c["id"] for c in data["chats"]] and data["locked_chats_left_out"] == 1
    assert client.get(f"/api/conversations/{cid}/export", headers=a["headers"]).status_code == 423


# ---------- deleting an account with all the new data ----------

def test_delete_account_cleans_up_new_data(client, make_user):
    a, b = make_user("del_a"), make_user("del_b")
    patch_user(client, b, is_private=True)
    client.post("/api/requests", json={"username": b["username"]}, headers=a["headers"])
    gid = make_group(client, b, "Mixed", )["id"]
    client.post(f"/api/groups/{gid}/members", json={"username": a["username"]}, headers=b["headers"])
    client.post(f"/api/groups/{gid}/photo", content=PNG, headers={**a["headers"], "Content-Type": "image/png"})
    client.post("/api/settings/avatar", content=PNG, headers={**a["headers"], "Content-Type": "image/png"})
    client.post("/api/categories", json={"name": "Stuff"}, headers=a["headers"])
    with client.websocket_connect("/ws", headers=b["headers"]) as ws:
        mid = send(ws, gid, "pin me")["id"]
    client.post(f"/api/messages/{mid}/pin", headers=a["headers"])

    r = client.post("/api/settings/delete-account", json={"confirm_username": a["username"]}, headers=a["headers"])
    assert r.status_code == 200, r.text
    client.cookies.clear()
    group = conv(client, b, gid)
    assert group["avatar_url"] is None and [m["username"] for m in group["members"]] == [b["username"]]
    assert client.get("/api/requests", headers=b["headers"]).json()["incoming"] == []
