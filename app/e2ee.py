"""Secret chats: end-to-end encrypted one-to-one chats.

How it works (the cryptography happens in the browser, in static/e2ee.js):
  * Each person makes a key pair (ECDH, P-256) in their browser the first time
    they set up secret chats, and chooses a passphrase.
  * The public key is stored here and shared with chat partners.
  * The private key is encrypted in the browser with a key made from the
    passphrase (PBKDF2, 310,000 rounds, then AES-GCM) before it's sent here.
    The server stores that sealed copy so you can unlock secret chats on another
    device, but without the passphrase it can't open it.
  * Two people's browsers combine their keys (ECDH) into a shared AES-256-GCM key
    per chat. Messages are encrypted before they leave the browser; the server
    only ever sees "e2e1:..." ciphertext.

  GET  /api/e2ee                  your public key and sealed private key
  PUT  /api/e2ee                  save a new key pair (first setup, or a reset)
  POST /api/secret-chats          start (or reopen) a secret chat: {"username": "riya"}

Limits, said plainly: there's no forward secrecy (one key pair per person), and
the server could in theory hand out a fake public key, which is why each chat
shows a "security code" both people can compare.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .auth import get_current_user
from .chat_requests import needs_request
from .db import get_session
from .manager import manager
from .models import Conversation, ConversationMember, User, utcnow
from .services import conversation_out, has_blocked, iso, member_ids, system_message

router = APIRouter(prefix="/api", tags=["e2ee"])


def _check_public_key(text: str) -> None:
    try:
        jwk = json.loads(text)
    except ValueError:
        raise HTTPException(422, "The public key isn't valid.")
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" or not jwk.get("x") or not jwk.get("y") or "d" in jwk:
        raise HTTPException(422, "The public key must be a P-256 public key (and never include the private part).")


def _check_sealed(text: str) -> None:
    try:
        sealed = json.loads(text)
    except ValueError:
        raise HTTPException(422, "The sealed private key isn't valid.")
    if not isinstance(sealed, dict) or not all(sealed.get(k) for k in ("salt", "iv", "ct")):
        raise HTTPException(422, "The sealed private key isn't valid.")


@router.get("/e2ee")
def my_keys(me: User = Depends(get_current_user)):
    return {
        "public_key": me.e2ee_public_key,
        "private_wrapped": me.e2ee_private_wrapped,
        "updated_at": iso(me.e2ee_updated_at),
    }


class KeysBody(BaseModel):
    public_key: str = Field(min_length=20, max_length=2000)
    private_wrapped: str = Field(min_length=20, max_length=8000)


def secret_chat_between(session: Session, a: int, b: int) -> Conversation | None:
    mine = select(ConversationMember.conversation_id).where(ConversationMember.user_id == a)
    return session.exec(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(Conversation.type == "secret", ConversationMember.user_id == b, col(Conversation.id).in_(mine))
    ).first()


@router.put("/e2ee")
async def save_keys(body: KeysBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    _check_public_key(body.public_key)
    _check_sealed(body.private_wrapped)
    changed = me.e2ee_public_key is not None and me.e2ee_public_key != body.public_key
    me.e2ee_public_key = body.public_key
    me.e2ee_private_wrapped = body.private_wrapped
    me.e2ee_updated_at = utcnow()
    session.add(me)
    session.commit()
    if changed:
        # Older secret messages can't be read with the new key, and partners should re-check the code.
        chats = session.exec(
            select(Conversation)
            .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
            .where(Conversation.type == "secret", ConversationMember.user_id == me.id)
        ).all()
        for chat in chats:
            line = system_message(session, chat.id, me, f"{me.username}'s security code changed")
            await manager.send_to_users(member_ids(session, chat.id), {"type": "message", "message": line, "client_id": None})
            await manager.send_to_users(member_ids(session, chat.id), {"type": "conversations_changed"})
    return {"ok": True, "changed": changed}


class SecretChatBody(BaseModel):
    username: str


@router.post("/secret-chats")
async def start_secret_chat(body: SecretChatBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    other = session.exec(select(User).where(User.username == body.username.strip().lower())).first()
    if other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    if other.id == me.id or other.is_bot:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Secret chats are between two people.")
    if not me.e2ee_public_key:
        raise HTTPException(status.HTTP_409_CONFLICT, "Set up secret chats in Settings first.")
    if not other.e2ee_public_key:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{other.username} hasn't set up secret chats yet. Ask them to turn it on in Settings.")
    if has_blocked(session, other.id, me.id) or has_blocked(session, me.id, other.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't message this person.")
    existing = secret_chat_between(session, me.id, other.id)
    if existing:
        return conversation_out(session, existing, me)
    if needs_request(session, me, other):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{other.username} has a private account. Send a chat request first.")

    chat = Conversation(type="secret")
    session.add(chat)
    session.commit()
    session.refresh(chat)
    session.add(ConversationMember(conversation_id=chat.id, user_id=me.id))
    session.add(ConversationMember(conversation_id=chat.id, user_id=other.id))
    session.commit()
    line = system_message(session, chat.id, me, "Messages in this secret chat are end-to-end encrypted. Only the two of you can read them.")
    await manager.send_to_users([me.id, other.id], {"type": "message", "message": line, "client_id": None})
    await manager.send_to_users([other.id], {"type": "conversations_changed"})
    return conversation_out(session, chat, me)
