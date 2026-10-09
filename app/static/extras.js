// Organising and managing chats: filters and folders (archived, locked, categories),
// chat requests, the ⋮ menu, pinned messages, search inside a chat, the media
// gallery, profiles and group invite links.
// Loaded after chat.js and groups.js, so it uses their helpers ($, el, api, state,
// openModal, field, primaryButton…). User text always goes in with textContent.

// ---------- small helpers ----------

function menuItem(label, onClick, { danger = false } = {}) {
  const b = el("button", `menu__item${danger ? " menu__item--danger" : ""}`, label);
  b.type = "button";
  b.setAttribute("role", "menuitem");
  b.addEventListener("click", () => {
    closeChatMenu();
    onClick();
  });
  return b;
}

function activeConversation() {
  return findConversation(state.activeId);
}

// Ask for the chat-lock PIN in a small pop-up. Resolves to the PIN, or null if cancelled.
function askPin(title, note) {
  return new Promise((resolve) => {
    const form = el("form", "stack");
    const input = el("input");
    input.type = "password";
    input.inputMode = "numeric";
    input.autocomplete = "off";
    input.maxLength = 8;
    input.classList.add("input--pin");
    const button = primaryButton("Continue");
    form.append(el("p", "small-note", note), field("PIN", input), button);
    let done = false;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!/^\d{4,8}$/.test(input.value)) return modalError("Enter your 4–8 digit PIN.");
      done = true;
      closeModal();
      resolve(input.value);
    });
    openModal(title, form);
    const stop = () => {
      if (!done) resolve(null);
    };
    $("close-modal").addEventListener("click", stop, { once: true });
    $("modal-backdrop").addEventListener("click", stop, { once: true });
  });
}

// ---------- filters: All, Unread, Groups, Requests, categories, Archived, Locked ----------

async function loadFolders() {
  const [categories, requests, locked] = await Promise.all([
    api("/api/categories"), api("/api/requests"), api("/api/locked"),
  ]);
  state.categories = categories;
  state.requests = requests;
  state.locked = locked;
  if (state.filter === "locked" && !locked.open) state.filter = "all";
  renderSidebar();
}

function renderFilters() {
  const box = $("filters");
  const chips = [];
  const unread = state.conversations.filter((c) => !c.locked && !c.archived && c.unread > 0).length;
  const add = (key, label, count = 0) => {
    const chip = el("button", "filter", label);
    chip.type = "button";
    chip.setAttribute("role", "tab");
    chip.setAttribute("aria-selected", String(state.filter === key));
    if (count) chip.append(el("span", "filter__count", String(count)));
    chip.addEventListener("click", () => setFilter(key));
    chips.push(chip);
  };
  add("all", "All");
  add("unread", "Unread", unread);
  add("groups", "Groups");
  const incoming = state.requests.incoming.length;
  if (incoming || state.requests.outgoing.length || state.me?.is_private) add("requests", "Requests", incoming);
  for (const c of state.categories) add(`cat:${c.id}`, c.name);
  if (state.conversations.some((c) => c.archived && !c.locked) || state.filter === "archived") add("archived", "Archived");
  if (state.locked.has_pin || state.locked.count) add("locked", "🔒 Locked", 0);
  const manage = el("button", "filter filter--add", "+");
  manage.type = "button";
  manage.title = "Make or edit categories";
  manage.setAttribute("aria-label", "Make or edit categories");
  manage.addEventListener("click", openCategories);
  chips.push(manage);
  box.replaceChildren(...chips);
}

async function setFilter(key) {
  if (key === state.filter) return;
  const wasLocked = state.filter === "locked";
  if (key === "locked" && !state.locked.open) {
    if (!state.locked.has_pin) return toast("Set a chat lock PIN in Settings first.");
    const pin = await askPin("Locked chats", "Enter your chat lock PIN to see your locked chats.");
    if (!pin) return;
    try {
      await api("/api/locked/open", { method: "POST", body: JSON.stringify({ pin }) });
    } catch (error) {
      return toast(error.message);
    }
    state.locked.open = true;
    scheduleLockedClose();
    await loadConversations();
  }
  state.filter = key;
  if (wasLocked && key !== "locked") await closeLockedFolder();
  if (key === "requests") await refreshRequests();
  renderSidebar();
}

let lockedCloseTimer = null;
function scheduleLockedClose() {
  clearTimeout(lockedCloseTimer);
  lockedCloseTimer = setTimeout(() => closeLockedFolder(), 15 * 60 * 1000);
}

// Leaving the locked folder (or 15 minutes passing) hides locked chats again.
async function closeLockedFolder() {
  clearTimeout(lockedCloseTimer);
  if (!state.locked.open) return;
  state.locked.open = false;
  await fetch("/api/locked/close", { method: "POST" });
  const open = activeConversation();
  if (state.filter === "locked") state.filter = "all";
  await loadConversations();
  if (open?.locked) closeConversation();
}

// ---------- chat requests (private accounts) ----------

async function refreshRequests() {
  state.requests = await api("/api/requests");
  renderSidebar();
}

function renderRequests(list) {
  const parts = [];
  const { incoming, outgoing } = state.requests;
  parts.push(sectionHeading("People who want to chat"));
  if (incoming.length === 0) parts.push(el("p", "list-empty", state.me.is_private ? "No requests right now." : "No requests. Your account is public, so anyone can message you."));
  for (const r of incoming) {
    const row = el("div", "item item--static");
    const text = el("div", "item__text");
    text.append(el("p", "item__title", r.user.username));
    text.append(el("p", "item__preview item__preview--wrap", r.note || "Wants to chat with you"));
    const actions = el("div", "item__actions");
    const accept = el("button", "btn btn--primary btn--sm", "Accept");
    accept.addEventListener("click", async () => {
      try {
        const result = await api(`/api/requests/${r.id}/accept`, { method: "POST" });
        await loadConversations();
        await refreshRequests();
        state.filter = "all";
        await openConversation(result.conversation.id);
      } catch (error) {
        toast(error.message);
      }
    });
    const decline = el("button", "btn btn--night btn--sm", "Decline");
    decline.addEventListener("click", async () => {
      await api(`/api/requests/${r.id}/decline`, { method: "POST" }).catch((e) => toast(e.message));
      refreshRequests();
    });
    actions.append(accept, decline);
    const who = avatar(r.user);
    who.addEventListener("click", () => openProfile(r.user.username));
    row.append(who, text, actions);
    parts.push(row);
  }
  if (outgoing.length) {
    parts.push(sectionHeading("Requests you sent"));
    for (const r of outgoing) {
      const row = el("div", "item item--static");
      const text = el("div", "item__text");
      text.append(el("p", "item__title", r.user.username));
      text.append(el("p", "item__preview", "Waiting for them to accept"));
      const cancel = el("button", "btn btn--night btn--sm", "Cancel");
      cancel.addEventListener("click", async () => {
        await api(`/api/requests/${r.id}`, { method: "DELETE" }).catch((e) => toast(e.message));
        refreshRequests();
      });
      row.append(avatar(r.user), text, cancel);
      parts.push(row);
    }
  }
  list.replaceChildren(...parts);
}

function showRequestSent(request) {
  toast(`Chat request sent to ${request.user.username}. You can chat once they accept.`);
  refreshRequests();
}

// ---------- categories ----------

function openCategories() {
  const list = el("div", "members");
  for (const c of state.categories) {
    const row = el("form", "row-form");
    const name = el("input");
    name.value = c.name;
    name.maxLength = 24;
    const save = el("button", "btn btn--quiet btn--sm", "Rename");
    const del = el("button", "btn btn--danger btn--sm", "Delete");
    del.type = "button";
    row.append(field(`Category`, name), save, del);
    row.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api(`/api/categories/${c.id}`, { method: "PATCH", body: JSON.stringify({ name: name.value }) });
        await loadFolders();
        openCategories();
      } catch (error) {
        modalError(error.message);
      }
    });
    del.addEventListener("click", async () => {
      await api(`/api/categories/${c.id}`, { method: "DELETE" });
      if (state.filter === `cat:${c.id}`) state.filter = "all";
      await loadFolders();
      await loadConversations();
      openCategories();
    });
    list.append(row);
  }
  const addForm = el("form", "row-form");
  const input = el("input");
  input.maxLength = 24;
  input.placeholder = "e.g. Hostel, Projects, Family";
  addForm.append(field("New category", input), el("button", "btn btn--primary", "Add"));
  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/categories", { method: "POST", body: JSON.stringify({ name: input.value }) });
      await loadFolders();
      openCategories();
    } catch (error) {
      modalError(error.message);
    }
  });
  const note = el("p", "small-note", "Categories are only for you. Put a chat in one from its ⋮ menu, then tap the category above your chat list.");
  openModal("Categories", ...(state.categories.length ? [list] : []), addForm, note);
}

function chooseCategory(conversation) {
  const list = el("div", "choice-list");
  for (const c of state.categories) {
    const b = el("button", `choice${conversation.category_id === c.id ? " choice--on" : ""}`, c.name);
    b.type = "button";
    b.addEventListener("click", () => setPrefs(conversation, { category_id: c.id }, `Moved to ${c.name}`));
    list.append(b);
  }
  if (conversation.category_id) {
    const none = el("button", "choice", "No category");
    none.type = "button";
    none.addEventListener("click", () => setPrefs(conversation, { clear_category: true }, "Removed from category"));
    list.append(none);
  }
  const make = el("button", "btn btn--quiet btn--block", "Make or edit categories");
  make.addEventListener("click", openCategories);
  openModal("Add to category", ...(state.categories.length ? [list] : [el("p", "small-note", "You haven't made any categories yet.")]), make);
}

async function setPrefs(conversation, prefs, message) {
  try {
    await api(`/api/conversations/${conversation.id}/prefs`, { method: "PATCH", body: JSON.stringify(prefs) });
    closeModal();
    await loadConversations();
    toast(message);
  } catch (error) {
    toast(error.message);
  }
}

// ---------- locking a chat ----------

async function toggleLock(conversation) {
  if (!state.locked.has_pin) {
    toast("Set a chat lock PIN in Settings → Chat lock first.");
    openSettings();
    return;
  }
  const locking = !conversation.locked;
  const pin = await askPin(locking ? "Lock this chat" : "Remove from locked chats",
    locking ? "Locked chats are hidden from your chat list until you enter your PIN." : "Enter your PIN to move this chat back to your normal list.");
  if (!pin) return;
  try {
    await api(`/api/conversations/${conversation.id}/${locking ? "lock" : "unlock"}`, { method: "POST", body: JSON.stringify({ pin }) });
    state.locked = await api("/api/locked");
    if (locking) {
      toast("Chat locked. Find it under 🔒 Locked.");
      closeConversation();
      if (state.filter === "locked") state.filter = "all";
      await loadConversations();
    } else {
      await loadConversations();
      toast("Chat moved back to your list.");
    }
  } catch (error) {
    toast(error.message);
  }
}

// ---------- the ⋮ menu ----------

function openChatMenu() {
  const conversation = activeConversation();
  if (!conversation) return;
  const isGroup = conversation.type === "group";
  const other = otherMember(conversation);
  const items = [
    menuItem("Search in chat", openChatSearch),
    menuItem("Media, links and docs", openMedia),
  ];
  if (conversation.pinned?.length) items.push(menuItem("Pinned messages", openPinnedList));
  items.push(el("hr", "menu__rule"));
  if (conversation.type === "secret") items.push(menuItem("Verify security code", () => window.showSecurityCode?.(conversation)));
  if (isGroup) items.push(menuItem("Group info", openGroupInfo));
  else if (!other.is_bot) items.push(menuItem("View profile", () => openProfile(other.username)));
  if (conversation.type === "direct" && !other.is_bot) items.push(menuItem("Start secret chat 🔒", () => window.startSecretChat?.(other.username)));
  if (!conversation.locked) items.push(menuItem(conversation.archived ? "Unarchive chat" : "Archive chat", () =>
    setPrefs(conversation, { archived: !conversation.archived }, conversation.archived ? "Chat unarchived" : "Chat archived")));
  items.push(menuItem(conversation.locked ? "Remove from locked chats" : "Lock chat", () => toggleLock(conversation)));
  items.push(menuItem("Add to category", () => chooseCategory(conversation)));
  items.push(menuItem("Export chat", () => { window.location.href = `/api/conversations/${conversation.id}/export`; }));
  items.push(el("hr", "menu__rule"));
  if (!isGroup && !other.is_bot) {
    items.push(menuItem(conversation.blocked_by_me ? `Unblock ${other.username}` : `Block ${other.username}`, () => $("block-toggle").click(), { danger: !conversation.blocked_by_me }));
  }
  $("chat-menu").replaceChildren(...items);
  $("chat-menu").classList.remove("hidden");
  $("chat-menu-btn").setAttribute("aria-expanded", "true");
  $("chat-menu").querySelector("button")?.focus();
}

function closeChatMenu() {
  $("chat-menu").classList.add("hidden");
  $("chat-menu-btn").setAttribute("aria-expanded", "false");
}

$("chat-menu-btn").addEventListener("click", (event) => {
  event.stopPropagation();
  if ($("chat-menu").classList.contains("hidden")) openChatMenu();
  else closeChatMenu();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".menu-wrap")) closeChatMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("chat-menu").classList.contains("hidden")) {
    closeChatMenu();
    $("chat-menu-btn").focus();
  }
});

$("chat-who").addEventListener("click", () => {
  const conversation = activeConversation();
  if (!conversation) return;
  if (conversation.type === "group") return openGroupInfo();
  const other = otherMember(conversation);
  if (!other.is_bot) openProfile(other.username);
});

// ---------- pinned messages ----------

let pinnedIndex = 0;

function renderPinnedBar(conversation = activeConversation()) {
  const bar = $("pinned-bar");
  const pinned = conversation?.pinned || [];
  bar.classList.toggle("hidden", pinned.length === 0);
  if (!pinned.length) return;
  pinnedIndex %= pinned.length;
  const m = pinned[pinnedIndex];
  $("pinned-label").textContent = pinned.length > 1 ? `Pinned message ${pinnedIndex + 1} of ${pinned.length}` : "Pinned message";
  $("pinned-preview").textContent = `${m.sender_is_bot ? "@ai" : m.sender_username}: ${describe(m)}`;
}

$("pinned-bar").addEventListener("click", async () => {
  const pinned = activeConversation()?.pinned || [];
  if (!pinned.length) return;
  await scrollToMessage(pinned[pinnedIndex % pinned.length].id);
  pinnedIndex = (pinnedIndex + 1) % pinned.length; // next tap shows the next pin
  renderPinnedBar();
});

function openPinnedList() {
  const conversation = activeConversation();
  const list = el("div", "choice-list");
  for (const m of conversation.pinned) {
    const b = el("button", "choice choice--message");
    b.type = "button";
    b.append(el("span", "choice__who", m.sender_is_bot ? "@ai" : m.sender_username), el("span", "choice__text", describe(m)));
    b.addEventListener("click", () => {
      closeModal();
      scrollToMessage(m.id);
    });
    list.append(b);
  }
  openModal("Pinned messages", list, el("p", "small-note", "Anyone in the chat can pin up to 3 messages. Unpin from a message's hover menu."));
}

async function togglePin(message) {
  try {
    await api(`/api/messages/${message.id}/pin`, { method: message.pinned ? "DELETE" : "POST" });
  } catch (error) {
    showError(error.message);
  }
}

// Scroll the open chat to a message, loading older history if needed.
async function scrollToMessage(messageId) {
  for (let i = 0; i < 30 && !document.querySelector(`[data-message-id="${messageId}"]`) && state.hasMore; i++) {
    const first = $("messages").querySelector("[data-message-id]");
    if (!first) break;
    await loadHistory(Number(first.dataset.messageId));
  }
  const row = document.querySelector(`#messages [data-message-id="${messageId}"]`);
  if (!row) return toast("That message isn't available any more.");
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 2000);
}

// ---------- search inside a chat ----------

const chatSearch = { results: [], index: 0, timer: null, run: 0 };

function openChatSearch() {
  $("chat-search").classList.remove("hidden");
  $("chat-search-input").focus();
}

function closeChatSearch() {
  $("chat-search").classList.add("hidden");
  $("chat-search-input").value = "";
  $("chat-search-count").textContent = "";
  chatSearch.results = [];
}

$("chat-search-input").addEventListener("input", () => {
  clearTimeout(chatSearch.timer);
  const q = $("chat-search-input").value.trim();
  if (!q) {
    chatSearch.results = [];
    $("chat-search-count").textContent = "";
    return;
  }
  chatSearch.timer = setTimeout(async () => {
    const run = ++chatSearch.run;
    // Secret chats can only be searched here: the server can't read them.
    const secret = activeConversation()?.type === "secret";
    const results = secret
      ? [...state.messages.values()].filter((m) => m.type === "text" && !m.deleted && m.content.toLowerCase().includes(q.toLowerCase())).sort((a, b) => b.id - a.id)
      : await api(`/api/conversations/${state.activeId}/search?q=${encodeURIComponent(q)}`).catch(() => []);
    if (run !== chatSearch.run) return;
    chatSearch.results = results; // newest first
    chatSearch.index = 0;
    showSearchHit();
  }, 250);
});

function showSearchHit() {
  const total = chatSearch.results.length;
  $("chat-search-count").textContent = total ? `${chatSearch.index + 1} of ${total}` : "No matches";
  if (total) scrollToMessage(chatSearch.results[chatSearch.index].id);
}

$("chat-search-up").addEventListener("click", () => {
  if (!chatSearch.results.length) return;
  chatSearch.index = Math.min(chatSearch.index + 1, chatSearch.results.length - 1);
  showSearchHit();
});
$("chat-search-down").addEventListener("click", () => {
  if (!chatSearch.results.length) return;
  chatSearch.index = Math.max(chatSearch.index - 1, 0);
  showSearchHit();
});
$("chat-search-close").addEventListener("click", closeChatSearch);
$("chat-search-input").addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeChatSearch();
  if (event.key === "Enter") {
    event.preventDefault();
    $(event.shiftKey ? "chat-search-down" : "chat-search-up").click();
  }
});

// ---------- media, links and docs ----------

const MEDIA_TABS = [["photos", "Photos"], ["docs", "Docs"], ["links", "Links"], ["voice", "Voice"], ["places", "Places"]];

async function openMedia(kind = "photos") {
  const conversationId = state.activeId;
  const tabs = el("div", "segments segments--tabs");
  for (const [key, label] of MEDIA_TABS) {
    const b = el("button", "segment", label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(key === kind));
    b.addEventListener("click", () => openMedia(key));
    tabs.append(b);
  }
  const body = el("div", "media-body");
  body.append(el("p", "small-note", "Loading…"));
  openModal("Media, links and docs", tabs, body);
  let items = [];
  try {
    items = await api(`/api/conversations/${conversationId}/media?kind=${kind}`);
  } catch (error) {
    return modalError(error.message);
  }
  const jump = (m) => () => {
    closeModal();
    scrollToMessage(m.id);
  };
  if (items.length === 0) return body.replaceChildren(el("p", "small-note", "Nothing here yet."));
  if (kind === "photos") {
    const grid = el("div", "media-grid");
    for (const m of items) {
      const b = el("button", "media-thumb");
      b.type = "button";
      const img = el("img");
      img.loading = "lazy";
      img.alt = describe(m);
      if (m.media_url) img.src = m.media_url;
      else if (m.attachment) img.src = m.attachment.url;
      else {
        b.textContent = m.content; // built-in sticker
        b.classList.add("media-thumb--emoji");
      }
      if (img.src) b.append(img);
      b.addEventListener("click", jump(m));
      grid.append(b);
    }
    return body.replaceChildren(grid);
  }
  const list = el("div", "choice-list");
  for (const m of items) {
    const row = el("div", "media-row");
    const when = el("span", "media-row__when", `${m.sender_is_bot ? "@ai" : m.sender_username} · ${new Date(m.created_at).toLocaleDateString([], { day: "numeric", month: "short" })}`);
    if (kind === "docs") {
      const a = el("a", "media-row__main", `📎 ${m.attachment.filename} (${formatSize(m.attachment.size)})`);
      a.href = m.attachment.url;
      a.download = m.attachment.filename;
      row.append(a, when);
    } else if (kind === "links") {
      for (const url of m.links) {
        const a = el("a", "media-row__main", url);
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        row.append(a);
      }
      row.append(when);
    } else if (kind === "voice") {
      const audio = el("audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = m.attachment.url;
      row.append(audio, when);
    } else {
      const a = el("a", "media-row__main", `📍 ${m.content}`);
      a.href = m.location?.map_url || "#";
      a.target = "_blank";
      a.rel = "noopener";
      row.append(a, when);
    }
    const go = el("button", "act-btn", "Show in chat");
    go.addEventListener("click", jump(m));
    row.append(go);
    list.append(row);
  }
  body.replaceChildren(list);
}

// ---------- profiles ----------

async function openProfile(username) {
  let p;
  try {
    p = await api(`/api/users/${encodeURIComponent(username)}/profile`);
  } catch (error) {
    return toast(error.message);
  }
  const card = el("div", "profile");
  const big = avatar(p);
  big.classList.add("avatar--xl");
  card.append(big, el("p", "profile__name", `@${p.username}`));
  let status = p.online ? "Online" : p.last_seen_at ? `Last seen ${new Date(p.last_seen_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : "";
  if (p.is_private) status = status ? `${status} · Private account` : "Private account";
  if (status) card.append(el("p", "profile__status", status));
  card.append(el("p", `profile__bio${p.bio ? "" : " profile__bio--empty"}`, p.bio || "No About line yet."));
  if (p.groups_in_common.length) card.append(el("p", "small-note", `Groups in common: ${p.groups_in_common.join(", ")}`));

  const actions = el("div", "profile__actions");
  if (p.is_me) {
    const edit = el("button", "btn btn--primary btn--block", "Edit your profile in Settings");
    edit.addEventListener("click", () => {
      closeModal();
      openSettings();
    });
    actions.append(edit);
  } else {
    if (p.conversation_id) {
      const msg = el("button", "btn btn--primary", "Message");
      msg.addEventListener("click", () => {
        closeModal();
        state.filter = findConversation(p.conversation_id) ? state.filter : "all";
        openConversation(p.conversation_id);
      });
      actions.append(msg);
      if (!p.blocked_by_me && !p.is_bot) {
        const secret = el("button", "btn btn--quiet", "🔒 Secret chat");
        secret.title = "End-to-end encrypted: only the two of you can read it";
        secret.addEventListener("click", () => {
          closeModal();
          window.startSecretChat?.(p.username);
        });
        actions.append(secret);
      }
      if (!p.blocked_by_me) {
        const voice = el("button", "btn btn--quiet", "Voice call");
        voice.addEventListener("click", async () => {
          closeModal();
          await openConversation(p.conversation_id);
          window.startCall?.(false);
        });
        const video = el("button", "btn btn--quiet", "Video call");
        video.addEventListener("click", async () => {
          closeModal();
          await openConversation(p.conversation_id);
          window.startCall?.(true);
        });
        actions.append(voice, video);
      }
    } else if (p.request_pending) {
      actions.append(el("button", "btn btn--quiet", "Request sent"));
      actions.lastChild.disabled = true;
    } else {
      const start = el("button", "btn btn--primary", p.is_private ? "Send chat request" : "Message");
      start.addEventListener("click", () => {
        closeModal();
        startChat(p.username);
      });
      actions.append(start);
    }
  }
  card.append(actions);
  openModal(p.is_me ? "Your profile" : "Profile", card);
}

// ---------- group invite links (/join/<code>) ----------

async function handleJoinLink() {
  const match = location.hash.match(/^#join=([A-Za-z0-9_-]{4,40})$/);
  if (!match) return;
  history.replaceState(null, "", "/chat");
  const code = match[1];
  let preview;
  try {
    preview = await api(`/api/invites/${code}`);
  } catch (error) {
    return openModal("Invite link", el("p", "", error.message));
  }
  const card = el("div", "profile");
  const pic = avatar({ username: preview.title, is_bot: false, online: false, avatar_url: preview.avatar_url });
  pic.classList.add("avatar--xl", "avatar--group");
  card.append(pic, el("p", "profile__name", preview.title), el("p", "profile__status", plural(preview.member_count, "member")));
  if (preview.description) card.append(el("p", "profile__bio", preview.description));
  const join = el("button", "btn btn--primary btn--block", preview.already_member ? "Open group" : "Join group");
  join.addEventListener("click", async () => {
    try {
      const group = await api(`/api/invites/${code}/join`, { method: "POST" });
      closeModal();
      await loadConversations();
      await openConversation(group.id);
    } catch (error) {
      modalError(error.message);
    }
  });
  card.append(join);
  openModal("Group invite", card);
}

// ---------- live updates ----------

hooks.event.push((event) => {
  if (event.type === "pins_changed") {
    const conversation = findConversation(event.conversation_id);
    if (!conversation) return;
    conversation.pinned = event.pinned;
    if (event.conversation_id === state.activeId) {
      pinnedIndex = 0;
      renderPinnedBar(conversation);
    }
  } else if (event.type === "requests_changed") {
    const before = state.requests.incoming.length;
    api("/api/requests").then((requests) => {
      state.requests = requests;
      if (requests.incoming.length > before) toast(`${requests.incoming[0].user.username} wants to chat with you. See Requests.`);
      renderSidebar();
    });
  }
});

hooks.headerRendered.push((conversation) => renderPinnedBar(conversation));
hooks.chatOpened.push(() => {
  closeChatSearch();
  pinnedIndex = 0;
});
hooks.conversationsLoaded.push(() => {
  // The locked folder timed out on the server: show the normal list again.
  if (state.filter === "locked" && !state.conversations.some((c) => c.locked)) {
    api("/api/locked").then((locked) => {
      state.locked = locked;
      if (!locked.open) {
        state.filter = "all";
        renderSidebar();
      }
    });
  }
});
hooks.init.push(async () => {
  state.me = { ...state.me, ...(await api("/api/settings").catch(() => ({}))) };
  await loadFolders();
  handleJoinLink();
});

window.addEventListener("hashchange", handleJoinLink);

// Used from chat.js (message actions, sidebar) and groups.js.
Object.assign(window, { openProfile, togglePin, renderFilters, renderRequests, showRequestSent, loadFolders });
