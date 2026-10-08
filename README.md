# Valley

A real-time chat app for campus, built for the First Commit hackathon (Kamand Prompt, IIT Mandi).
Sign up, find someone by username, and message them instantly. Mention `@ai` in any chat and an
AI assistant joins in, using the recent conversation as context.

> "Valley" is a placeholder name. Rename it in `app/static/*.html`.

## Features

- **Accounts:** register, log in, log out. Passwords hashed with bcrypt; sessions are signed JWTs in an httpOnly cookie.
- **One-to-one chat in real time** over WebSockets. Messages are saved to the database *before* they're delivered, so anything on screen is persisted.
- **Instant feedback:** your message appears immediately and is confirmed when the server saves it.
- **Typing indicators** and **online / last-seen status**.
- **AI participant:** `@ai` in any conversation, or a direct chat with `ai`. The assistant sees the last 20 messages.
- **Message history** with "load earlier messages" pagination.
- **Auto-reconnect** with backoff; missed messages are fetched when the connection returns.
- **Works on phones:** one pane at a time on small screens.

## Architecture

```
Browser (HTML + Tailwind + vanilla JS)
   │  HTTPS: pages, login, history          │  WebSocket /ws: live messages, typing, presence
   ▼                                        ▼
┌───────────────────── One FastAPI app (Uvicorn) ─────────────────────┐
│  auth.py        api.py              ws.py + manager.py     bot.py   │
│  register/login conversations,      connection manager:    calls an │
│  JWT cookie     history, search     user_id → open sockets LLM API  │
└──────────────────────────┬──────────────────────────────────┬───────┘
                           ▼                                  ▼
               PostgreSQL / SQLite (SQLModel)        Groq / Gemini / OpenAI
```

**Why one server?** FastAPI serves the pages, the REST API and the WebSocket from one process, so
there's one thing to deploy and no cross-origin setup. Serverless hosts (like Vercel functions)
can't hold WebSockets open, which is why this runs on Render as a long-lived server.

### How a message travels

1. The browser sends `{"type": "message", "conversation_id": 7, "content": "hi", "client_id": "tmp-…"}` over its socket.
2. The server already knows who sent it: the login cookie was checked when the socket opened.
3. It checks the sender is a member of conversation 7, saves the message, then pushes it to every open socket belonging to the conversation's members.
4. The sender's browser matches `client_id` and swaps its "Sending…" bubble for the saved message.
5. If the text mentions `@ai`, the bot runs as a background task (so the chat never waits on it), shows a typing indicator, and posts its reply the same way.

### WebSocket events

| Direction | Event | Fields |
|---|---|---|
| browser → server | `message` | `conversation_id`, `content`, `client_id` |
| browser → server | `typing` | `conversation_id`, `is_typing` |
| server → browser | `message` | `message` (id, sender, content, time), `client_id` |
| server → browser | `typing` | `conversation_id`, `user_id`, `username`, `is_typing` |
| server → browser | `presence` | `user_id`, `online` |
| server → browser | `error` | `detail`, maybe `client_id` |

A socket without a valid login is closed with code `4401`.

### REST endpoints

| Method | Path | What it does |
|---|---|---|
| POST | `/api/auth/register` | Create an account and log in |
| POST | `/api/auth/login` | Log in |
| POST | `/api/auth/logout` | Log out |
| GET | `/api/auth/me` | Current user |
| GET | `/api/users?q=` | Search users by username |
| GET | `/api/conversations` | Your chats, most recent first |
| POST | `/api/conversations` | Start (or reopen) a DM: `{"username": "riya"}` |
| GET | `/api/conversations/{id}/messages?before=` | 50 messages, oldest first; `before` pages back |

Interactive API docs are at `/docs` while the server runs.

### Database

| Table | Columns |
|---|---|
| `users` | id, username, password_hash, is_bot, created_at, last_seen_at |
| `conversations` | id, type (`direct`), name, created_at |
| `conversation_members` | conversation_id, user_id, last_read_message_id, joined_at |
| `messages` | id, conversation_id, sender_id, content, type, created_at |

A DM is a conversation with two members, so group chats only need a `group` type and a way to add members.

## Run it locally

Needs Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
uvicorn app.main:app --reload
```

Open http://localhost:8000. To test a conversation, open a second browser profile or a private
window, make a second account, and message between them.

With no `DATABASE_URL` set, data goes into a local `chat.db` SQLite file. Delete it to start fresh.

To switch on the AI, get a free key from https://console.groq.com/keys, put it in `.env` as
`LLM_API_KEY`, and restart. Without a key the assistant replies with a reminder to add one.

## Tests

```bash
pytest -q
```

The tests use a throwaway database and cover sign-up and login, real-time delivery between two
users over WebSockets, persistence, access control (outsiders can't read or post), typing,
pagination, bad input, and the AI flow.

## Deploy (Render + Supabase, both free)

1. **Database:** create a Supabase project. Go to *Connect* and copy the **Session pooler**
   connection string (the direct one is IPv6-only and Render can't reach it).
2. **App:** push this repo to GitHub. On Render choose *New → Blueprint* and pick the repo;
   `render.yaml` sets everything up. Fill in `DATABASE_URL` and `LLM_API_KEY` when asked.
3. Open the `.onrender.com` URL. Free Render services sleep after inactivity, so open the link a
   minute before demoing.

## Environment variables

| Name | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | for deployment | Postgres connection string. Defaults to local SQLite. |
| `JWT_SECRET` | yes, in production | Signs login tokens. Long and random. |
| `COOKIE_SECURE` | in production | `true` behind HTTPS so cookies are never sent unencrypted. |
| `LLM_API_KEY` | for AI | Key for the chat API. Stays on the server. |
| `LLM_BASE_URL` | no | Any OpenAI-compatible API. Default: Groq. |
| `LLM_MODEL` | no | Model name for that API. |
| `AI_CONTEXT_MESSAGES` | no | How many recent messages the AI sees. Default 20. |

## Project layout

```
app/
  main.py        app setup, page routes
  config.py      settings from environment variables
  db.py          database connection
  models.py      tables
  auth.py        passwords, JWT cookie, auth routes
  api.py         REST routes
  manager.py     who's connected right now
  ws.py          the WebSocket endpoint and event handling
  bot.py         AI replies
  services.py    shared helpers
  static/        index.html (login), chat.html, chat.js, auth.js, theme.js
tests/           pytest suite
```

## Ideas for what to build next

- **Group chats:** add `type="group"`, a create-group route, and member management. The socket code already sends to every member.
- **Read receipts:** `conversation_members.last_read_message_id` is ready; send a `read` event when a chat is open.
- **Images, voice notes and files:** an upload route that stores to Supabase Storage, plus `message.type`.
- **Search, edit and delete messages; reactions; notifications.**
- **Smarter AI:** summarise a long chat, or remember each user's preferences.

## Known limits

- Who's online is kept in memory, so it assumes a single server process. Running several would need Redis pub/sub.
- Database calls inside the socket handler are synchronous; fine at hackathon scale.
