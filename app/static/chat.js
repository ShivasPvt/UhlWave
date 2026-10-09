// Everything the chat page does in the browser.
// Rule of thumb: user-written text is always inserted with textContent, never
// innerHTML, so nobody can inject HTML or scripts by sending a message.
// groups.js, settings.js, extras.js, picker.js, location.js, calls.js and
// appearance.js are loaded after this file and use its helpers. They plug in
// through the small `hooks` object below.

const $ = (id) => document.getElementById(id);

const state = {
  me: null,
  conversations: [],   // newest activity first; each has .unread and .read_state from the server
  activeId: null,
  messages: new Map(), // id -> message, for the open chat
  typing: {},          // conversation id -> { username, timer }
  hasMore: false,      // can we load older messages in the open chat?
  editing: null,       // id of the message being edited, if any
  lastReadSent: {},    // conversation id -> newest message id we've reported as read
  filter: "all",       // which chats the sidebar shows: all, unread, groups, requests, archived, locked, or cat:<id>
  categories: [],      // your own chat categories
  requests: { incoming: [], outgoing: [] }, // chat requests (private accounts)
  locked: { has_pin: false, count: 0, open: false },
};

// Other files add functions here to run at certain moments.
const hooks = {
  init: [], conversationsLoaded: [], headerRendered: [], chatOpened: [], event: [],
  // Async functions that prepare a message before it's drawn (e2ee.js decrypts secret chats here).
  transform: [],
  // Async function that prepares text before it's sent: (conversation, text) => text to send.
  outgoing: [],
  // (row, bubble, message) after a message is drawn: double-click, swipe, right-click (interact.js).
  messageRendered: [],
  // A new message arrived live (not from history): confetti, the jump button's counter.
  messageArrived: [],
};

async function transformMessages(list) {
  let out = list;
  for (const fn of hooks.transform) out = await Promise.all(out.map((m) => fn(m)));
  return out;
}

async function prepareOutgoing(conversation, text) {
  let out = text;
  for (const fn of hooks.outgoing) out = await fn(conversation, out);
  return out;
}

async function transformConversations(list) {
  for (const c of list) {
    if (c.last_message) [c.last_message] = await transformMessages([c.last_message]);
    if (c.pinned?.length) c.pinned = await transformMessages(c.pinned);
  }
  return list;
}
function runHooks(name, ...args) {
  for (const fn of hooks[name]) {
    try {
      fn(...args);
    } catch (error) {
      console.error(error);
    }
  }
}

// A short message at the bottom of the screen.
let appToastTimer = null;
function toast(text) {
  const box = $("app-toast");
  box.textContent = text;
  box.classList.add("toast--show");
  clearTimeout(appToastTimer);
  appToastTimer = setTimeout(() => box.classList.remove("toast--show"), 2600);
}

const REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
const MAX_UPLOAD = 5 * 1024 * 1024;

// Settings that only apply to this browser (the Settings panel changes them).
const localPrefs = {
  hidePreviews: false, notify: false,
  // Media auto-download (Settings): load these straight away, or wait for a tap.
  autoPhotos: true, autoGifs: true, autoVoice: true, autoDocs: false,
  sounds: true,
};
try {
  Object.assign(localPrefs, JSON.parse(localStorage.getItem("uhlwave-prefs") || "{}"));
} catch {}

function saveLocalPrefs() {
  try {
    localStorage.setItem("uhlwave-prefs", JSON.stringify(localPrefs));
  } catch {}
}

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

function showError(text) {
  $("composer-error").textContent = text;
}

// ---------- sounds (Settings → "Sounds") ----------
// Tiny tones made with the Web Audio API, so there are no sound files to load.

let audioContext = null;
function playSound(kind) {
  if (!localPrefs.sounds) return;
  try {
    audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
    const now = audioContext.currentTime;
    const notes = kind === "send" ? [[520, 0, 0.07], [780, 0.05, 0.08]] : [[880, 0, 0.09], [660, 0.09, 0.14]];
    for (const [freq, at, length] of notes) {
      const osc = audioContext.createOscillator();
      const gain = audioContext.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, now + at);
      gain.gain.exponentialRampToValueAtTime(kind === "send" ? 0.05 : 0.08, now + at + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + at + length);
      osc.connect(gain).connect(audioContext.destination);
      osc.start(now + at);
      osc.stop(now + at + length + 0.02);
    }
  } catch {}
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
    markRead();
  });

  // Events are handled one at a time, in order, even when one needs a moment
  // (decrypting a secret message), so messages never appear out of order.
  socket.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    eventChain = eventChain.then(() => handleEvent(data)).catch((error) => console.error(error));
  });

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

let eventChain = Promise.resolve();

async function handleEvent(event) {
  if (event.message) [event.message] = await transformMessages([event.message]);
  if (event.type === "pins_changed") event.pinned = await transformMessages(event.pinned);
  if (event.type === "message") onMessage(event.message, event.client_id);
  else if (event.type === "message_updated") onMessageUpdated(event.message);
  else if (event.type === "typing") onTyping(event);
  else if (event.type === "read") onRead(event);
  else if (event.type === "presence") onPresence(event);
  else if (event.type === "conversations_changed") onConversationsChanged();
  else if (event.type === "error") onServerError(event);
  runHooks("event", event); // pins, requests and calls are handled in extras.js and calls.js
}

// ---------- incoming events ----------

function onMessage(message, clientId) {
  const conversation = findConversation(message.conversation_id);
  if (!conversation) {
    // Someone started a new chat or added us to a group. Reload the list to get it.
    // If it's still not there, it's one of our locked chats: notify without saying who.
    loadConversations().then(() => {
      if (!findConversation(message.conversation_id)) maybeNotify(message, null);
    });
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
    markRead();
  }
  if (message.sender_id !== state.me.id && message.type !== "system" && !(message.conversation_id === state.activeId && pageVisible())) {
    conversation.unread = (conversation.unread || 0) + 1;
  }
  renderSidebar();
  maybeNotify(message, conversation);
  autoSaveDocument(message);
  if (message.sender_id !== state.me.id && message.type !== "system") playSound("receive");
  runHooks("messageArrived", message);
}

// "Save documents automatically" (Settings): download files people send, as they arrive.
function autoSaveDocument(message) {
  if (!localPrefs.autoDocs || message.type !== "file" || !message.attachment || message.sender_id === state.me.id) return;
  const link = el("a");
  link.href = message.attachment.url;
  link.download = message.attachment.filename;
  document.body.append(link);
  link.click();
  link.remove();
}

function onMessageUpdated(message) {
  const conversation = findConversation(message.conversation_id);
  if (!conversation) return;
  if (conversation.last_message?.id === message.id) {
    conversation.last_message = message;
    renderSidebar();
  }
  if (message.conversation_id !== state.activeId) return;
  const row = document.querySelector(`[data-message-id="${message.id}"]`);
  if (row) row.replaceWith(renderMessage(message));
  if (state.editing === message.id && message.deleted) cancelEdit();
}

function onRead({ conversation_id, user_id, message_id }) {
  const conversation = findConversation(conversation_id);
  if (!conversation) return;
  conversation.read_state ||= {};
  conversation.read_state[user_id] = Math.max(conversation.read_state[user_id] || 0, message_id);
  if (conversation_id === state.activeId) updateTicks();
}

async function onConversationsChanged() {
  await loadConversations();
  // Removed from the group we're looking at? Close it.
  if (state.activeId && !findConversation(state.activeId)) closeConversation();
}

// Desktop notification for messages you aren't looking at (if switched on in Settings).
function maybeNotify(message, conversation) {
  if (!localPrefs.notify || message.sender_id === state.me.id || message.type === "system") return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (pageVisible() && message.conversation_id === state.activeId) return;
  if (!conversation || conversation.locked) {
    // Locked chats never show who wrote or what they said.
    new Notification("UhlWave", { body: "New message", tag: "locked" });
    return;
  }
  const body = localPrefs.hidePreviews ? "New message" : describe(message).slice(0, 140);
  const from = message.sender_is_bot ? "@ai" : message.sender_username;
  const title = conversation.type === "group" ? `${from} in ${conversation.title}` : from;
  const note = new Notification(title, { body, tag: `chat-${conversation.id}` });
  note.onclick = () => {
    window.focus();
    openConversation(conversation.id);
    note.close();
  };
}

function onTyping({ conversation_id, username, is_typing, drawing }) {
  clearTimeout(state.typing[conversation_id]?.timer);
  if (is_typing) {
    // Safety net: hide the indicator if the "stopped typing" event never arrives.
    // Drawing a picture can take a while, so that one waits longer.
    const timer = setTimeout(() => clearTyping(conversation_id), drawing ? 120000 : 6000);
    state.typing[conversation_id] = { username, timer, drawing };
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
  if (client_id) document.querySelector(`[data-client-id="${CSS.escape(client_id)}"] .bubble`)?.classList.add("bubble--failed");
  showError(detail);
}

// ---------- read receipts ----------

function pageVisible() {
  return document.visibilityState === "visible";
}

// Tell the server we've seen everything in the open chat (if the tab is actually visible).
function markRead() {
  const id = state.activeId;
  if (!id || !pageVisible()) return;
  const ids = [...state.messages.keys()];
  if (ids.length === 0) return;
  const newest = Math.max(...ids);
  if ((state.lastReadSent[id] || 0) >= newest) return;
  if (send({ type: "read", conversation_id: id, message_id: newest })) {
    state.lastReadSent[id] = newest;
    const conversation = findConversation(id);
    if (conversation && conversation.unread) {
      conversation.unread = 0;
      renderSidebar();
    }
  }
}

document.addEventListener("visibilitychange", markRead);
window.addEventListener("focus", markRead);

// ✓ sent, ✓✓ seen (by everyone, or "Seen by 2" in a group).
function receipt(message, conversation) {
  if (!conversation || message.sender_id !== state.me.id || !message.id || message.deleted || message.type === "system") return null;
  const others = conversation.members.filter((m) => m.id !== state.me.id && !m.is_bot);
  if (others.length === 0) return null;
  const readers = others.filter((m) => (conversation.read_state?.[m.id] || 0) >= message.id);
  if (readers.length === 0) return { text: "✓", title: "Sent", seen: false };
  if (readers.length === others.length) {
    return { text: "✓✓", title: others.length > 1 ? "Seen by everyone" : `Seen by ${readers[0].username}`, seen: true };
  }
  return { text: `✓✓ ${readers.length}`, title: `Seen by ${readers.map((r) => r.username).join(", ")}`, seen: false };
}

function updateTicks() {
  const conversation = findConversation(state.activeId);
  document.querySelectorAll("#messages [data-message-id]").forEach((row) => {
    const message = state.messages.get(Number(row.dataset.messageId));
    const tick = row.querySelector(".tick");
    if (!message || !tick) return;
    paintTick(tick, receipt(message, conversation));
  });
}

function paintTick(node, r) {
  node.textContent = r ? r.text : "";
  node.title = r ? r.title : "";
  node.classList.toggle("tick--seen", Boolean(r?.seen));
}

// ---------- loading data ----------

async function loadConversations() {
  const list = await api("/api/conversations");
  state.conversations = list; // set first: decrypting needs to look chats up
  await transformConversations(list);
  renderSidebar();
  renderHeader();
  if (state.activeId) updateTicks();
  runHooks("conversationsLoaded");
}

async function resync() {
  await loadConversations();
  if (state.activeId) await loadHistory();
}

async function openConversation(id) {
  if (state.editing) cancelEdit();
  cancelReply();
  state.activeId = id;
  state.messages = new Map();
  $("empty-state").classList.add("hidden");
  $("chat-view").classList.remove("hidden");
  showError("");
  setView("chat");
  renderSidebar();
  renderHeader();
  renderTyping();
  await loadHistory();
  markRead();
  runHooks("chatOpened", id);
  $("message-input").focus();
}

function closeConversation() {
  state.activeId = null;
  state.messages = new Map();
  $("chat-view").classList.add("hidden");
  $("empty-state").classList.remove("hidden");
  setView("list");
  renderSidebar();
}

async function loadHistory(beforeId = null) {
  const id = state.activeId;
  const query = beforeId ? `?before=${beforeId}` : "";
  let messages;
  try {
    messages = await transformMessages(await api(`/api/conversations/${id}/messages${query}`));
  } catch (error) {
    // e.g. the locked folder timed out: go back to the list.
    showError(error.message);
    return;
  }
  if (id !== state.activeId) return; // user switched chats while this was loading

  const box = $("messages");
  state.hasMore = messages.length === 50;

  if (beforeId) {
    // Prepend older messages without making the view jump.
    const previousHeight = box.scrollHeight;
    box.querySelector("#load-older")?.remove();
    box.prepend(...messages.map((m) => renderMessage(m)));
    addLoadOlderButton();
    box.scrollTop = box.scrollHeight - previousHeight;
  } else {
    state.messages = new Map();
    box.replaceChildren(...messages.map((m) => renderMessage(m)));
    if (messages.length === 0) box.append(emptyChatNote());
    addLoadOlderButton();
    box.scrollTop = box.scrollHeight;
    // A quick fade when a chat opens (single messages slide in instead; see appendMessage).
    box.classList.remove("messages--fresh");
    void box.offsetWidth; // restart the animation
    box.classList.add("messages--fresh");
  }
}

function addLoadOlderButton() {
  if (!state.hasMore) return;
  const first = $("messages").querySelector("[data-message-id]");
  const button = el("button", "load-older", "Load earlier messages");
  button.id = "load-older";
  button.addEventListener("click", () => loadHistory(Number(first.dataset.messageId)));
  $("messages").prepend(button);
}

// Open a chat and scroll to one message (used by search results).
async function jumpToMessage(conversationId, messageId) {
  await openConversation(conversationId);
  for (let i = 0; i < 20 && !document.querySelector(`[data-message-id="${messageId}"]`) && state.hasMore; i++) {
    const first = $("messages").querySelector("[data-message-id]");
    await loadHistory(Number(first.dataset.messageId));
  }
  const row = document.querySelector(`[data-message-id="${messageId}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 2000);
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
  showError("");
  if (!state.activeId || state.editing) return;
  if (!typingSent) {
    typingSent = send({ type: "typing", conversation_id: state.activeId, is_typing: true });
  }
  clearTimeout(stopTypingTimer);
  stopTypingTimer = setTimeout(stopTyping, 2000);
});

$("message-input").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.editing) cancelEdit();
  if (event.key === "Escape" && state.replyTo) cancelReply();
});

function newClientId() {
  return `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

function pendingMessage(fields) {
  return {
    id: null, sender_id: state.me.id, sender_username: state.me.username, sender_is_bot: false,
    type: "text", content: "", attachment: null, reactions: [], deleted: false, reply_to: null, poll: null,
    created_at: new Date().toISOString(), conversation_id: state.activeId, ...fields,
  };
}

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("message-input");
  const content = input.value.trim();
  if (!content || !state.activeId) return;

  const conversation = findConversation(state.activeId);
  let outgoing;
  try {
    outgoing = await prepareOutgoing(conversation, content); // encrypted in secret chats
  } catch (error) {
    return showError(error.message);
  }

  if (state.editing) {
    const id = state.editing;
    try {
      await api(`/api/messages/${id}`, { method: "PATCH", body: JSON.stringify({ content: outgoing }) });
      cancelEdit();
    } catch (error) {
      showError(error.message);
    }
    return;
  }

  stopTyping();
  // Show the message straight away ("optimistic UI"); swap in the real one when the server confirms.
  const clientId = newClientId();
  const replyTo = state.replyTo;
  const pending = renderMessage(pendingMessage({ content, reply_to: replyTo ? replyPreview(replyTo) : null }), clientId);
  appendMessage(pending);

  const sent = send({ type: "message", conversation_id: state.activeId, content: outgoing, client_id: clientId, reply_to_id: replyTo?.id ?? null });
  if (!sent) {
    pending.querySelector(".bubble").classList.add("bubble--failed");
    showError("Not sent. You're offline; reconnecting now.");
    return;
  }
  input.value = "";
  cancelReply();
  playSound("send");
});

// ---------- replying ----------

function replyPreview(message) {
  return {
    id: message.id, sender_id: message.sender_id, sender_username: message.sender_username,
    sender_is_bot: message.sender_is_bot, type: message.type, content: message.content, deleted: message.deleted,
  };
}

function startReply(message) {
  if (state.editing) cancelEdit();
  state.replyTo = message;
  const who = message.sender_id === state.me.id ? "yourself" : message.sender_is_bot ? "@ai" : message.sender_username;
  $("reply-who").textContent = `Replying to ${who}`;
  $("reply-text").textContent = describe(message);
  $("reply-banner").classList.remove("hidden");
  $("message-input").focus();
}

function cancelReply() {
  state.replyTo = null;
  $("reply-banner").classList.add("hidden");
}

$("cancel-reply").addEventListener("click", cancelReply);

// The quoted message shown above a reply. Tap it to jump to the original.
function quoteBlock(reply) {
  const quote = el("button", "quote");
  quote.type = "button";
  const who = reply.sender_id === state.me.id ? "You" : reply.sender_is_bot ? "@ai" : reply.sender_username;
  quote.append(el("span", "quote__who", who), el("span", "quote__text", reply.deleted ? "Message deleted" : describe(reply)));
  quote.title = "Go to the original message";
  quote.addEventListener("click", (event) => {
    event.stopPropagation();
    if (reply.id && !reply.deleted) window.scrollToMessage?.(reply.id);
  });
  return quote;
}

// ---------- editing ----------

function startEdit(message) {
  cancelReply();
  state.editing = message.id;
  $("edit-banner").classList.remove("hidden");
  const input = $("message-input");
  input.value = message.content;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function cancelEdit() {
  state.editing = null;
  $("edit-banner").classList.add("hidden");
  $("message-input").value = "";
}

$("cancel-edit").addEventListener("click", cancelEdit);

// ---------- attachments: files, images, pasted screenshots ----------

async function uploadFile(file, kind = "auto", filename = file.name) {
  const conversationId = state.activeId;
  if (!conversationId) return;
  if (file.size > MAX_UPLOAD) return showError("Files can be up to 5 MB.");
  if (file.size === 0) return showError("That file is empty.");
  showError("");

  const clientId = newClientId();
  const label = kind === "voice" ? "Sending voice note…" : `Uploading ${filename}…`;
  const pending = renderMessage(pendingMessage({ content: label }), clientId);
  appendMessage(pending);

  try {
    const res = await fetch(
      `/api/conversations/${conversationId}/attachments?filename=${encodeURIComponent(filename)}&kind=${kind}`,
      { method: "POST", body: file, headers: { "Content-Type": file.type || "application/octet-stream", "X-Client-Id": clientId } },
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : "Upload failed.");
    }
    // The real message arrives over the WebSocket and replaces the pending one.
    // If the socket is down, put it in place ourselves.
    const message = await res.json();
    const stillPending = document.querySelector(`[data-client-id="${CSS.escape(clientId)}"]`);
    if (stillPending && conversationId === state.activeId) stillPending.replaceWith(renderMessage(message));
  } catch (error) {
    pending.querySelector(".bubble")?.classList.add("bubble--failed");
    showError(error.message);
  }
}

// The paperclip opens a small menu (file, location, poll) in interact.js.
$("file-input").addEventListener("change", () => {
  for (const file of $("file-input").files) uploadFile(file);
  $("file-input").value = "";
});

$("message-input").addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])];
  if (files.length === 0) return;
  event.preventDefault();
  files.forEach((file) => uploadFile(file, "auto", file.name || "pasted-image.png"));
});

// Drag a file onto the chat to send it.
$("chat-view").addEventListener("dragover", (event) => {
  if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
});
$("chat-view").addEventListener("drop", (event) => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault();
  [...event.dataTransfer.files].forEach((file) => uploadFile(file));
});

// ---------- sharing your location ----------
// The pin button opens the place picker in location.js, which falls back to
// searching or tapping the map when the browser can't find you.

// ---------- voice notes ----------

const recorder = { media: null, chunks: [], started: 0, timer: null, cancelled: false };

function pickAudioType() {
  const options = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
  return options.find((t) => window.MediaRecorder?.isTypeSupported?.(t)) || "";
}

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    return showError("Voice notes aren't supported in this browser.");
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    return showError("Microphone access was blocked. Allow it in your browser to record voice notes.");
  }
  const mimeType = pickAudioType();
  const media = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  Object.assign(recorder, { media, chunks: [], started: Date.now(), cancelled: false });
  media.addEventListener("dataavailable", (e) => e.data.size && recorder.chunks.push(e.data));
  media.addEventListener("stop", () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(recorder.timer);
    setRecordingUI(false);
    if (recorder.cancelled || recorder.chunks.length === 0) return;
    const type = (media.mimeType || "audio/webm").split(";")[0];
    const blob = new Blob(recorder.chunks, { type });
    const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
    uploadFile(blob, "voice", `voice-note.${ext}`);
  });
  media.start();
  setRecordingUI(true);
  recorder.timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - recorder.started) / 1000);
    $("record-time").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    if (seconds >= 120) stopRecording(); // two minutes max
  }, 250);
}

function stopRecording(cancel = false) {
  recorder.cancelled = cancel;
  if (recorder.media?.state === "recording") recorder.media.stop();
}

function setRecordingUI(on) {
  $("recording-bar").classList.toggle("hidden", !on);
  $("composer").classList.toggle("hidden", on);
  $("record-time").textContent = "0:00";
}

$("mic-btn").addEventListener("click", startRecording);
$("record-send").addEventListener("click", () => stopRecording(false));
$("record-cancel").addEventListener("click", () => stopRecording(true));

// ---------- message actions: react, edit, delete ----------

async function react(message, emoji) {
  try {
    await api(`/api/messages/${message.id}/reactions`, { method: "POST", body: JSON.stringify({ emoji }) });
  } catch (error) {
    showError(error.message);
  }
}

async function deleteMessage(message) {
  try {
    await api(`/api/messages/${message.id}`, { method: "DELETE" });
  } catch (error) {
    showError(error.message);
  }
}

function actionBar(message, mine) {
  const bar = el("div", "msg-actions");
  for (const emoji of REACTIONS) {
    const b = el("button", "act-emoji", emoji);
    b.type = "button";
    b.title = `React ${emoji}`;
    b.addEventListener("click", () => react(message, emoji));
    bar.append(b);
  }
  if (mine && message.type === "text") {
    const edit = el("button", "act-btn", "Edit");
    edit.type = "button";
    edit.addEventListener("click", () => startEdit(message));
    bar.append(edit);
  }
  const reply = el("button", "act-btn", "Reply");
  reply.type = "button";
  reply.addEventListener("click", () => startReply(message));
  bar.append(reply);
  const pin = el("button", "act-btn", message.pinned ? "Unpin" : "Pin");
  pin.type = "button";
  pin.addEventListener("click", () => window.togglePin?.(message));
  bar.append(pin);
  if (mine) {
    const del = el("button", "act-btn act-btn--danger", "Delete");
    del.type = "button";
    // Two clicks: the first asks "Delete for everyone?", the second does it.
    del.addEventListener("click", () => {
      if (del.dataset.armed) return deleteMessage(message);
      del.dataset.armed = "1";
      del.textContent = "Delete for everyone?";
      setTimeout(() => {
        delete del.dataset.armed;
        del.textContent = "Delete";
      }, 3000);
    });
    bar.append(del);
  }
  return bar;
}

// ---------- rendering ----------

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function findConversation(id) {
  return state.conversations.find((c) => c.id === id);
}

function otherMember(conversation) {
  return conversation.members.find((m) => m.id !== state.me.id) || conversation.members[0];
}

// One-line summary of a message, for the chat list and notifications.
function describe(message) {
  if (message.deleted) return "Message deleted";
  if (typeof message.content === "string" && message.content.startsWith("e2e1:")) return "🔒 Encrypted message";
  if (message.type === "poll") return `📊 ${message.content}`;
  if (message.type === "image") return "📷 Photo";
  if (message.type === "voice") return "🎤 Voice note";
  if (message.type === "file") return `📎 ${message.attachment?.filename || "File"}`;
  if (message.type === "location") return `📍 ${message.content}`;
  if (message.type === "gif") return "GIF";
  if (message.type === "sticker") return message.media_url ? "Sticker" : `${message.content} Sticker`;
  if (message.type === "call") return `📞 ${message.content}`;
  return message.content;
}

// Each person gets their own colour (av-0 … av-5 in styles.css), picked from
// their username so it never changes.
function avatarColor(username) {
  let hash = 0;
  for (const ch of username) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return `av-${hash % 6}`;
}

function avatar(user, small = false) {
  const node = el("div", "avatar", user.is_bot ? "AI" : user.username.slice(0, 2));
  node.classList.add(user.is_bot ? "avatar--ai" : avatarColor(user.username));
  if (small === true) node.classList.add("avatar--sm");
  if (user.avatar_url) {
    node.textContent = "";
    node.classList.add("avatar--photo");
    const img = el("img");
    img.src = user.avatar_url;
    img.alt = "";
    img.loading = "lazy";
    node.append(img);
  }
  if (user.online && !user.is_bot) {
    const dot = el("span", "avatar__dot");
    dot.title = "Online";
    node.append(dot);
  }
  return node;
}

function conversationAvatar(conversation) {
  if (conversation.type !== "group") return avatar(otherMember(conversation));
  const node = avatar({ username: conversation.title || "group", is_bot: false, online: false, avatar_url: conversation.avatar_url });
  node.classList.add("avatar--group");
  return node;
}

function messageBody(message, mine) {
  const kind = mine ? "bubble--mine" : message.sender_is_bot ? "bubble--ai" : "bubble--theirs";
  if (message.deleted) {
    return el("p", `bubble ${kind} bubble--deleted`, mine ? "You deleted this message" : "This message was deleted");
  }
  const a = message.attachment;
  if (message.type === "image" && a) {
    if (!localPrefs.autoPhotos && !loadedMedia.has(message.id) && !mine) {
      return tapToLoad(message, `📷 Photo · ${formatSize(a.size)}`, kind);
    }
    const link = el("a", `bubble ${kind} bubble--media`);
    link.href = a.url;
    link.target = "_blank";
    link.rel = "noopener";
    const img = el("img");
    img.src = a.url;
    img.alt = message.sender_is_bot ? message.content : a.filename;
    img.loading = "lazy";
    link.append(img);
    // Pictures @ai draws carry the description they were made from.
    if (message.sender_is_bot && message.content && message.content !== a.filename) {
      link.append(el("span", "media-caption", message.content));
    }
    return link;
  }
  if (message.type === "voice" && a) {
    const box = el("div", `bubble ${kind} bubble--voice`);
    const audio = el("audio");
    audio.controls = true;
    audio.preload = localPrefs.autoVoice || mine ? "metadata" : "none";
    audio.src = a.url;
    box.append(audio);
    return box;
  }
  if (message.type === "gif" || message.type === "sticker") {
    if (!message.media_url) return el("p", "bubble bubble--sticker-emoji", message.content); // built-in sticker
    if (!localPrefs.autoGifs && !loadedMedia.has(message.id) && !mine) {
      return tapToLoad(message, message.type === "gif" ? "GIF · Tap to play" : "Sticker · Tap to show", kind);
    }
    const box = el("div", message.type === "gif" ? `bubble ${kind} bubble--media bubble--gif` : "bubble bubble--sticker");
    const img = el("img");
    img.src = message.media_url;
    img.alt = message.content;
    img.loading = "lazy";
    box.append(img);
    if (message.type === "gif") box.append(el("span", "gif-badge", "GIF"));
    return box;
  }
  if (message.type === "poll" && message.poll && window.renderPoll) return window.renderPoll(message, mine, kind);
  if (message.type === "call") {
    const missed = message.content.startsWith("Missed");
    const box = el("p", `bubble ${kind} bubble--call${missed ? " bubble--missed" : ""}`);
    const icon = el("span", "call-icon");
    icon.innerHTML = message.content.toLowerCase().includes("video") ? VIDEO_ICON : PHONE_ICON; // fixed SVG
    box.append(icon, el("span", "", message.content));
    return box;
  }
  if (message.type === "file" && a) {
    const link = el("a", `bubble ${kind} bubble--file`);
    link.href = a.url;
    link.download = a.filename;
    link.title = `Download ${a.filename}`;
    const icon = el("span", "file-icon");
    icon.innerHTML = FILE_ICON; // a fixed SVG from this file, never user text
    const text = el("span");
    text.append(el("span", "file-name", a.filename));
    text.append(el("span", "file-size", formatSize(a.size)));
    link.append(icon, text);
    return link;
  }
  if (message.type === "location" && message.location) {
    const loc = message.location;
    const link = el("a", `bubble ${kind} bubble--location`);
    link.href = loc.map_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = "Open in OpenStreetMap";
    const map = el("span", "loc-map");
    map.append(mapTiles(loc.latitude, loc.longitude));
    const pin = el("span", "loc-pin");
    pin.innerHTML = PIN_ICON; // fixed SVG, never user text
    map.append(pin);
    const text = el("span", "loc-text");
    text.append(el("span", "loc-name", message.content));
    text.append(el("span", "loc-coords", `${loc.latitude.toFixed(4)}, ${loc.longitude.toFixed(4)}`));
    link.append(map, text);
    return link;
  }
  return el("p", `bubble ${kind}`, message.content);
}

// Media the person tapped "load" on (when auto-download is off), so redraws keep it.
const loadedMedia = new Set();

function tapToLoad(message, label, kind) {
  const button = el("button", `bubble ${kind} bubble--tap`, label);
  button.type = "button";
  button.title = "Tap to load";
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    loadedMedia.add(message.id);
    button.closest(".msg")?.replaceWith(renderMessage(message));
  });
  return button;
}

// A little map for shared locations: OpenStreetMap tiles, centred on the spot.
function mapTiles(lat, lon, width = 240, height = 120, zoom = 15) {
  const box = el("span", "loc-tiles");
  const n = 2 ** zoom;
  const px = ((lon + 180) / 360) * n * 256;
  const rad = (lat * Math.PI) / 180;
  const py = ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * n * 256;
  const left = px - width / 2;
  const top = py - height / 2;
  for (let tx = Math.floor(left / 256); tx <= Math.floor((left + width) / 256); tx++) {
    for (let ty = Math.floor(top / 256); ty <= Math.floor((top + height) / 256); ty++) {
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      img.src = `https://tile.openstreetmap.org/${zoom}/${((tx % n) + n) % n}/${ty}.png`;
      img.style.left = `${tx * 256 - left}px`;
      img.style.top = `${ty * 256 - top}px`;
      img.addEventListener("error", () => img.remove());
      box.append(img);
    }
  }
  return box;
}

const PHONE_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M20 15.5c-1.25 0-2.45-.2-3.57-.57a1.02 1.02 0 0 0-1.02.24l-2.2 2.2a15.05 15.05 0 0 1-6.59-6.59l2.2-2.21a.96.96 0 0 0 .25-1A11.36 11.36 0 0 1 8.5 4c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1 0 9.39 7.61 17 17 17 .55 0 1-.45 1-1v-3.5c0-.55-.45-1-1-1z"/></svg>';
const VIDEO_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></svg>';

const PIN_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2a8 8 0 0 0-8 8c0 6 8 12 8 12s8-6 8-12a8 8 0 0 0-8-8zm0 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/></svg>';

const FILE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M12 18v-6"/><path d="m9 15 3 3 3-3"/></svg>';

function reactionChips(message) {
  if (!message.reactions?.length) return null;
  const box = el("div", "reactions");
  for (const r of message.reactions) {
    const mineToo = r.user_ids.includes(state.me.id);
    const chip = el("button", `reaction${mineToo ? " reaction--mine" : ""}`, `${r.emoji} ${r.user_ids.length}`);
    chip.type = "button";
    const conversation = findConversation(message.conversation_id);
    const names = r.user_ids.map((id) => (id === state.me.id ? "you" : conversation?.members.find((m) => m.id === id)?.username || "someone"));
    chip.title = `${names.join(", ")} reacted ${r.emoji}`;
    chip.addEventListener("click", () => react(message, r.emoji));
    box.append(chip);
  }
  return box;
}

function renderMessage(message, clientId = null) {
  if (message.id) state.messages.set(message.id, message);

  if (message.type === "system") {
    const row = el("p", "system-line", message.content);
    row.dataset.messageId = message.id;
    return row;
  }

  const mine = message.sender_id === state.me.id;
  const row = el("div", `msg ${mine ? "msg--mine" : "msg--theirs"}`);
  if (message.id) row.dataset.messageId = message.id;
  if (clientId) row.dataset.clientId = clientId;

  const conversation = findConversation(message.conversation_id ?? state.activeId);
  const showName = !mine && (message.sender_is_bot || conversation?.type === "group");
  if (showName) {
    const name = el("button", "msg__name", message.sender_is_bot ? "@ai" : message.sender_username);
    name.type = "button";
    if (!message.sender_is_bot) name.addEventListener("click", () => window.openProfile?.(message.sender_username));
    // Group member tags, like "Class rep", next to the name.
    const tag = conversation?.members.find((m) => m.id === message.sender_id)?.tag;
    if (tag) name.append(el("span", "member-tag", tag));
    row.append(name);
  }
  if (message.pinned) row.classList.add("msg--pinned");
  if (message.reply_to) row.append(quoteBlock(message.reply_to));

  const body = messageBody(message, mine);
  if (clientId && !message.id) body.classList.add("bubble--pending");
  row.append(body);
  const chips = reactionChips(message);
  if (chips) row.append(chips);

  // Under the bubble: ✓✓, the time, "Edited", and the disappearing timer.
  const meta = el("div", "msg__meta");
  const tick = el("span", "tick");
  paintTick(tick, receipt(message, conversation));
  meta.append(tick, el("span", "", message.id ? formatTime(message.created_at) : "Sending…"));
  if (message.edited_at && !message.deleted) meta.append(el("span", "", "Edited"));
  if (message.expires_at) {
    const seconds = (new Date(message.expires_at) - new Date(message.created_at)) / 1000;
    const timer = el("span", "", `Disappears after ${durationLabel(seconds)}`);
    timer.title = new Date(message.expires_at).toLocaleString();
    meta.append(timer);
    scheduleDisappear(row, message);
  }
  // The toolbar (react / edit / delete) floats beside the time when you hover or tap.
  if (message.id && !message.deleted && !conversation?.blocked_by_me) meta.append(actionBar(message, mine));
  row.append(meta);

  // On touch screens there's no hover: tap a bubble to show its actions.
  body.addEventListener("click", (event) => {
    if (event.target.closest("a, audio, button")) return;
    document.querySelectorAll(".msg.show-actions").forEach((r) => r !== row && r.classList.remove("show-actions"));
    row.classList.toggle("show-actions");
  });
  if (message.id && !message.deleted) runHooks("messageRendered", row, body, message);
  return row;
}

function durationLabel(seconds) {
  if (seconds >= 6 * 86400) return `${Math.round(seconds / (7 * 86400))} week`;
  if (seconds >= 86400) return `${Math.round(seconds / 86400)} day`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} hour`;
  return `${Math.max(1, Math.round(seconds / 60))} min`;
}

// Disappearing messages: take the bubble off screen the moment its time is up.
function scheduleDisappear(row, message) {
  const wait = new Date(message.expires_at) - Date.now();
  if (wait > 2 ** 31 - 1) return; // too far ahead for setTimeout; it'll be gone on a later visit
  setTimeout(() => {
    row.remove();
    state.messages.delete(message.id);
    const conversation = findConversation(message.conversation_id);
    if (conversation?.last_message?.id === message.id) loadConversations();
  }, Math.max(0, wait));
}

function appendMessage(element) {
  const box = $("messages");
  box.querySelector("#empty-chat")?.remove();
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  element.classList.add("msg--enter"); // slide in from the sender's side
  box.append(element);
  if (nearBottom || element.dataset.clientId) box.scrollTop = box.scrollHeight;
}

function emptyChatNote() {
  const note = el("p", "empty-chat", "No messages yet. Say hello.");
  note.id = "empty-chat";
  return note;
}

// Which chats the current filter shows. Archived and locked chats only show in their own folders.
function visibleConversations() {
  const f = state.filter;
  return state.conversations.filter((c) => {
    if (f === "locked") return c.locked;
    if (c.locked) return false;
    if (f === "archived") return c.archived;
    if (c.archived) return false;
    if (f === "unread") return c.unread > 0;
    if (f === "groups") return c.type === "group";
    if (f.startsWith("cat:")) return c.category_id === Number(f.slice(4));
    return true;
  });
}

const EMPTY_FILTER = {
  all: "No chats yet. Search for someone, start a group, or chat with @ai.",
  unread: "You're all caught up.",
  groups: "No groups yet. Tap Group to start one.",
  archived: "No archived chats. Archive one from its ⋮ menu to tidy your list.",
  locked: "No locked chats. Lock one from its ⋮ menu.",
};

function renderSidebar() {
  const list = $("conversation-list");
  window.renderFilters?.();
  if (state.filter === "requests") return window.renderRequests?.(list);
  const shown = visibleConversations();
  if (shown.length === 0) {
    const text = EMPTY_FILTER[state.filter] || "No chats in this category yet. Add one from a chat's ⋮ menu.";
    list.replaceChildren(el("p", "list-empty", text));
    return;
  }
  list.replaceChildren(...shown.map((conversation) => {
    const item = el("button", "item");
    item.setAttribute("aria-current", String(conversation.id === state.activeId));
    item.addEventListener("click", () => openConversation(conversation.id));

    const text = el("div", "item__text");
    const title = el("p", "item__title", conversation.title);
    if (conversation.type === "secret") title.prepend(el("span", "lock-badge", "🔒"));
    text.append(title);
    const last = conversation.last_message;
    let preview = "No messages yet";
    if (last) {
      const who = last.type === "system" ? "" : last.sender_id === state.me.id ? "You: " : conversation.type === "group" ? `${last.sender_username}: ` : "";
      preview = who + describe(last);
    }
    if (last && (localPrefs.hidePreviews || conversation.locked)) preview = "Message preview hidden";
    text.append(el("p", "item__preview", preview));

    item.append(conversationAvatar(conversation), text);
    if (conversation.unread) item.append(el("span", "badge", String(conversation.unread)));
    return item;
  }));
}

function renderHeader() {
  const conversation = findConversation(state.activeId);
  if (!conversation) return;
  const isGroup = conversation.type === "group";
  const other = otherMember(conversation);
  $("chat-title").textContent = conversation.type === "secret" ? `🔒 ${conversation.title}` : conversation.title;
  $("chat-avatar").replaceChildren(isGroup ? conversationAvatar(conversation) : avatar(other));

  let status = "Offline";
  if (isGroup) {
    const names = conversation.members.map((m) => (m.id === state.me.id ? "you" : m.username));
    status = `${plural(conversation.members.length, "member")}: ${names.join(", ")}`;
  } else if (other.is_bot) status = "AI assistant";
  else if (other.online) status = "Online";
  else if (other.last_seen_at) status = `Last seen ${new Date(other.last_seen_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
  $("chat-status").textContent = status;

  $("group-info").classList.toggle("hidden", !isGroup);
  const canCall = !isGroup && !other.is_bot && !conversation.blocked_by_me;
  $("call-voice").classList.toggle("hidden", !canCall);
  $("call-video").classList.toggle("hidden", !canCall);

  // Block / unblock (one-to-one only), and lock the composer while you've blocked them.
  const blockBtn = $("block-toggle");
  const canBlock = !isGroup && !other.is_bot && other.id !== state.me.id;
  blockBtn.classList.toggle("hidden", !canBlock);
  blockBtn.textContent = conversation.blocked_by_me ? "Unblock" : "Block";
  blockBtn.dataset.userId = other.id;
  blockBtn.dataset.username = other.username;
  const blocked = canBlock && conversation.blocked_by_me;
  $("blocked-note").classList.toggle("hidden", !blocked);
  $("blocked-note").textContent = blocked ? `You blocked ${other.username}. Unblock to send messages.` : "";
  for (const id of ["message-input", "attach-btn", "mic-btn", "loc-btn", "emoji-btn"]) $(id).disabled = blocked;
  $("composer").classList.toggle("composer--locked", blocked);
  runHooks("headerRendered", conversation);
}

$("block-toggle").addEventListener("click", async () => {
  const { userId, username } = $("block-toggle").dataset;
  const conversation = findConversation(state.activeId);
  try {
    if (conversation.blocked_by_me) {
      await api(`/api/settings/blocked/${userId}`, { method: "DELETE" });
    } else {
      await api("/api/settings/blocked", { method: "POST", body: JSON.stringify({ username }) });
    }
    await loadConversations();
    await loadHistory(); // redraw messages with or without their action buttons
  } catch (error) {
    showError(error.message);
  }
});

// "kabir is typing" with three bouncing dots.
function renderTyping() {
  const typing = state.typing[state.activeId];
  const box = $("typing");
  if (!typing) return box.replaceChildren();
  const dots = el("span", "typing__dots");
  dots.append(el("i"), el("i"), el("i"));
  const label = typing.drawing ? "@ai is drawing" : typing.username === "ai" ? "@ai is thinking" : `${typing.username} is typing`;
  box.replaceChildren(dots, label);
}

// ---------- search: people and messages ----------

let searchTimer = null;
let searchRun = 0;

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

function sectionHeading(text) {
  return el("p", "list-label", text);
}

async function runSearch(q) {
  const run = ++searchRun;
  const [users, found] = await Promise.all([
    api(`/api/users?q=${encodeURIComponent(q)}`),
    q.length >= 2 ? api(`/api/search/messages?q=${encodeURIComponent(q)}`) : Promise.resolve([]),
  ]);
  if (run !== searchRun) return; // a newer search finished first

  const box = $("search-results");
  const parts = [sectionHeading("People")];
  if (users.length === 0) parts.push(el("p", "list-empty", `Nobody called "${q}".`));
  for (const user of users) {
    const item = el("button", "item");
    const text = el("div", "item__text");
    text.append(el("p", "item__title", user.username));
    let note = user.is_bot ? "AI assistant" : user.online ? "Online" : "Start a chat";
    if (user.is_private && !user.is_bot && !findDirectChat(user.id)) note = "Private account · Send a request";
    text.append(el("p", "item__preview", note));
    item.append(avatar(user), text);
    item.addEventListener("click", () => startChat(user.username));
    parts.push(item);
  }

  if (q.length >= 2) {
    parts.push(sectionHeading("Messages"));
    if (found.length === 0) parts.push(el("p", "list-empty", "No messages match."));
    for (const m of found) {
      const item = el("button", "item");
      const text = el("div", "item__text");
      const top = el("div", "item__row");
      top.append(el("p", "item__title", m.conversation_title));
      top.append(el("span", "item__date", new Date(m.created_at).toLocaleDateString([], { day: "numeric", month: "short" })));
      text.append(top);
      text.append(highlighted(`${m.sender_id === state.me.id ? "You" : m.sender_username}: ${m.content}`, q));
      item.append(text);
      item.addEventListener("click", () => {
        $("search").value = "";
        showSearch(false);
        jumpToMessage(m.conversation_id, m.id);
      });
      parts.push(item);
    }
  }
  box.replaceChildren(...parts);
  showSearch(true);
}

// The search text in bold, without using innerHTML.
function highlighted(text, q) {
  const p = el("p", "item__snippet");
  const at = text.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) {
    p.textContent = text;
    return p;
  }
  p.append(text.slice(0, at), el("mark", "", text.slice(at, at + q.length)), text.slice(at + q.length));
  return p;
}

function findDirectChat(userId) {
  return state.conversations.find((c) => c.type === "direct" && c.members.some((m) => m.id === userId));
}

async function startChat(username) {
  try {
    // Private accounts get a chat request instead (extras.js); everyone else opens straight away.
    const result = await api("/api/requests", { method: "POST", body: JSON.stringify({ username }) });
    if (result.status === "pending") {
      $("search").value = "";
      showSearch(false);
      window.showRequestSent?.(result.request);
      return;
    }
    const conversation = result.conversation;
    if (!findConversation(conversation.id)) state.conversations.unshift(conversation);
    $("search").value = "";
    showSearch(false);
    await openConversation(conversation.id);
  } catch (error) {
    showError(error.message);
  }
}

// ---------- layout (phones show one pane at a time) ----------

// On phones, styles.css shows the sidebar or the chat depending on data-view.
function setView(view) {
  $("app").dataset.view = view;
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
  runHooks("init");
})();
