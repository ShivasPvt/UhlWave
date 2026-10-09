"""Shared helpers used by both the REST routes and the WebSocket handler."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, col, func, or_, select

from .config import BOT_USERNAME
from .manager import manager
from .models import (
    Attachment, Block, Conversation, ConversationMember, Message, Poll, PollOption, PollVote, Reaction, User, utcnow,
)


def iso(dt: datetime | None) -> str | None:
    """Timestamps as ISO strings. SQLite forgets the timezone, so we put UTC back."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def avatar_url(attachment_id: int | None) -> str | None:
    return f"/api/avatars/{attachment_id}" if attachment_id else None


def user_out(user: User) -> dict:
    # People who turned off "Show online status" always look offline, with no last-seen time.
    visible = user.is_bot or user.show_presence
    return {
        "id": user.id,
        "username": user.username,
        "is_bot": user.is_bot,
        "online": True if user.is_bot else visible and manager.is_online(user.id),
        "last_seen_at": iso(user.last_seen_at) if visible else None,
        "avatar_url": avatar_url(user.avatar_id),
        "is_private": user.is_private,
    }


def not_expired():
    """SQL condition: the message hasn't disappeared yet."""
    return or_(col(Message.expires_at).is_(None), col(Message.expires_at) > utcnow())


# ---- messages ----------------------------------------------------------------

def attachment_meta(session: Session, ids: list[int]) -> dict[int, dict]:
    """Name, type and size of attachments, without loading the file bytes."""
    if not ids:
        return {}
    rows = session.exec(
        select(Attachment.id, Attachment.filename, Attachment.content_type, Attachment.size)
        .where(col(Attachment.id).in_(ids))
    ).all()
    return {
        r[0]: {"id": r[0], "filename": r[1], "content_type": r[2], "size": r[3], "url": f"/api/attachments/{r[0]}"}
        for r in rows
    }


def reactions_for(session: Session, message_ids: list[int]) -> dict[int, list[dict]]:
    """{message_id: [{"emoji": "👍", "user_ids": [2, 5]}, ...]}"""
    if not message_ids:
        return {}
    grouped: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in session.exec(
        select(Reaction).where(col(Reaction.message_id).in_(message_ids)).order_by(Reaction.created_at)
    ).all():
        grouped[r.message_id][r.emoji].append(r.user_id)
    return {mid: [{"emoji": e, "user_ids": uids} for e, uids in emojis.items()] for mid, emojis in grouped.items()}


def message_out(
    message: Message, sender: User, attachment: dict | None = None, reactions: list | None = None,
    reply: dict | None = None, poll: dict | None = None,
) -> dict:
    deleted = message.deleted_at is not None
    return {
        "reply_to": reply,
        "poll": None if deleted else poll,
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "sender_username": sender.username,
        "sender_is_bot": sender.is_bot,
        "content": "" if deleted else message.content,
        "type": message.type,
        "attachment": None if deleted else attachment,
        "location": location_out(message) if message.type == "location" and not deleted else None,
        "media_url": None if deleted else message.media_url,
        "pinned": message.pinned_at is not None and not deleted,
        "reactions": [] if deleted else (reactions or []),
        "created_at": iso(message.created_at),
        "edited_at": iso(message.edited_at),
        "deleted": deleted,
        "expires_at": iso(message.expires_at),
    }


def location_out(message: Message) -> dict | None:
    if message.latitude is None or message.longitude is None:
        return None
    lat, lon = message.latitude, message.longitude
    return {
        "latitude": lat, "longitude": lon,
        "map_url": f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=15/{lat:.5f}/{lon:.5f}",
    }


def replies_for(session: Session, ids: list[int]) -> dict[int, dict]:
    """A short preview of each message being replied to. In secret chats the text is
    still end-to-end encrypted here; the browser decrypts it."""
    if not ids:
        return {}
    out = {}
    for m, u in session.exec(select(Message, User).join(User, Message.sender_id == User.id).where(col(Message.id).in_(ids))).all():
        gone = m.deleted_at is not None or (m.expires_at is not None and m.expires_at.replace(tzinfo=None) <= utcnow().replace(tzinfo=None))
        out[m.id] = {
            "id": m.id, "sender_id": m.sender_id, "sender_username": u.username, "sender_is_bot": u.is_bot,
            "type": m.type, "content": "" if gone else m.content, "deleted": gone,
        }
    return out


def polls_for(session: Session, message_ids: list[int]) -> dict[int, dict]:
    """{message_id: poll} with options and who voted for each."""
    if not message_ids:
        return {}
    polls = session.exec(select(Poll).where(col(Poll.message_id).in_(message_ids))).all()
    if not polls:
        return {}
    poll_ids = [p.id for p in polls]
    options = session.exec(select(PollOption).where(col(PollOption.poll_id).in_(poll_ids)).order_by(PollOption.position)).all()
    votes = session.exec(select(PollVote).where(col(PollVote.poll_id).in_(poll_ids))).all()
    voters: dict[int, list[int]] = defaultdict(list)
    for v in votes:
        voters[v.option_id].append(v.user_id)
    out = {}
    for p in polls:
        mine = [o for o in options if o.poll_id == p.id]
        everyone = {v.user_id for v in votes if v.poll_id == p.id}
        out[p.message_id] = {
            "id": p.id, "multiple": p.multiple, "closed": p.closed, "voter_count": len(everyone),
            "options": [{"id": o.id, "text": o.text, "voters": voters.get(o.id, [])} for o in mine],
        }
    return out


def messages_out(session: Session, rows: list[tuple[Message, User]]) -> list[dict]:
    """message_out for many messages, fetching attachments, reactions, replies and polls in a few queries."""
    attachments = attachment_meta(session, [m.attachment_id for m, _ in rows if m.attachment_id])
    reactions = reactions_for(session, [m.id for m, _ in rows])
    replies = replies_for(session, [m.reply_to_id for m, _ in rows if m.reply_to_id])
    polls = polls_for(session, [m.id for m, _ in rows if m.type == "poll"])
    return [
        message_out(m, u, attachments.get(m.attachment_id), reactions.get(m.id), replies.get(m.reply_to_id), polls.get(m.id))
        for m, u in rows
    ]


def load_message_out(session: Session, message_id: int) -> dict | None:
    row = session.exec(select(Message, User).join(User, Message.sender_id == User.id).where(Message.id == message_id)).first()
    return messages_out(session, [row])[0] if row else None


# ---- conversations -----------------------------------------------------------

def member_ids(session: Session, conversation_id: int) -> list[int]:
    return list(
        session.exec(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation_id
            )
        ).all()
    )


def is_member(session: Session, conversation_id: int, user_id: int) -> bool:
    return session.get(ConversationMember, (conversation_id, user_id)) is not None


def membership(session: Session, conversation_id: int, user_id: int) -> ConversationMember | None:
    return session.get(ConversationMember, (conversation_id, user_id))


def is_admin(session: Session, conversation: Conversation, user_id: int) -> bool:
    """Group admins manage members, roles and the invite link. If a group somehow
    has no admins left (they all left or deleted their accounts), everyone is one."""
    if conversation.type != "group":
        return False
    me = membership(session, conversation.id, user_id)
    if me is None:
        return False
    if me.role == "admin":
        return True
    any_admin = session.exec(
        select(ConversationMember.user_id).where(
            ConversationMember.conversation_id == conversation.id, ConversationMember.role == "admin"
        )
    ).first()
    return any_admin is None


def members(session: Session, conversation_id: int) -> list[User]:
    return list(
        session.exec(
            select(User)
            .join(ConversationMember, ConversationMember.user_id == User.id)
            .where(ConversationMember.conversation_id == conversation_id)
            .order_by(User.username)
        ).all()
    )


def read_state(session: Session, conversation_id: int, me: User) -> dict[str, int]:
    """{user_id: newest message id they've seen}, for other people who share read
    receipts. Empty if you've switched yours off: it works both ways, like WhatsApp."""
    if not me.send_read_receipts:
        return {}
    rows = session.exec(
        select(ConversationMember.user_id, ConversationMember.last_read_message_id)
        .join(User, User.id == ConversationMember.user_id)
        .where(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id != me.id,
            col(User.send_read_receipts).is_(True),
            col(User.is_bot).is_(False),
        )
    ).all()
    return {str(uid): last for uid, last in rows if last is not None}


def unread_count(session: Session, conversation_id: int, me: User) -> int:
    membership = session.get(ConversationMember, (conversation_id, me.id))
    query = select(func.count()).select_from(Message).where(
        Message.conversation_id == conversation_id,
        Message.sender_id != me.id,
        col(Message.deleted_at).is_(None),
        Message.type != "system",
        not_expired(),
    )
    if membership and membership.last_read_message_id:
        query = query.where(Message.id > membership.last_read_message_id)
    return session.exec(query).one()


def pinned_messages(session: Session, conversation_id: int) -> list[dict]:
    rows = session.exec(
        select(Message, User)
        .join(User, Message.sender_id == User.id)
        .where(
            Message.conversation_id == conversation_id, col(Message.pinned_at).is_not(None),
            col(Message.deleted_at).is_(None), not_expired(),
        )
        .order_by(col(Message.pinned_at).desc())
    ).all()
    return messages_out(session, list(rows))


def conversation_out(session: Session, conversation: Conversation, me: User) -> dict:
    rows = session.exec(
        select(User, ConversationMember)
        .join(ConversationMember, ConversationMember.user_id == User.id)
        .where(ConversationMember.conversation_id == conversation.id)
        .order_by(User.username)
    ).all()
    people = [u for u, _ in rows]
    roles = {u.id: m for u, m in rows}
    mine = roles.get(me.id)
    others = [u for u in people if u.id != me.id]
    row = session.exec(
        select(Message, User)
        .join(User, Message.sender_id == User.id)
        .where(Message.conversation_id == conversation.id, not_expired())
        .order_by(Message.id.desc())
        .limit(1)
    ).first()
    is_group = conversation.type == "group"
    admin = is_admin(session, conversation, me.id)
    member_list = []
    for u in people:
        item = user_out(u)
        if is_group:
            item["role"] = roles[u.id].role
            item["tag"] = roles[u.id].tag
        if conversation.type == "secret":
            item["public_key"] = u.e2ee_public_key  # browsers derive the shared key from these
        member_list.append(item)
    return {
        "id": conversation.id,
        "type": conversation.type,
        "title": conversation.name if is_group else (", ".join(u.username for u in others) or "Just you"),
        "created_by": conversation.created_by,
        "description": conversation.description if is_group else None,
        "avatar_url": avatar_url(conversation.avatar_id) if is_group else (avatar_url(others[0].avatar_id) if others else None),
        "i_am_admin": admin,
        "invite_code": conversation.invite_code if admin else None,
        "archived": bool(mine and mine.archived),
        "locked": bool(mine and mine.locked),
        "category_id": mine.category_id if mine else None,
        "pinned": pinned_messages(session, conversation.id),
        "members": member_list,
        "last_message": messages_out(session, [row])[0] if row else None,
        "unread": unread_count(session, conversation.id, me),
        "read_state": read_state(session, conversation.id, me),
        # Blocking only applies to one-to-one chats; groups carry on as normal.
        "blocked_by_me": not is_group and bool(others) and all(has_blocked(session, me.id, u.id) for u in others),
    }


def send_problem(session: Session, conversation_id: int, user_id: int) -> str | None:
    """Why this person can't post here right now, or None if they can."""
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or not is_member(session, conversation_id, user_id):
        return "You're not in that conversation."
    if conversation.type in ("direct", "secret"):
        others = [uid for uid in member_ids(session, conversation_id) if uid != user_id]
        if any(has_blocked(session, user_id, uid) for uid in others):
            return "You blocked this person. Unblock them in Settings to send messages."
        if any(has_blocked(session, uid, user_id) for uid in others):
            return "You can't message this person."
    return None


# ---- blocking ----------------------------------------------------------------

def has_blocked(session: Session, blocker_id: int, blocked_id: int) -> bool:
    return session.get(Block, (blocker_id, blocked_id)) is not None


def blocked_either_way(session: Session, a: int, b: int) -> bool:
    return has_blocked(session, a, b) or has_blocked(session, b, a)


# ---- saving ------------------------------------------------------------------

def save_message(
    session: Session,
    conversation_id: int,
    sender: User,
    content: str,
    disappear_seconds: int | None = None,
    type: str = "text",
    attachment_id: int | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    media_url: str | None = None,
    reply_to_id: int | None = None,
) -> dict:
    """Store a message. It disappears after the sender's timer unless one is given."""
    seconds = sender.disappear_seconds if disappear_seconds is None else disappear_seconds
    expires_at = utcnow() + timedelta(seconds=seconds) if seconds else None
    message = Message(
        conversation_id=conversation_id, sender_id=sender.id, content=content,
        type=type, attachment_id=attachment_id, expires_at=expires_at,
        latitude=latitude, longitude=longitude, media_url=media_url, reply_to_id=reply_to_id,
    )
    session.add(message)
    session.commit()
    session.refresh(message)
    # Sending a message means you've read everything before it.
    membership = session.get(ConversationMember, (conversation_id, sender.id))
    if membership is not None:
        membership.last_read_message_id = message.id
        session.add(membership)
        session.commit()
    return messages_out(session, [(message, sender)])[0]


def system_message(session: Session, conversation_id: int, actor: User, text: str) -> dict:
    """A grey line in the chat like "riya added kabir". Never disappears."""
    return save_message(session, conversation_id, actor, text, disappear_seconds=0, type="system")


def ensure_bot_user(session: Session) -> User:
    """Create the AI bot account on first start. Nobody can log in as it."""
    bot = session.exec(select(User).where(User.username == BOT_USERNAME)).first()
    if bot is None:
        bot = User(username=BOT_USERNAME, password_hash="!", is_bot=True)
        session.add(bot)
        session.commit()
        session.refresh(bot)
    return bot
