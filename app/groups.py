"""Group chats: create, edit, members, admins, tags and invite links.

Who can do what:
  anyone in the group   add people, rename, change the description and photo,
                        set their own tag, leave
  admins                remove people, make or dismiss admins, set anyone's tag,
                        create, reset or turn off the invite link

Whoever creates a group is its first admin. If the last admin leaves, the person
who has been in the group longest becomes admin, so a group is never stuck.
Each change posts a grey "system" line in the chat, like WhatsApp.

  POST   /api/groups                                  create
  PATCH  /api/groups/{id}                             {"name": ..., "description": ...}
  POST   /api/groups/{id}/photo                       upload a group photo (raw image body)
  DELETE /api/groups/{id}/photo
  POST   /api/groups/{id}/members                     add someone: {"username": ...}
  DELETE /api/groups/{id}/members/{user_id}           remove someone, or leave (your own id)
  PUT    /api/groups/{id}/members/{user_id}/role      {"role": "admin" | "member"}
  PUT    /api/groups/{id}/members/{user_id}/tag       {"tag": "Class rep"}  (empty to clear)
  POST   /api/groups/{id}/invite                      create or reset the invite link
  DELETE /api/groups/{id}/invite                      turn the link off
  GET    /api/invites/{code}                          what the link leads to (name, members…)
  POST   /api/invites/{code}/join                     join through the link
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .auth import get_current_user
from .chat_requests import private_add_problem
from .db import engine, get_session
from .manager import manager
from .models import Conversation, ConversationMember, User
from .profiles import MAX_AVATAR_BYTES, delete_attachment, read_image_upload, save_image
from .services import avatar_url, conversation_out, has_blocked, is_admin, is_member, member_ids, system_message

router = APIRouter(prefix="/api", tags=["groups"])

MAX_GROUP_SIZE = 50


class NewGroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    usernames: list[str] = Field(default_factory=list, max_length=MAX_GROUP_SIZE)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)


class AddMemberBody(BaseModel):
    username: str


class RoleBody(BaseModel):
    role: str = Field(pattern="^(admin|member)$")


class TagBody(BaseModel):
    tag: str | None = Field(default=None, max_length=30)


def _group_for_member(session: Session, group_id: int, me: User) -> Conversation:
    group = session.get(Conversation, group_id)
    if group is None or group.type != "group" or not is_member(session, group_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found.")
    return group


def _require_admin(session: Session, group: Conversation, me: User, what: str) -> None:
    if not is_admin(session, group, me.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Only group admins can {what}.")


async def _announce(conversation_id: int, message: dict | None, also: list[int] = ()) -> None:
    """Push the system message to everyone in the group, and tell everyone affected
    (including people just removed) to refresh their chat list."""
    with Session(engine) as session:
        people = member_ids(session, conversation_id)
    if message:
        await manager.send_to_users(people, {"type": "message", "message": message, "client_id": None})
    await manager.send_to_users([*people, *also], {"type": "conversations_changed"})


def _admins(session: Session, group_id: int) -> list[ConversationMember]:
    return list(session.exec(select(ConversationMember).where(
        ConversationMember.conversation_id == group_id, ConversationMember.role == "admin"
    )).all())


def _promote_if_no_admin(session: Session, group: Conversation) -> User | None:
    """If nobody is admin any more, make the longest-standing member admin."""
    if _admins(session, group.id):
        return None
    oldest = session.exec(
        select(ConversationMember).where(ConversationMember.conversation_id == group.id)
        .order_by(col(ConversationMember.joined_at), col(ConversationMember.user_id))
    ).first()
    if oldest is None:
        return None
    oldest.role = "admin"
    session.add(oldest)
    session.commit()
    return session.get(User, oldest.user_id)


# ---- create and edit -------------------------------------------------------------

@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_group(body: NewGroupBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Give the group a name.")
    wanted = {u.strip().lower() for u in body.usernames if u.strip()} - {me.username}
    people = session.exec(select(User).where(col(User.username).in_(wanted))).all() if wanted else []
    missing = wanted - {u.username for u in people}
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No user called {sorted(missing)[0]}.")
    for user in people:
        problem = private_add_problem(session, me, user)
        if problem:
            raise HTTPException(status.HTTP_403_FORBIDDEN, problem)
        if has_blocked(session, user.id, me.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"You can't add {user.username}.")

    group = Conversation(type="group", name=name, created_by=me.id, description=(body.description or "").strip() or None)
    session.add(group)
    session.commit()
    session.refresh(group)
    for user in [me, *people]:
        role = "admin" if user.id == me.id else "member"
        session.add(ConversationMember(conversation_id=group.id, user_id=user.id, role=role))
    session.commit()

    text = f"{me.username} created the group"
    if people:
        text += " with " + ", ".join(u.username for u in people)
    message = system_message(session, group.id, me, text)
    out = conversation_out(session, group, me)
    await _announce(group.id, message)
    return out


@router.patch("/groups/{group_id}")
async def edit_group(group_id: int, body: GroupPatch, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    lines = []
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Give the group a name.")
        if name != group.name:
            group.name = name
            lines.append(f'{me.username} renamed the group to "{name}"')
    if body.description is not None:
        description = body.description.strip() or None
        if description != group.description:
            group.description = description
            lines.append(f"{me.username} {'changed' if description else 'removed'} the group description")
    if not lines:
        return conversation_out(session, group, me)
    session.add(group)
    session.commit()
    message = None
    for line in lines:
        message = system_message(session, group.id, me, line)
        await manager.send_to_users(member_ids(session, group.id), {"type": "message", "message": message, "client_id": None})
    out = conversation_out(session, group, me)
    await _announce(group.id, None)
    return out


@router.post("/groups/{group_id}/photo")
async def set_group_photo(group_id: int, request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    data, content_type = await read_image_upload(request, MAX_AVATAR_BYTES)
    old = group.avatar_id
    group.avatar_id = save_image(session, me, data, content_type, "group-photo").id
    session.add(group)
    delete_attachment(session, old)
    session.commit()
    message = system_message(session, group.id, me, f"{me.username} changed the group photo")
    await _announce(group.id, message)
    return {"avatar_url": avatar_url(group.avatar_id)}


@router.delete("/groups/{group_id}/photo")
async def remove_group_photo(group_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    if group.avatar_id:
        old = group.avatar_id
        group.avatar_id = None
        session.add(group)
        delete_attachment(session, old)
        session.commit()
        message = system_message(session, group.id, me, f"{me.username} removed the group photo")
        await _announce(group.id, message)
    return {"avatar_url": None}


# ---- members --------------------------------------------------------------------

@router.post("/groups/{group_id}/members")
async def add_member(group_id: int, body: AddMemberBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    user = session.exec(select(User).where(User.username == body.username.strip().lower())).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    if is_member(session, group_id, user.id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"{user.username} is already in the group.")
    problem = private_add_problem(session, me, user)
    if problem:
        raise HTTPException(status.HTTP_403_FORBIDDEN, problem)
    if has_blocked(session, user.id, me.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"You can't add {user.username}.")
    if len(member_ids(session, group_id)) >= MAX_GROUP_SIZE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Groups can have up to {MAX_GROUP_SIZE} people.")
    session.add(ConversationMember(conversation_id=group_id, user_id=user.id))
    session.commit()
    message = system_message(session, group_id, me, f"{me.username} added {user.username}")
    out = conversation_out(session, group, me)
    await _announce(group_id, message)
    return out


@router.delete("/groups/{group_id}/members/{user_id}")
async def remove_member(group_id: int, user_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Leave the group (your own id), or remove someone (admins only)."""
    group = _group_for_member(session, group_id, me)
    leaving = user_id == me.id
    if not leaving:
        _require_admin(session, group, me, "remove people")
    membership = session.get(ConversationMember, (group_id, user_id))
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "They're not in this group.")
    user = session.get(User, user_id)
    session.delete(membership)
    if leaving and group.created_by == me.id:
        group.created_by = None
        session.add(group)
    session.commit()

    text = f"{me.username} left" if leaving else f"{me.username} removed {user.username}"
    message = system_message(session, group_id, me, text)
    await manager.send_to_users(member_ids(session, group_id), {"type": "message", "message": message, "client_id": None})
    promoted = _promote_if_no_admin(session, group)
    if promoted:
        note = system_message(session, group_id, promoted, f"{promoted.username} is now an admin")
        await manager.send_to_users(member_ids(session, group_id), {"type": "message", "message": note, "client_id": None})
    await _announce(group_id, None, also=[user_id])
    return {"ok": True}


@router.put("/groups/{group_id}/members/{user_id}/role")
async def set_role(group_id: int, user_id: int, body: RoleBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    _require_admin(session, group, me, "change who's an admin")
    membership = session.get(ConversationMember, (group_id, user_id))
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "They're not in this group.")
    if membership.role == body.role:
        return conversation_out(session, group, me)
    if body.role == "member" and membership.role == "admin" and len(_admins(session, group_id)) == 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A group needs at least one admin. Make someone else admin first.")
    membership.role = body.role
    session.add(membership)
    session.commit()
    user = session.get(User, user_id)
    if body.role == "admin":
        text = f"{me.username} made {user.username} an admin"
    elif user_id == me.id:
        text = f"{me.username} is no longer an admin"
    else:
        text = f"{me.username} dismissed {user.username} as admin"
    message = system_message(session, group_id, me, text)
    await _announce(group_id, message)
    return conversation_out(session, group, me)


@router.put("/groups/{group_id}/members/{user_id}/tag")
async def set_tag(group_id: int, user_id: int, body: TagBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    if user_id != me.id:
        _require_admin(session, group, me, "change other people's tags")
    membership = session.get(ConversationMember, (group_id, user_id))
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "They're not in this group.")
    membership.tag = (body.tag or "").strip() or None
    session.add(membership)
    session.commit()
    await _announce(group_id, None)
    return conversation_out(session, group, me)


# ---- invite links ------------------------------------------------------------------

@router.post("/groups/{group_id}/invite")
async def create_invite(group_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Make a new invite link. Any old link stops working."""
    group = _group_for_member(session, group_id, me)
    _require_admin(session, group, me, "manage the invite link")
    group.invite_code = secrets.token_urlsafe(12)
    session.add(group)
    session.commit()
    return {"invite_code": group.invite_code, "path": f"/join/{group.invite_code}"}


@router.delete("/groups/{group_id}/invite")
async def revoke_invite(group_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_for_member(session, group_id, me)
    _require_admin(session, group, me, "manage the invite link")
    group.invite_code = None
    session.add(group)
    session.commit()
    return {"invite_code": None}


def _group_by_code(session: Session, code: str) -> Conversation:
    group = session.exec(select(Conversation).where(Conversation.invite_code == code)).first() if code else None
    if group is None or group.type != "group":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite link doesn't work any more. Ask for a new one.")
    return group


@router.get("/invites/{code}")
def invite_preview(code: str, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_by_code(session, code)
    return {
        "id": group.id,
        "title": group.name,
        "description": group.description,
        "avatar_url": avatar_url(group.avatar_id),
        "member_count": len(member_ids(session, group.id)),
        "already_member": is_member(session, group.id, me.id),
    }


@router.post("/invites/{code}/join")
async def join_with_invite(code: str, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _group_by_code(session, code)
    if not is_member(session, group.id, me.id):
        if len(member_ids(session, group.id)) >= MAX_GROUP_SIZE:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"This group is full ({MAX_GROUP_SIZE} people).")
        session.add(ConversationMember(conversation_id=group.id, user_id=me.id))
        session.commit()
        message = system_message(session, group.id, me, f"{me.username} joined using the invite link")
        await _announce(group.id, message)
    return conversation_out(session, group, me)
