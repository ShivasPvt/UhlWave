// The Settings panel. Loaded after chat.js, so it can use $, el, api, state,
// avatar, localPrefs and friends from there.

const DISAPPEAR_LABELS = { 0: "Off", 300: "5 min", 3600: "1 hour", 86400: "1 day", 604800: "1 week" };
let settings = null;
let lastFocus = null;

// ---------- open / close ----------

async function openSettings() {
  lastFocus = document.activeElement;
  $("settings").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  $("close-settings").focus();
  try {
    settings = await api("/api/settings");
    renderSettings();
    window.renderAppearanceSettings?.();
    window.renderE2EESettings?.();
  } catch (error) {
    flash(error.message);
  }
}

function closeSettings() {
  $("settings").classList.add("hidden");
  document.body.style.overflow = "";
  $("delete-form").classList.add("hidden");
  $("delete-confirm").value = "";
  lastFocus?.focus();
}

$("open-settings").addEventListener("click", openSettings);
$("close-settings").addEventListener("click", closeSettings);
$("settings-backdrop").addEventListener("click", closeSettings);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("settings").classList.contains("hidden")) closeSettings();
});

// Small "Saved" message at the bottom of the panel.
let flashTimer = null;
function flash(text) {
  const box = $("settings-status");
  box.textContent = text;
  box.classList.add("toast--show");
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => box.classList.remove("toast--show"), 2200);
}

// ---------- drawing ----------

function renderSettings() {
  const s = settings;
  const pic = avatar({ username: s.username, is_bot: false, online: false, avatar_url: s.avatar_url }, false);
  pic.classList.add("avatar--lg");
  $("set-avatar").replaceChildren(pic);
  $("avatar-remove").classList.toggle("hidden", !s.avatar_url);
  if (document.activeElement !== $("bio-input")) $("bio-input").value = s.bio || "";
  renderPinSection(s.has_lock_pin);
  $("set-username").textContent = `@${s.username}`;
  $("set-signin").textContent = s.sign_in_method === "google" ? `Signed in with Google as ${s.email}` : "Signed in with a username and password";
  $("new-username").value = s.username;
  $("delete-name").textContent = s.username;

  // Toggles stored on the server...
  document.querySelectorAll("[data-setting]").forEach((sw) => {
    sw.setAttribute("aria-checked", String(Boolean(s[sw.dataset.setting])));
  });
  // ...and the ones that only apply to this browser.
  document.querySelectorAll("[data-local]").forEach((sw) => {
    sw.setAttribute("aria-checked", String(Boolean(localPrefs[sw.dataset.local])));
  });
  if (!("Notification" in window)) {
    $("notify-desc").textContent = "This browser doesn't support notifications.";
  } else if (Notification.permission === "denied") {
    $("notify-desc").textContent = "Notifications are blocked for this site. Allow them in your browser's site settings first.";
  }

  $("disappear-choices").replaceChildren(...s.disappear_choices.map((seconds) => {
    const button = el("button", "segment", DISAPPEAR_LABELS[seconds] || `${seconds}s`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(seconds === s.disappear_seconds));
    button.addEventListener("click", () => save({ disappear_seconds: seconds }, seconds ? `New messages disappear after ${DISAPPEAR_LABELS[seconds]}` : "Disappearing messages off"));
    return button;
  }));

  const list = $("blocked-list");
  if (s.blocked.length === 0) {
    list.replaceChildren(el("p", "set-desc", "You haven't blocked anyone."));
  } else {
    list.replaceChildren(...s.blocked.map((user) => {
      const row = el("div", "blocked-row");
      const unblock = el("button", "btn btn--quiet btn--sm", "Unblock");
      unblock.addEventListener("click", async () => {
        settings = await api(`/api/settings/blocked/${user.id}`, { method: "DELETE" });
        renderSettings();
        flash(`Unblocked ${user.username}`);
        loadConversations();
      });
      row.append(avatar({ ...user, online: false }, true), el("span", "", user.username), unblock);
      return row;
    }));
  }
}

// ---------- saving ----------

async function save(changes, message = "Saved") {
  try {
    settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(changes) });
    state.me.is_private = settings.is_private;
    renderSettings();
    renderSidebar();
    if ("is_private" in changes) {
      message = changes.is_private ? "Private account on. New people must send a request." : "Private account off. Anyone can message you.";
    }
    flash(message);
  } catch (error) {
    flash(error.message);
  }
}

document.querySelectorAll("[data-setting]").forEach((sw) => {
  sw.addEventListener("click", () => {
    const on = sw.getAttribute("aria-checked") !== "true";
    save({ [sw.dataset.setting]: on }, on ? "Turned on" : "Turned off");
  });
});

document.querySelectorAll("[data-local]").forEach((sw) => {
  sw.addEventListener("click", async () => {
    const key = sw.dataset.local;
    let on = sw.getAttribute("aria-checked") !== "true";
    if (key === "notify" && on) {
      if (!("Notification" in window)) return flash("This browser doesn't support notifications.");
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        on = false;
        flash("Notifications weren't allowed.");
      }
    }
    localPrefs[key] = on;
    saveLocalPrefs();
    sw.setAttribute("aria-checked", String(on));
    if (key === "hidePreviews") renderSidebar();
    if (on || key !== "notify") flash(on ? "Turned on" : "Turned off");
  });
});

$("username-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = $("new-username").value.trim();
  if (!/^[A-Za-z0-9_]{3,30}$/.test(username)) return flash("Use 3–30 letters, numbers or underscores.");
  try {
    settings = await api("/api/settings/username", { method: "PUT", body: JSON.stringify({ username }) });
    state.me.username = settings.username;
    $("me-name").textContent = `@${settings.username}`;
    renderSettings();
    flash("Username changed");
  } catch (error) {
    flash(error.message);
  }
});

$("logout-everywhere").addEventListener("click", async () => {
  try {
    await api("/api/settings/logout-everywhere", { method: "POST" });
    flash("Logged out on every other device");
  } catch (error) {
    flash(error.message);
  }
});

$("delete-start").addEventListener("click", () => {
  $("delete-form").classList.remove("hidden");
  $("delete-confirm").focus();
});

$("delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/settings/delete-account", {
      method: "POST",
      body: JSON.stringify({ confirm_username: $("delete-confirm").value }),
    });
    window.location.href = "/";
  } catch (error) {
    flash(error.message);
  }
});


// ---------- profile: photo and About ----------

$("avatar-upload").addEventListener("click", () => $("avatar-input").click());
$("avatar-input").addEventListener("change", async () => {
  const file = $("avatar-input").files[0];
  $("avatar-input").value = "";
  if (!file) return;
  if (file.size > 3 * 1024 * 1024) return flash("Profile photos can be up to 3 MB.");
  const res = await fetch("/api/settings/avatar", { method: "POST", body: file, headers: { "Content-Type": file.type || "application/octet-stream" } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return flash(typeof data.detail === "string" ? data.detail : "Couldn't upload that photo.");
  settings = await api("/api/settings");
  renderSettings();
  flash("Profile photo updated");
});

$("avatar-remove").addEventListener("click", async () => {
  await api("/api/settings/avatar", { method: "DELETE" });
  settings = await api("/api/settings");
  renderSettings();
  flash("Profile photo removed");
});

$("bio-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const { bio } = await api("/api/settings/profile", { method: "PUT", body: JSON.stringify({ bio: $("bio-input").value }) });
    settings.bio = bio;
    flash(bio ? "About updated" : "About removed");
  } catch (error) {
    flash(error.message);
  }
});

// ---------- chat lock PIN ----------

function renderPinSection(hasPin) {
  $("pin-current-wrap").classList.toggle("hidden", !hasPin);
  $("pin-new-label").textContent = hasPin ? "New PIN (4–8 digits)" : "Choose a PIN (4–8 digits)";
  $("pin-save").textContent = hasPin ? "Change PIN" : "Set PIN";
  $("pin-remove").classList.toggle("hidden", !hasPin);
  $("pin-desc").textContent = hasPin
    ? "Your PIN is set. Lock a chat from its ⋮ menu; find locked chats under 🔒 Locked above your chat list."
    : "Lock a chat from its ⋮ menu to hide it in a locked folder that needs this PIN. It's separate from your password.";
}

$("pin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const pin = $("pin-new").value;
  if (!/^\d{4,8}$/.test(pin)) return flash("Use 4 to 8 digits.");
  try {
    await api("/api/locked/pin", { method: "PUT", body: JSON.stringify({ pin, current_pin: $("pin-current").value || null }) });
    $("pin-new").value = "";
    $("pin-current").value = "";
    settings.has_lock_pin = true;
    renderSettings();
    await window.loadFolders?.();
    flash("Chat lock PIN saved");
  } catch (error) {
    flash(error.message);
  }
});

$("pin-remove").addEventListener("click", async () => {
  const pin = $("pin-current").value;
  if (!pin) {
    $("pin-current").focus();
    return flash("Type your current PIN first.");
  }
  try {
    await api("/api/locked/pin", { method: "DELETE", body: JSON.stringify({ pin }) });
    $("pin-current").value = "";
    settings.has_lock_pin = false;
    renderSettings();
    await window.loadFolders?.();
    await loadConversations();
    flash("PIN removed. Every chat is unlocked.");
  } catch (error) {
    flash(error.message);
  }
});

// ---------- backup ----------

$("backup-media").addEventListener("click", () => {
  const on = $("backup-media").getAttribute("aria-checked") !== "true";
  $("backup-media").setAttribute("aria-checked", String(on));
});

$("backup-download").addEventListener("click", () => {
  const media = $("backup-media").getAttribute("aria-checked") === "true";
  window.location.href = `/api/backup?media=${media}`;
  flash(media ? "Preparing your backup with files…" : "Downloading your backup");
});

$("backup-open").addEventListener("click", () => $("backup-input").click());
$("backup-input").addEventListener("change", async () => {
  const file = $("backup-input").files[0];
  $("backup-input").value = "";
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
    if (data.app !== "UhlWave" || !Array.isArray(data.chats)) throw new Error();
  } catch {
    return flash("That isn't an UhlWave backup file.");
  }
  closeSettings();
  showBackup(data);
});

// Read-only view of a backup file, entirely in the browser.
function showBackup(data, chatIndex = null) {
  if (chatIndex === null) {
    const list = el("div", "choice-list");
    data.chats.forEach((chat, i) => {
      const b = el("button", "choice choice--message");
      b.type = "button";
      b.append(el("span", "choice__who", chat.title), el("span", "choice__text", `${chat.messages.length} messages · ${chat.type === "group" ? "group" : "chat"}`));
      b.addEventListener("click", () => showBackup(data, i));
      list.append(b);
    });
    const when = new Date(data.exported_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    openModal("Backup", el("p", "small-note", `@${data.account}'s chats, saved ${when}. Read-only.`), list);
    return;
  }
  const chat = data.chats[chatIndex];
  const back = el("button", "btn btn--quiet btn--sm", "← All chats");
  back.addEventListener("click", () => showBackup(data));
  const log = el("div", "backup-log");
  for (const m of chat.messages) {
    const row = el("div", `backup-msg${m.from === data.account ? " backup-msg--mine" : ""}${m.type === "system" ? " backup-msg--system" : ""}`);
    if (m.type !== "system") row.append(el("span", "backup-msg__who", m.from));
    if (m.file?.data_base64 && m.file.type.startsWith("image/")) {
      const img = el("img", "backup-msg__img");
      img.src = `data:${m.file.type};base64,${m.file.data_base64}`;
      img.alt = m.file.name;
      row.append(img);
    } else if (m.file?.data_base64 && m.file.type.startsWith("audio/")) {
      const audio = el("audio");
      audio.controls = true;
      audio.src = `data:${m.file.type};base64,${m.file.data_base64}`;
      row.append(audio);
    } else if (m.file) {
      row.append(el("span", "backup-msg__text", `📎 ${m.file.name}${m.file.data_base64 ? "" : " (not in this backup)"}`));
    } else {
      row.append(el("span", "backup-msg__text", m.deleted ? "This message was deleted" : m.text));
    }
    row.append(el("span", "backup-msg__when", new Date(m.at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })));
    log.append(row);
  }
  openModal(chat.title, back, log);
}
