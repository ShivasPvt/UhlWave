# UhlWave

A real-time chat app for campus, built for the First Commit hackathon (Kamand Prompt, IIT Mandi).
Sign up, find someone by username, and message them instantly. Mention `@ai` in any chat and an
AI assistant joins in, using the recent conversation as context.

## Features

- **Accounts:** register, log in, log out, or **Continue with Google**. Passwords hashed with bcrypt; sessions are signed JWTs in an httpOnly cookie.
- **One-to-one chat in real time** over WebSockets. Messages are saved to the database *before* they're delivered, so anything on screen is persisted.
- **Instant feedback:** your message appears immediately and is confirmed when the server saves it.
- **Typing indicators** and **online / last-seen status**.
- **AI participant:** `@ai` in any conversation, or a direct chat with `ai`. The assistant sees the last 20 messages.
- **Message history** with "load earlier messages" pagination.
- **Auto-reconnect** with backoff; missed messages are fetched when the connection returns.
- **Works on phones:** one pane at a time on small screens.
- **Encrypted storage:** messages, file names, photos, voice notes and files are encrypted inside the database with AES-256-GCM, so a leaked `chat.db` is unreadable.
- **Secret chats (end-to-end encrypted):** one-to-one chats encrypted in the browser with Web Crypto (ECDH P-256 + AES-GCM). The server only ever sees ciphertext. Keys are sealed with a passphrase so they work on other devices, and both people can compare a 60-digit security code.
- **Strong passwords:** sign-up shows a live strength meter and checklist (8+ characters, letters and numbers, not your username, not a common password), a show/hide toggle, a confirm field and a "suggest a strong password" button. The server enforces the same rules.
- **Replies:** reply from the hover bar, the right-click menu, or by swiping a message right; tap the quote to jump to the original.
- **Polls:** from the paperclip menu, single or multiple choice; votes update live for everyone, the creator can end it.
- **Feels alive:** double-click a message to ❤️ it, a jump-to-latest button that counts new messages, confetti on 🎉, soft send/receive sounds (Settings → Sounds), right-click or long-press menus, and keyboard shortcuts (press `?`).
- **Private accounts:** turn it on and strangers must send a chat request, which you accept or decline. They can't add you to groups either.
- **Profiles:** profile photo and an About line; tap anyone's name to see their card, groups in common, and call or message them.
- **Groups:** description and group photo, several admins (make or dismiss), member tags like "Class rep", member search, and **invite links** that admins can create, reset or turn off.
- **Organise your list:** filters (All, Unread, Groups, Requests), your own **categories**, **archived** chats, and **locked chats** hidden behind a PIN.
- **Pin messages** (up to 3 per chat), shown in a bar at the top.
- **Search** inside a chat, plus a **media gallery** of photos, docs, links, voice notes and places.
- **Emoji, stickers and GIFs:** emoji picker, built-in stickers, and GIPHY search for GIFs and stickers.
- **Pictures on demand:** type `/imagine a red panda in the library`, or ask `@ai draw…`, and the AI posts a generated picture.
- **Voice and video calls** in one-to-one chats, browser to browser (WebRTC), with a call log in the chat.
- **Share a location** from a map: your current location if the browser can find it, otherwise search for a place or tap the map. Locations show a map preview.
- **Appearance:** wallpaper (colours, patterns or your own photo), bubble colours and shape, font and text size. Saved to your account.
- **Media auto-download:** choose whether photos, GIFs and voice notes load by themselves, and whether documents save automatically.
- **Backup:** download all your chats (optionally with files) and open a backup again to read it; export any chat as a .txt.
- **Settings** (gear icon): hide online status, turn off typing indicators, disappearing messages (5 min to 1 week), block people, desktop notifications, hide message previews, change username, log out of all devices, delete account.

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
| GET | `/api/auth/config` | Public login settings (the Google client ID, or null) |
| POST | `/api/auth/google` | Log in or sign up with a Google ID token: `{"credential": "…"}` |
| POST | `/api/auth/logout` | Log out |
| GET | `/api/auth/me` | Current user |
| GET / PATCH | `/api/settings` | Read / change privacy settings and the disappearing-message timer |
| PUT | `/api/settings/username` | Change username |
| POST / DELETE | `/api/settings/blocked` · `/api/settings/blocked/{user_id}` | Block / unblock someone |
| POST | `/api/settings/logout-everywhere` | Invalidate every login token, keep this browser signed in |
| POST | `/api/settings/delete-account` | Delete the account and its messages: `{"confirm_username": "…"}` |
| GET | `/api/users?q=` | Search users by username |
| GET | `/api/conversations` | Your chats, most recent first |
| POST | `/api/conversations` | Start (or reopen) a DM: `{"username": "riya"}` |
| GET | `/api/conversations/{id}/messages?before=` | 50 messages, oldest first; `before` pages back |

Interactive API docs are at `/docs` while the server runs.

### Database

| Table | Columns |
|---|---|
| `users` | id, username, password_hash, is_bot, email, google_sub, show_presence, send_typing, disappear_seconds, token_version, created_at, last_seen_at |
| `conversations` | id, type (`direct`), name, created_at |
| `conversation_members` | conversation_id, user_id, last_read_message_id, joined_at |
| `messages` | id, conversation_id, sender_id, content, type, created_at, expires_at |
| `blocks` | blocker_id, blocked_id, created_at |

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

### Google sign-in

1. Go to https://console.cloud.google.com/apis/credentials, create a project if asked, and set up the
   *OAuth consent screen* (External; add your email as a test user while it's in testing).
2. *Create credentials → OAuth client ID → Web application.* Under **Authorized JavaScript origins**
   add `http://localhost:8000` and `http://localhost` (and later your `https://….onrender.com` URL).
   No redirect URI is needed.
3. Copy the client ID into `.env` as `GOOGLE_CLIENT_ID` and restart. A "Continue with Google" button
   appears on the login page.

How it works: Google's script gives the browser a signed ID token; `POST /api/auth/google` checks the
signature and that it was issued for this client ID, then logs the person in with the same cookie as a
password login. The first time, an account is created with a username taken from the email
(`riya.sharma@gmail.com` → `riya_sharma`, or `riya_sharma2` if taken). Set `GOOGLE_ALLOWED_DOMAIN` to
let in only one email domain, such as your college's.

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
| `MESSAGE_KEY` | yes, keep it safe | 32 random bytes (base64) that encrypt stored messages and files. Lose it and stored chats can't be read. Locally, one is made in `.message_key` if unset. |
| `JWT_SECRET` | yes, in production | Signs login tokens. Long and random. |
| `COOKIE_SECURE` | in production | `true` behind HTTPS so cookies are never sent unencrypted. |
| `LLM_API_KEY` | for AI | Key for the chat API. Stays on the server. |
| `GOOGLE_CLIENT_ID` | for Google sign-in | OAuth client ID (Web application). Empty turns the button off. |
| `GOOGLE_ALLOWED_DOMAIN` | no | Only allow emails at this domain, e.g. `students.iitmandi.ac.in`. |
| `LLM_BASE_URL` | no | Any OpenAI-compatible API. Default: Groq. |
| `LLM_MODEL` | no | Model name for that API. |
| `AI_CONTEXT_MESSAGES` | no | How many recent messages the AI sees. Default 20. |
| `GIPHY_API_KEY` | for GIF search | Free key from developers.giphy.com. Built-in stickers work without it. |
| `HF_TOKEN` | for `/imagine` | Hugging Face token (free credits). Or use `POLLINATIONS_API_KEY`. |
| `POLLINATIONS_API_KEY` | no | Alternative image service. With no key at all, a keyless fallback is tried. |
| `IMAGE_PROVIDER`, `IMAGES_PER_HOUR` | no | `auto`/`huggingface`/`pollinations`; pictures per person per hour (10). |
| `TURN_URLS`, `TURN_USERNAME`, `TURN_CREDENTIAL` | for strict networks | A TURN relay so calls connect through college Wi-Fi or mobile data. |

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
  groups.py      groups: members, admins, tags, description, photo, invite links
  messages.py    attachments, edit/delete/react, pins, search, media, GIFs/stickers, locations
  profiles.py    profile photos and About lines
  chat_requests.py  private accounts and chat requests
  folders.py     archive, categories, locked chats and the PIN
  calls.py       voice/video call signalling and the call log
  imagegen.py    /imagine and @ai pictures (Hugging Face or Pollinations)
  media_search.py  GIF and sticker search through GIPHY
  appearance.py  wallpaper, bubble colours and shape, font
  backup.py      backups and chat export
  crypto.py      encrypted storage: AES-256-GCM columns for messages and files
  e2ee.py        secret chats: public keys, sealed private keys, starting a secret chat
  polls.py       polls and live voting
  tools.py       what @ai can look up: weather, places, and drawing pictures
  static/        index.html (login), chat.html, styles.css, and one JS file per area:
                 chat.js (core), groups.js, settings.js, extras.js (folders, menu, pins,
                 profiles, invites), picker.js, location.js, calls.js, appearance.js,
                 e2ee.js (secret chats, all cryptography in the browser), polls.js,
                 interact.js (replies by swipe, hearts, right-click menu, shortcuts, confetti)
  static/vendor/leaflet/   the map library, kept locally so it works without a CDN
tests/           pytest suite
```

## Ideas for what to build next
- **Smarter AI:** summarise a long chat, or remember each user's preferences.

## Known limits

- **Encryption:** normal chats are encrypted in storage, but the running server holds the key (that is what lets @ai, search and backups work). Secret chats are end-to-end encrypted; they use one key pair per person (no forward secrecy), so compare the security code, and they carry text only (no files, polls, @ai or server search).

- **Calls are one-to-one.** Group calls would need a media server. On strict networks calls need a TURN relay (see the environment variables).
- **Locked chats** are hidden in the list, search, history and backups until the PIN is entered, but new messages still arrive live in the open tab (without a preview or notification text).
- Who's online and calls in progress are kept in memory, so it assumes a single server process. Running several would need Redis pub/sub.
- Database calls inside the socket handler are synchronous; fine at hackathon scale.
