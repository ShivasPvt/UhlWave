// Emoji, stickers and GIFs: the smiley button next to the message box.
//   Emoji     insert into your message
//   Stickers  built-in big stickers (always work) plus GIPHY stickers (if set up)
//   GIFs      GIPHY search; trending when the box is empty
// GIF searches go through our server, so the GIPHY key never reaches the browser.

const EMOJI = {
  "Smileys": "😀 😃 😄 😁 😆 🥹 😅 😂 🤣 🥲 😊 😇 🙂 🙃 😉 😌 😍 🥰 😘 😋 😛 😜 🤪 😝 🤗 🤭 🫢 🤫 🤔 🫡 🤐 🤨 😐 😑 😶 🫥 😏 😒 🙄 😬 😮‍💨 🤥 😴 🤤 😪 😮 😯 😲 🥱 😳 🥺 😦 😧 😨 😰 😥 😢 😭 😱 😖 😣 😞 😓 😩 😫 😤 😡 😠 🤬 😈 💀 🤡 🤓 😎 🥸 🤠 🥳 🤯 🤒 🤕 🤢 🤮 🤧 😷",
  "People": "👍 👎 👌 🤌 ✌️ 🤞 🫶 🤟 🤘 🤙 👋 👏 🙌 🙏 🤝 💪 👀 🧠 🫂 👶 🧒 👦 👧 🧑 👨 👩 🧑‍🎓 🧑‍💻 🧑‍🏫 🧑‍🍳 🧑‍🔬 🕺 💃 🚶 🏃 🧘",
  "Hearts": "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❤️‍🔥 💕 💞 💓 💗 💖 💘 💝 💯 ✨ ⭐ 🌟 💫 🔥 💥 🎉 🎊 🎈 🎁 🏆 🥇",
  "Nature": "🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🐔 🐧 🐦 🦅 🦉 🐝 🦋 🐌 🐢 🐍 🐙 🐬 🐳 🌸 🌼 🌻 🌹 🌷 🌱 🌲 🌳 🍁 🍂 🌈 ☀️ 🌤️ ⛅ 🌧️ ⛈️ ❄️ ☃️ 🌊 🏔️ ⛰️ 🌙",
  "Food": "🍎 🍊 🍋 🍌 🍉 🍇 🍓 🥭 🍍 🥥 🥑 🍅 🌶️ 🌽 🥕 🧄 🥔 🍞 🧀 🥚 🍳 🥞 🍔 🍟 🍕 🌭 🥪 🌮 🍜 🍝 🍛 🍣 🍱 🥟 🍦 🍩 🍪 🎂 🍫 🍬 ☕ 🍵 🧋 🥤 🧃",
  "Things": "📚 📖 ✏️ 📝 💻 🖥️ ⌨️ 🖱️ 📱 🔋 💡 🔦 📷 🎧 🎮 🎲 🧩 ⚽ 🏀 🏏 🏸 🎾 🏓 🚴 🚗 🚌 🚆 ✈️ 🚀 🛵 🏠 🏫 🏥 🏕️ ⏰ ⌛ 📅 📌 📍 🔑 🔒 🎵 🎶 🎤 🎬 ✅ ❌ ⚠️ ❓ ❗ ➕ 🆗 🆒",
};

const picker = { tab: "emoji", gifTimer: null, gifRun: 0, stickers: null };

function recentEmoji() {
  try {
    return JSON.parse(localStorage.getItem("uhlwave-recent-emoji") || "[]");
  } catch {
    return [];
  }
}

function rememberEmoji(emoji) {
  const recent = [emoji, ...recentEmoji().filter((e) => e !== emoji)].slice(0, 24);
  try {
    localStorage.setItem("uhlwave-recent-emoji", JSON.stringify(recent));
  } catch {}
}

function insertAtCaret(text) {
  const input = $("message-input");
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  const caret = start + text.length;
  input.focus();
  input.setSelectionRange(caret, caret);
  input.dispatchEvent(new Event("input")); // typing indicator
}

function openPicker(tab = picker.tab) {
  // Secret chats only carry encrypted text, so stickers and GIFs (sent through the server) are off.
  const secret = findConversation(state.activeId)?.type === "secret";
  if (secret) tab = "emoji";
  picker.tab = tab;
  const panel = $("picker");
  const tabs = el("div", "picker-tabs");
  const choices = secret ? [["emoji", "Emoji"]] : [["emoji", "Emoji"], ["stickers", "Stickers"], ["gifs", "GIFs"]];
  for (const [key, label] of choices) {
    const b = el("button", "picker-tab", label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(key === tab));
    b.addEventListener("click", () => openPicker(key));
    tabs.append(b);
  }
  const body = el("div", "picker-body");
  panel.replaceChildren(tabs, body);
  panel.classList.remove("hidden");
  $("emoji-btn").setAttribute("aria-expanded", "true");
  if (tab === "emoji") drawEmoji(body);
  else if (tab === "stickers") drawStickers(body);
  else drawGifs(body);
}

function closePicker() {
  $("picker").classList.add("hidden");
  $("emoji-btn").setAttribute("aria-expanded", "false");
}

function emojiButton(emoji) {
  const b = el("button", "emoji", emoji);
  b.type = "button";
  b.addEventListener("click", () => {
    insertAtCaret(emoji);
    rememberEmoji(emoji);
  });
  return b;
}

function drawEmoji(body) {
  const recent = recentEmoji();
  const groups = recent.length ? { "Recently used": recent.join(" "), ...EMOJI } : EMOJI;
  for (const [name, list] of Object.entries(groups)) {
    body.append(el("p", "picker-label", name));
    const grid = el("div", "emoji-grid");
    for (const emoji of list.split(" ").filter(Boolean)) grid.append(emojiButton(emoji));
    body.append(grid);
  }
}

async function sendSticker(payload) {
  const conversationId = state.activeId;
  if (!conversationId) return;
  closePicker();
  try {
    await api(`/api/conversations/${conversationId}/sticker`, { method: "POST", body: JSON.stringify(payload) });
  } catch (error) {
    showError(error.message);
  }
}

async function drawStickers(body) {
  picker.stickers ||= await api("/api/stickers").catch(() => []);
  body.append(el("p", "picker-label", "UhlWave stickers"));
  const grid = el("div", "sticker-grid");
  for (const emoji of picker.stickers) {
    const b = el("button", "sticker-pick", emoji);
    b.type = "button";
    b.title = "Send sticker";
    b.addEventListener("click", () => sendSticker({ kind: "sticker", emoji }));
    grid.append(b);
  }
  body.append(grid);
  gifSearch(body, "stickers", "Search GIPHY stickers");
}

function drawGifs(body) {
  gifSearch(body, "gifs", "Search GIFs");
}

function gifSearch(body, kind, placeholder) {
  const input = el("input", "picker-search");
  input.type = "search";
  input.placeholder = placeholder;
  input.setAttribute("aria-label", placeholder);
  const results = el("div", kind === "gifs" ? "gif-grid" : "gif-grid gif-grid--stickers");
  const note = el("p", "picker-note");
  body.append(input, results, note);
  const run = async () => {
    const id = ++picker.gifRun;
    const q = input.value.trim();
    note.textContent = "Loading…";
    let items;
    try {
      items = await api(`/api/gifs?kind=${kind}&q=${encodeURIComponent(q)}`);
    } catch (error) {
      if (id !== picker.gifRun) return;
      results.replaceChildren();
      note.textContent = error.message;
      return;
    }
    if (id !== picker.gifRun) return;
    note.textContent = items.length ? "Powered by GIPHY" : "No results.";
    results.replaceChildren(...items.map((gif) => {
      const b = el("button", "gif-pick");
      b.type = "button";
      b.title = gif.title;
      const img = el("img");
      img.src = gif.preview;
      img.alt = gif.title;
      img.loading = "lazy";
      b.append(img);
      b.addEventListener("click", () => sendSticker({ kind: kind === "gifs" ? "gif" : "sticker", url: gif.url, title: gif.title }));
      return b;
    }));
  };
  input.addEventListener("input", () => {
    clearTimeout(picker.gifTimer);
    picker.gifTimer = setTimeout(run, 350);
  });
  run();
  if (kind === "gifs") input.focus();
}

$("emoji-btn").addEventListener("click", (event) => {
  event.stopPropagation();
  if ($("picker").classList.contains("hidden")) openPicker();
  else closePicker();
});
// Clicks inside the picker never count as "outside" (its tabs redraw it, which
// detaches the button that was clicked).
$("picker").addEventListener("click", (event) => event.stopPropagation());
document.addEventListener("click", (event) => {
  if (!$("picker").classList.contains("hidden") && !event.target.closest("#emoji-btn")) closePicker();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("picker").classList.contains("hidden")) {
    closePicker();
    $("message-input").focus();
  }
});
hooks.chatOpened.push(closePicker);
