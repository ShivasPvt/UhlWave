// Everything the chat page does in the browser.
// Rule of thumb: user-written text is always inserted with textContent, never
// innerHTML, so nobody can inject HTML or scripts by sending a message.

const $ = (id) => document.getElementById(id);

const state = {
  me: null,
  conversations: [],   // newest activity first
  activeId: null,
  unread: {},          // conversation id -> count
  typing: {},          // conversation id -> { username, timer }
  hasMore: false,      // can we load older messages in the open chat?
};

let socket = null;
let reconnectDelay = 1000;
let hasConnectedBefore = false;

// ---------- talking to the REST API ----------

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (res.status === 401) {
    window.location.href = "/";
    throw new Error("Logged out");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong.");
  return data;
}

// ---------- WebSocket ----------

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws`);

  socket.addEventListener("open", () => {
    reconnectDelay = 1000;
    $("connection").textContent = "";
    // After a dropped connection, fetch anything we missed while offline.
    if (hasConnectedBefore) resync();
    hasConnectedBefore = true;
  });

  socket.addEventListener("message", (event) => handleEvent(JSON.parse(event.data)));

  socket.addEventListener("close", (event) => {
    if (event.code === 4401) {
      window.location.href = "/";
      return;
    }
    $("connection").textContent = "Reconnecting…";
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000); // back off: 1s, 2s, 4s… up to 15s
  });
}

function send(event) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(event));
    return true;
  }
  return false;
}

function handleEvent(event) {
  if (event.type === "message") onMessage(event.message, event.client_id);
  else if (event.type === "typing") onTyping(event);
  else if (event.type === "presence") onPresence(event);
  else if (event.type === "error") onServerError(event);
}

// ---------- incoming events ----------

function onMessage(message, clientId) {
  const conversation = findConversation(message.conversation_id);
  if (!conversation) {
    // Someone started a new chat with us. Reload the list to get it.
    state.unread[message.conversation_id] = (state.unread[message.conversation_id] || 0) + 1;
    loadConversations();
    return;
  }

  conversation.last_message = message;
  state.conversations = [conversation, ...state.conversations.filter((c) => c !== conversation)];

  if (message.conversation_id === state.activeId) {
    const pending = clientId && document.querySelector(`[data-client-id="${CSS.escape(clientId)}"]`);
    const element = renderMessage(message);
    if (pending) pending.replaceWith(element);
    else appendMessage(element);
    clearTyping(message.conversation_id);
  } else if (message.sender_id !== state.me.id) {
    state.unread[message.conversation_id] = (state.unread[message.conversation_id] || 0) + 1;
  }
  renderSidebar();
}

function onTyping({ conversation_id, username, is_typing }) {
  clearTimeout(state.typing[conversation_id]?.timer);
  if (is_typing) {
    // Safety net: hide the indicator if the "stopped typing" event never arrives.
    const timer = setTimeout(() => clearTyping(conversation_id), 6000);
    state.typing[conversation_id] = { username, timer };
  } else {
    delete state.typing[conversation_id];
  }
  renderTyping();
}

function clearTyping(conversationId) {
  clearTimeout(state.typing[conversationId]?.timer);
  delete state.typing[conversationId];
  renderTyping();
}

function onPresence({ user_id, online }) {
  for (const conversation of state.conversations) {
    for (const member of conversation.members) {
      if (member.id === user_id) {
        member.online = online;
        if (!online) member.last_seen_at = new Date().toISOString();
      }
    }
  }
  renderSidebar();
  renderHeader();
}

function onServerError({ detail, client_id }) {
  if (client_id) document.querySelector(`[data-client-id="${CSS.escape(client_id)}"] .bubble`)?.classList.add("bubble-failed");
  $("composer-error").textContent = detail;
}

// ---------- loading data ----------

async function loadConversations() {
  state.conversations = await api("/api/conversations");
  renderSidebar();
  renderHeader();
}

async function resync() {
  await loadConversations();
  if (state.activeId) await loadHistory();
}

async function openConversation(id) {
  state.activeId = id;
  state.unread[id] = 0;
  $("empty-state").classList.add("hidden");
  $("chat-view").classList.remove("hidden");
  $("chat-view").classList.add("flex");
  $("composer-error").textContent = "";
  setView("chat");
  renderSidebar();
  renderHeader();
  renderTyping();
  await loadHistory();
  $("message-input").focus();
}

async function loadHistory(beforeId = null) {
  const id = state.activeId;
  const query = beforeId ? `?before=${beforeId}` : "";
  const messages = await api(`/api/conversations/${id}/messages${query}`);
  if (id !== state.activeId) return; // user switched chats while this was loading

  const box = $("messages");
  state.hasMore = messages.length === 50;

  if (beforeId) {
    // Prepend older messages without making the view jump.
    const previousHeight = box.scrollHeight;
    box.querySelector("#load-older")?.remove();
    box.prepend(...messages.map(renderMessage));
    addLoadOlderButton();
    box.scrollTop = box.scrollHeight - previousHeight;
  } else {
    box.replaceChildren(...messages.map(renderMessage));
    if (messages.length === 0) box.append(emptyChatNote());
    addLoadOlderButton();
    box.scrollTop = box.scrollHeight;
  }
}

function addLoadOlderButton() {
  if (!state.hasMore) return;
  const first = $("messages").querySelector("[data-message-id]");
  const button = el("button", "mx-auto mb-3 block rounded-lg px-3 py-1 text-sm text-slate underline", "Load earlier messages");
  button.id = "load-older";
  button.addEventListener("click", () => loadHistory(Number(first.dataset.messageId)));
  $("messages").prepend(button);
}

// ---------- sending ----------

let typingSent = false;
let stopTypingTimer = null;

function stopTyping() {
  clearTimeout(stopTypingTimer);
  if (typingSent && state.activeId) send({ type: "typing", conversation_id: state.activeId, is_typing: false });
  typingSent = false;
}

$("message-input").addEventListener("input", () => {
  $("composer-error").textContent = "";
  if (!state.activeId) return;
  if (!typingSent) {
    typingSent = send({ type: "typing", conversation_id: state.activeId, is_typing: true });
  }
  clearTimeout(stopTypingTimer);
  stopTypingTimer = setTimeout(stopTyping, 2000);
});

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("message-input");
  const content = input.value.trim();
  if (!content || !state.activeId) return;
  stopTyping();

  // Show the message straight away ("optimistic UI"); swap in the real one when the server confirms.
  const clientId = `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const pending = renderMessage({
    id: null, sender_id: state.me.id, sender_username: state.me.username, sender_is_bot: false,
    content, created_at: new Date().toISOString(),
  }, clientId);
  appendMessage(pending);

  const sent = send({ type: "message", conversation_id: state.activeId, content, client_id: clientId });
  if (!sent) {
    pending.querySelector(".bubble").classList.add("bubble-failed");
    $("composer-error").textContent = "Not sent. You're offline; reconnecting now.";
    return;
  }
  input.value = "";
});

// ---------- rendering ----------

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function findConversation(id) {
  return state.conversations.find((c) => c.id === id);
}

function otherMember(conversation) {
  return conversation.members.find((m) => m.id !== state.me.id) || conversation.members[0];
}

function avatar(user, onDark) {
  const node = el("div", "avatar", user.is_bot ? "AI" : user.username.slice(0, 2));
  node.classList.add(...(user.is_bot ? ["bg-marigold", "text-ink"] : onDark ? ["bg-glacier", "text-pine"] : ["bg-pine", "text-mist"]));
  if (user.online && !user.is_bot) {
    const dot = el("span", "dot");
    dot.title = "Online";
    node.append(dot);
  }
  return node;
}

function renderMessage(message, clientId = null) {
  const mine = message.sender_id === state.me.id;
  const row = el("div", `flex flex-col ${mine ? "items-end" : "items-start"}`);
  if (message.id) row.dataset.messageId = message.id;
  if (clientId) row.dataset.clientId = clientId;

  const conversation = findConversation(message.conversation_id ?? state.activeId);
  const showName = !mine && (message.sender_is_bot || conversation?.members.length > 2);
  if (showName) row.append(el("span", "mb-1 px-1 text-xs font-bold text-slate", message.sender_is_bot ? "@ai" : message.sender_username));

  const bubble = el("p", "bubble", message.content);
  bubble.classList.add(mine ? "bubble-me" : message.sender_is_bot ? "bubble-ai" : "bubble-them");
  if (clientId && !message.id) bubble.classList.add("bubble-pending");
  row.append(bubble);
  row.append(el("time", "mt-1 px-1 text-xs text-slate", message.id ? formatTime(message.created_at) : "Sending…"));
  return row;
}

function appendMessage(element) {
  const box = $("messages");
  box.querySelector("#empty-chat")?.remove();
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  box.append(element);
  if (nearBottom || element.dataset.clientId) box.scrollTop = box.scrollHeight;
}

function emptyChatNote() {
  const note = el("p", "mt-10 text-center text-slate", "No messages yet. Say hello.");
  note.id = "empty-chat";
  return note;
}

function renderSidebar() {
  const list = $("conversation-list");
  if (state.conversations.length === 0) {
    list.replaceChildren(el("p", "px-3 py-6 text-sm text-glacier", "No chats yet. Find someone above, or start with @ai."));
    return;
  }
  list.replaceChildren(...state.conversations.map((conversation) => {
    const item = el("button", "side-item");
    item.setAttribute("aria-current", String(conversation.id === state.activeId));
    item.addEventListener("click", () => openConversation(conversation.id));

    const text = el("div", "min-w-0 flex-1");
    text.append(el("p", "truncate font-bold", conversation.title));
    const last = conversation.last_message;
    const preview = last ? `${last.sender_id === state.me.id ? "You: " : ""}${last.content}` : "No messages yet";
    text.append(el("p", "truncate text-sm text-glacier/80", preview));

    item.append(avatar(otherMember(conversation), true), text);
    const unread = state.unread[conversation.id];
    if (unread) item.append(el("span", "rounded-full bg-marigold px-2 py-0.5 text-xs font-bold text-ink", String(unread)));
    return item;
  }));
}

function renderHeader() {
  const conversation = findConversation(state.activeId);
  if (!conversation) return;
  const other = otherMember(conversation);
  $("chat-title").textContent = conversation.title;
  $("chat-avatar").replaceChildren(avatar(other, false));
  let status = "Offline";
  if (other.is_bot) status = "AI assistant";
  else if (other.online) status = "Online";
  else if (other.last_seen_at) status = `Last seen ${new Date(other.last_seen_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
  $("chat-status").textContent = status;
}

function renderTyping() {
  const typing = state.typing[state.activeId];
  $("typing").textContent = !typing ? "" : typing.username === "ai" ? "@ai is thinking…" : `${typing.username} is typing…`;
}

// ---------- search & starting chats ----------

let searchTimer = null;

$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("search").value.trim();
  if (!q) return showSearch(false);
  searchTimer = setTimeout(() => runSearch(q), 250);
});

$("search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    $("search").value = "";
    showSearch(false);
  }
});

function showSearch(visible) {
  $("search-results").classList.toggle("hidden", !visible);
  $("conversation-list").classList.toggle("hidden", visible);
}

async function runSearch(q) {
  const users = await api(`/api/users?q=${encodeURIComponent(q)}`);
  const box = $("search-results");
  if (users.length === 0) {
    box.replaceChildren(el("p", "px-3 py-6 text-sm text-glacier", `Nobody called "${q}" yet.`));
  } else {
    box.replaceChildren(...users.map((user) => {
      const item = el("button", "side-item");
      const text = el("div", "min-w-0 flex-1");
      text.append(el("p", "truncate font-bold", user.username));
      text.append(el("p", "text-sm text-glacier/80", user.is_bot ? "AI assistant" : user.online ? "Online" : "Start a chat"));
      item.append(avatar(user, true), text);
      item.addEventListener("click", () => startChat(user.username));
      return item;
    }));
  }
  showSearch(true);
}

async function startChat(username) {
  try {
    const conversation = await api("/api/conversations", { method: "POST", body: JSON.stringify({ username }) });
    if (!findConversation(conversation.id)) state.conversations.unshift(conversation);
    $("search").value = "";
    showSearch(false);
    await openConversation(conversation.id);
  } catch (error) {
    $("composer-error").textContent = error.message;
  }
}

// ---------- layout (phones show one pane at a time) ----------

function setView(view) {
  $("app").dataset.view = view;
  $("sidebar").classList.toggle("max-md:hidden", view === "chat");
  $("chat-pane").classList.toggle("hidden", view !== "chat");
  $("chat-pane").classList.toggle("flex", view === "chat");
}

$("back").addEventListener("click", () => {
  stopTyping();
  setView("list");
});

$("chat-with-ai").addEventListener("click", () => startChat("ai"));

$("logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/";
});

// ---------- start ----------

(async function init() {
  state.me = await api("/api/auth/me");
  $("me-name").textContent = `@${state.me.username}`;
  await loadConversations();
  connect();
})();
