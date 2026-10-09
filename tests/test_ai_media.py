"""@ai reading photos, voice notes and files. The AI service itself is faked:
we capture what would be sent to it instead of calling Groq."""
import io
import zipfile

import pytest

from app import ai_media, bot, ws
from tests.test_chat import receive, start_chat

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_pdf(text: str) -> bytes:
    """A real one-page PDF containing `text`, built by hand so tests need no extra tools."""
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def make_docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="w"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", xml)
    return buffer.getvalue()


# ---------- reading files ----------

def test_extract_text_from_common_files(monkeypatch):
    assert "Hello Uhl" in ai_media.extract_text("notes.pdf", "application/pdf", make_pdf("Hello Uhl"))
    assert ai_media.extract_text("essay.docx", "application/octet-stream", make_docx("First line", "Tom &amp; Jerry")) == "First line\nTom & Jerry"
    assert ai_media.extract_text("main.py", "application/octet-stream", b"print('hi')") == "print('hi')"
    assert ai_media.extract_text("data.csv", "text/csv", b"a,b\n1,2") == "a,b\n1,2"
    assert ai_media.extract_text("song.mp3", "audio/mpeg", b"\x00\x01") is None
    assert ai_media.extract_text("broken.pdf", "application/pdf", b"not a pdf") is None

    monkeypatch.setattr(ai_media, "AI_MAX_FILE_CHARS", 10)
    long = ai_media.extract_text("long.txt", "text/plain", b"x" * 50)
    assert long.startswith("x" * 10) and "cut off" in long


def test_big_photos_are_not_sent(monkeypatch):
    monkeypatch.setattr(ai_media, "AI_MAX_IMAGE_BYTES", 10)
    assert ai_media.image_data_url("image/png", b"x" * 11) is None
    assert ai_media.image_data_url("image/png", b"x" * 5).startswith("data:image/png;base64,")


# ---------- what the model receives ----------

def test_photos_become_image_parts_and_switch_model():
    history = [
        {"sender_is_bot": False, "sender_username": "riya", "content": "[photo: cat.png]", "images": ["data:image/png;base64,AAAA"]},
        {"sender_is_bot": False, "sender_username": "riya", "content": "what is this?"},
    ]
    messages = bot.build_messages(history)
    assert messages[1]["content"][0] == {"type": "text", "text": "riya: [photo: cat.png]"}
    assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png")
    assert messages[2]["content"] == "riya: what is this?"
    assert bot.choose_model(history) == bot.LLM_VISION_MODEL
    assert bot.choose_model(history[1:]) == bot.LLM_MODEL
    assert bot.clean_reply("<think>hmm</think> It's a cat.") == "It's a cat."


# ---------- end to end: send things to @ai ----------

@pytest.fixture
def fake_ai(monkeypatch):
    """Capture the history the AI would get, and fake Whisper."""
    seen = []
    calls = {"transcribe": 0}

    async def fake_reply(history):
        seen.append(history)
        return "got it"

    async def fake_transcribe(filename, content_type, data):
        calls["transcribe"] += 1
        return "meet at the library at five"

    monkeypatch.setattr(ws, "generate_reply", fake_reply)
    monkeypatch.setattr(ai_media, "transcribe", fake_transcribe)
    return seen, calls


def upload(client, user, cid, data, content_type, filename, kind="auto"):
    r = client.post(
        f"/api/conversations/{cid}/attachments?filename={filename}&kind={kind}",
        content=data, headers={**user["headers"], "content-type": content_type},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_ai_reads_photos_voice_and_files(client, make_user, fake_ai):
    seen, calls = fake_ai
    alice = make_user("alice")
    cid = start_chat(client, alice, "ai")["id"]

    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
        # A photo sent straight to @ai gets an answer, and the AI sees the image.
        upload(client, alice, cid, PNG, "image/png", "board.png")
        assert receive(ws_alice, "message")["message"]["type"] == "image"
        assert receive(ws_alice, "message")["message"]["content"] == "got it"
        last = seen[-1][-1]
        assert last["content"] == "[photo: board.png]" and last["images"][0].startswith("data:image/png;base64,")

        # A voice note is transcribed.
        upload(client, alice, cid, b"OggS" + b"\x00" * 64, "audio/webm", "voice.webm", kind="voice")
        receive(ws_alice, "message"); receive(ws_alice, "message")
        assert seen[-1][-1]["content"] == "[voice note] meet at the library at five"

        # A PDF is read.
        upload(client, alice, cid, make_pdf("Exam on Friday"), "application/pdf", "notice.pdf")
        receive(ws_alice, "message"); receive(ws_alice, "message")
        assert seen[-1][-1]["content"].startswith("[file: notice.pdf]") and "Exam on Friday" in seen[-1][-1]["content"]

        # The voice note was transcribed once and remembered, not again on every reply.
        assert calls["transcribe"] == 1


def test_ai_in_a_group_only_sees_the_newest_photos(client, make_user, fake_ai, monkeypatch):
    seen, _ = fake_ai
    monkeypatch.setattr(ai_media, "AI_MAX_IMAGES", 1)
    alice, bob = make_user("alice"), make_user("bob")
    gid = client.post("/api/groups", json={"name": "Photos", "usernames": [bob["username"]]}, headers=alice["headers"]).json()["id"]
    upload(client, alice, gid, PNG, "image/png", "old.png")
    upload(client, bob, gid, PNG, "image/png", "new.png")
    assert seen == []  # photos in a group don't wake the AI on their own

    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
        ws_alice.send_json({"type": "message", "conversation_id": gid, "content": "@ai which photo is newer?"})
        receive(ws_alice, "message")
        assert receive(ws_alice, "message")["message"]["sender_is_bot"] is True
    contents = [m["content"] for m in seen[-1]]
    assert "[earlier photo: old.png]" in contents and "[photo: new.png]" in contents
    assert sum(1 for m in seen[-1] if m.get("images")) == 1
