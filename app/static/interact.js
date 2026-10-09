// Small things that make chatting feel alive:
//   the paperclip menu (file, location, poll)
//   double-click a message to ❤️ it, with a little burst
//   swipe a message to the right (or drag it) to reply
//   right-click or long-press a message for a quick menu
//   a "jump to latest" button with a count of new messages
//   confetti when someone celebrates 🎉
//   keyboard shortcuts (press ? to see them)
// Loaded last; uses helpers from chat.js, extras.js and polls.js.

// ---------- the paperclip menu ----------

function toggleAttachMenu(open) {
  const menu = $("attach-menu");
  const show = open ?? menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !show);
  $("attach-btn").setAttribute("aria-expanded", String(show));
  if (show) {
    const secret = findConversation(state.activeId)?.type === "secret";
    for (const id of ["attach-file", "loc-btn", "attach-poll"]) {
      $(id).disabled = secret;
      $(id).title = secret ? "Secret chats are for end-to-end encrypted text only" : "";
    }
    menu.querySelector("button:not(:disabled)")?.focus();
  }
}

$("attach-btn").addEventListener("click", (event) => {
  event.stopPropagation();
  toggleAttachMenu();
});
$("attach-menu").addEventListener("click", (event) => {
  event.stopPropagation();
  if (event.target.closest("button")) toggleAttachMenu(false);
});
$("attach-file").addEventListener("click", () => $("file-input").click());
$("attach-poll").addEventListener("click", () => window.openNewPoll?.());
document.addEventListener("click", () => toggleAttachMenu(false));

// ---------- double-click to ❤️ ----------

function heartBurst(x, y) {
  const heart = el("span", "heart-burst", "❤️");
  heart.style.left = `${x}px`;
  heart.style.top = `${y}px`;
  document.body.append(heart);
  heart.addEventListener("animationend", () => heart.remove());
}

// ---------- swipe or drag right to reply ----------

function enableSwipe(row, bubble, message) {
  let startX = 0, startY = 0, dx = 0, active = false, pointerId = null;
  bubble.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("a, audio, button, input")) return;
    startX = event.clientX;
    startY = event.clientY;
    dx = 0;
    active = true;
    pointerId = event.pointerId;
  });
  bubble.addEventListener("pointermove", (event) => {
    if (!active || event.pointerId !== pointerId) return;
    const moveX = event.clientX - startX;
    if (Math.abs(event.clientY - startY) > 30) {
      active = false; // they're scrolling, not swiping
      row.style.transform = "";
      return;
    }
    if (moveX <= 0) return;
    dx = Math.min(moveX, 90);
    if (dx > 8) bubble.setPointerCapture?.(pointerId);
    row.style.transform = `translateX(${dx}px)`;
    row.classList.toggle("msg--swipe-ready", dx > 60);
  });
  const finish = () => {
    if (!active) return;
    active = false;
    row.style.transition = "transform 220ms var(--spring)";
    row.style.transform = "";
    setTimeout(() => (row.style.transition = ""), 240);
    row.classList.remove("msg--swipe-ready");
    if (dx > 60) {
      navigator.vibrate?.(15);
      startReply(message);
    }
  };
  bubble.addEventListener("pointerup", finish);
  bubble.addEventListener("pointercancel", finish);
}

// ---------- right-click / long-press menu ----------

function closeContextMenu() {
  $("ctx-menu").classList.add("hidden");
  document.body.classList.remove("ctx-open");
}

function openContextMenu(message, x, y) {
  const menu = $("ctx-menu");
  const mine = message.sender_id === state.me.id;
  const conversation = findConversation(message.conversation_id);
  const items = [];
  if (!conversation?.blocked_by_me) {
    const emojis = el("div", "ctx-emojis");
    for (const emoji of REACTIONS) {
      const b = el("button", "act-emoji", emoji);
      b.type = "button";
      b.setAttribute("aria-label", `React ${emoji}`);
      b.addEventListener("click", () => {
        closeContextMenu();
        react(message, emoji);
      });
      emojis.append(b);
    }
    items.push(emojis);
  }
  const add = (label, fn, danger = false) => {
    const b = el("button", `menu__item${danger ? " menu__item--danger" : ""}`, label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.addEventListener("click", () => {
      closeContextMenu();
      fn();
    });
    items.push(b);
  };
  if (!conversation?.blocked_by_me) add("Reply", () => startReply(message));
  if (["text", "location", "gif", "sticker", "poll"].includes(message.type) && message.content) {
    add("Copy text", async () => {
      try {
        await navigator.clipboard.writeText(message.content);
        toast("Copied");
      } catch {
        toast("Couldn't copy. Select the text instead.");
      }
    });
  }
  add(message.pinned ? "Unpin" : "Pin", () => togglePin(message));
  if (mine && message.type === "text") add("Edit", () => startEdit(message));
  if (mine) add("Delete for everyone", () => deleteMessage(message), true);
  menu.replaceChildren(...items);
  menu.classList.remove("hidden");
  document.body.classList.add("ctx-open"); // hides the hover toolbar underneath
  // Keep it on screen.
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - height - 8))}px`;
  menu.querySelector("button")?.focus();
}

document.addEventListener("click", (event) => {
  if (!event.target.closest("#ctx-menu")) closeContextMenu();
});
document.addEventListener("scroll", closeContextMenu, true);

hooks.messageRendered.push((row, bubble, message) => {
  if (message.type === "system") return;
  bubble.addEventListener("dblclick", (event) => {
    if (event.target.closest("a, audio, button, input")) return;
    if (findConversation(message.conversation_id)?.blocked_by_me) return;
    window.getSelection()?.removeAllRanges();
    heartBurst(event.clientX, event.clientY);
    const mineAlready = message.reactions?.some((r) => r.emoji === "❤️" && r.user_ids.includes(state.me.id));
    if (!mineAlready) react(message, "❤️");
  });
  row.addEventListener("contextmenu", (event) => {
    if (event.target.closest("a")) return; // let people copy links normally
    event.preventDefault();
    openContextMenu(message, event.clientX, event.clientY);
  });
  // Long-press on touch screens.
  let pressTimer = null;
  bubble.addEventListener("touchstart", (event) => {
    const touch = event.touches[0];
    pressTimer = setTimeout(() => {
      navigator.vibrate?.(20);
      openContextMenu(message, touch.clientX, touch.clientY);
    }, 520);
  }, { passive: true });
  for (const type of ["touchend", "touchmove", "touchcancel"]) bubble.addEventListener(type, () => clearTimeout(pressTimer), { passive: true });
  enableSwipe(row, bubble, message);
});

// ---------- jump to latest ----------

let unseenBelow = 0;

function nearBottom() {
  const box = $("messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 160;
}

function updateJumpButton() {
  const show = !nearBottom();
  if (!show) unseenBelow = 0;
  $("jump-latest").classList.toggle("hidden", !show);
  $("jump-count").classList.toggle("hidden", unseenBelow === 0);
  $("jump-count").textContent = unseenBelow > 99 ? "99+" : String(unseenBelow);
}

$("messages").addEventListener("scroll", updateJumpButton, { passive: true });
$("jump-latest").addEventListener("click", () => {
  const box = $("messages");
  box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
  unseenBelow = 0;
  updateJumpButton();
});

hooks.messageArrived.push((message) => {
  if (message.conversation_id !== state.activeId) return;
  if (!nearBottom() && message.sender_id !== state.me.id) {
    unseenBelow++;
    $("jump-latest").classList.remove("pulse");
    void $("jump-latest").offsetWidth;
    $("jump-latest").classList.add("pulse");
  }
  updateJumpButton();
  maybeConfetti(message);
});
hooks.chatOpened.push(() => {
  unseenBelow = 0;
  updateJumpButton();
});

// ---------- confetti ----------

const CELEBRATE = /🎉|🥳|🎊|\bcongrat|\bhappy birthday\b|\bhbd\b/i;

function maybeConfetti(message) {
  if (message.type !== "text" && message.type !== "sticker") return;
  if (!CELEBRATE.test(message.content || "")) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const colours = ["#0e9f96", "#df3f5c", "#f4a62a", "#3a7bd5", "#8b5cf6", "#36d399"];
  const area = $("messages").getBoundingClientRect();
  for (let i = 0; i < 70; i++) {
    const bit = el("i", "confetti");
    bit.style.left = `${area.left + Math.random() * area.width}px`;
    bit.style.top = `${area.top - 10}px`;
    bit.style.background = colours[i % colours.length];
    bit.style.setProperty("--fall", `${area.height * (0.6 + Math.random() * 0.5)}px`);
    bit.style.setProperty("--drift", `${(Math.random() - 0.5) * 160}px`);
    bit.style.setProperty("--spin", `${(Math.random() - 0.5) * 1080}deg`);
    bit.style.animationDelay = `${Math.random() * 300}ms`;
    bit.style.animationDuration = `${1400 + Math.random() * 900}ms`;
    if (i % 3 === 0) bit.classList.add("confetti--round");
    document.body.append(bit);
    bit.addEventListener("animationend", () => bit.remove());
  }
}

// ---------- keyboard shortcuts ----------

const SHORTCUTS = [
  ["Ctrl + K", "Search people and messages"],
  ["Ctrl + F", "Search inside the open chat"],
  ["Alt + ↑ / ↓", "Previous / next chat"],
  ["↑ in an empty message box", "Edit your last message"],
  ["Esc", "Cancel a reply or edit, close menus"],
  ["Double-click a message", "React ❤️"],
  ["Right-click a message", "Reply, react, copy, pin, edit, delete"],
  ["?", "Show these shortcuts"],
];

function showShortcuts() {
  const list = el("dl", "shortcuts");
  for (const [keys, what] of SHORTCUTS) {
    const row = el("div", "shortcut");
    const dt = el("dt");
    for (const [i, part] of keys.split(" + ").entries()) {
      if (i) dt.append(" + ");
      dt.append(el("kbd", "", part));
    }
    row.append(dt, el("dd", "", what));
    list.append(row);
  }
  openModal("Keyboard shortcuts", list);
}

function typingInField(target) {
  return target.closest("input, textarea, select, [contenteditable]");
}

document.addEventListener("keydown", (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === "k") {
    event.preventDefault();
    $("search").focus();
    $("search").select();
    return;
  }
  if (mod && event.key.toLowerCase() === "f" && state.activeId && !$("chat-view").classList.contains("hidden")) {
    event.preventDefault();
    window.openChatSearch?.();
    return;
  }
  if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
    const list = visibleConversations();
    if (!list.length) return;
    event.preventDefault();
    const at = list.findIndex((c) => c.id === state.activeId);
    const next = event.key === "ArrowDown" ? Math.min(list.length - 1, at + 1) : Math.max(0, at - 1);
    if (list[next] && list[next].id !== state.activeId) openConversation(list[next].id);
    return;
  }
  if (event.key === "Escape") {
    closeContextMenu();
    toggleAttachMenu(false);
  }
  if (event.key === "?" && !typingInField(event.target) && $("modal").classList.contains("hidden")) {
    event.preventDefault();
    showShortcuts();
  }
});

// ↑ in an empty message box edits your last text message.
$("message-input").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowUp" || $("message-input").value || state.editing) return;
  const mine = [...state.messages.values()]
    .filter((m) => m.sender_id === state.me.id && m.type === "text" && !m.deleted)
    .sort((a, b) => b.id - a.id)[0];
  if (!mine) return;
  event.preventDefault();
  startEdit(mine);
});

// A small "?" hint for people who never press it.
hooks.init.push(() => {
  try {
    if (localStorage.getItem("uhlwave-shortcuts-seen")) return;
    localStorage.setItem("uhlwave-shortcuts-seen", "1");
  } catch {
    return;
  }
  setTimeout(() => toast("Tip: press ? to see keyboard shortcuts"), 1500);
});
