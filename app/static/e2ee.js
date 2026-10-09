// Secret chats: end-to-end encryption, done entirely in this browser with Web Crypto.
//
//   Your key pair      ECDH P-256, made here the first time you set up secret chats.
//   Your passphrase    turns into a key (PBKDF2-SHA256, 310,000 rounds) that seals your
//                      private key with AES-GCM. Only the sealed copy goes to the server,
//                      so you can unlock on another device; the server can't open it.
//   Each secret chat   your private key + their public key -> ECDH -> HKDF -> an AES-256-GCM
//                      key that only the two browsers can work out.
//   Each message       "e2e1:" + base64(random 12-byte IV + ciphertext). The chat id is
//                      bound in as extra authenticated data, so a message can't be moved
//                      to another chat.
//
// This browser keeps your unlocked private key in IndexedDB as a non-exportable
// key (scripts can use it but can't read it out). "Lock on this device" forgets it.

const E2EE = { privateKey: null, publicKey: null, server: null, chatKeys: new Map() };
const PBKDF2_ROUNDS = 310000;
const te = new TextEncoder();
const td = new TextDecoder();

// ---------- small helpers ----------

const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const fromB64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
const randomBytes = (n) => crypto.getRandomValues(new Uint8Array(n));

function publicText(jwk) {
  // The same text every time for the same key, so it can be compared.
  return JSON.stringify({ kty: "EC", crv: "P-256", x: jwk.x, y: jwk.y });
}

function db() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("uhlwave-e2ee", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("keys");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function store(action, key, value) {
  const handle = await db();
  return new Promise((resolve, reject) => {
    const tx = handle.transaction("keys", action === "get" ? "readonly" : "readwrite");
    const keys = tx.objectStore("keys");
    const req = action === "get" ? keys.get(key) : action === "put" ? keys.put(value, key) : keys.delete(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function passphraseKey(passphrase, salt) {
  const base = await crypto.subtle.importKey("raw", te.encode(passphrase), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: PBKDF2_ROUNDS, hash: "SHA-256" },
    base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"],
  );
}

// Keep the private key on this device, as a key that can be used but not exported.
async function rememberHere(privateJwk, publicKeyText) {
  const privateKey = await crypto.subtle.importKey("jwk", privateJwk, { name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
  E2EE.privateKey = privateKey;
  E2EE.publicKey = publicKeyText;
  E2EE.chatKeys.clear();
  try {
    await store("put", `user:${state.me.id}`, { privateKey, publicKey: publicKeyText });
  } catch {} // private windows may not keep it; you'll just unlock again next time
}

// ---------- setting up, unlocking, locking ----------

async function createKeys(passphrase) {
  const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const publicJwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  const privateJwk = await crypto.subtle.exportKey("jwk", pair.privateKey);
  const salt = randomBytes(16);
  const iv = randomBytes(12);
  const sealedBytes = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await passphraseKey(passphrase, salt), te.encode(JSON.stringify(privateJwk)));
  const sealed = { v: 1, kdf: "PBKDF2-SHA256", iterations: PBKDF2_ROUNDS, salt: toB64(salt), iv: toB64(iv), ct: toB64(sealedBytes) };
  const publicKey = publicText(publicJwk);
  await api("/api/e2ee", { method: "PUT", body: JSON.stringify({ public_key: publicKey, private_wrapped: JSON.stringify(sealed) }) });
  await rememberHere(privateJwk, publicKey);
  E2EE.server = await api("/api/e2ee");
}

async function unlockWith(passphrase) {
  const server = await api("/api/e2ee");
  E2EE.server = server;
  if (!server.private_wrapped) throw new Error("Secret chats aren't set up yet.");
  const sealed = JSON.parse(server.private_wrapped);
  let plain;
  try {
    const key = await passphraseKey(passphrase, fromB64(sealed.salt));
    plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: fromB64(sealed.iv) }, key, fromB64(sealed.ct));
  } catch {
    throw new Error("That passphrase isn't right.");
  }
  await rememberHere(JSON.parse(td.decode(plain)), server.public_key);
}

async function lockHere() {
  E2EE.privateKey = null;
  E2EE.chatKeys.clear();
  await store("delete", `user:${state.me.id}`).catch(() => {});
}

async function loadFromThisDevice() {
  E2EE.server = await api("/api/e2ee").catch(() => null);
  let saved = null;
  try {
    saved = await store("get", `user:${state.me.id}`);
  } catch {}
  if (saved && E2EE.server?.public_key && saved.publicKey === E2EE.server.public_key) {
    E2EE.privateKey = saved.privateKey;
    E2EE.publicKey = saved.publicKey;
  } else if (saved) {
    await lockHere(); // keys were reset on another device; this copy is out of date
  }
}

const isSetUp = () => Boolean(E2EE.server?.public_key);
const isUnlocked = () => Boolean(E2EE.privateKey);

// ---------- encrypting and decrypting ----------

function partnerOf(conversation) {
  return conversation.members.find((m) => m.id !== state.me.id);
}

async function chatKey(conversation) {
  const partner = partnerOf(conversation);
  if (!partner?.public_key) throw new Error("The other person's key is missing.");
  const cacheKey = `${conversation.id}|${partner.public_key}|${E2EE.publicKey}`;
  if (E2EE.chatKeys.has(cacheKey)) return E2EE.chatKeys.get(cacheKey);
  const theirs = await crypto.subtle.importKey("jwk", JSON.parse(partner.public_key), { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: theirs }, E2EE.privateKey, 256);
  const hkdf = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: te.encode("uhlwave-secret-chat-v1"), info: te.encode(`conversation:${conversation.id}`) },
    hkdf, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"],
  );
  E2EE.chatKeys.set(cacheKey, key);
  return key;
}

async function encryptFor(conversation, text) {
  const iv = randomBytes(12);
  const sealed = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: te.encode(`c${conversation.id}`) }, await chatKey(conversation), te.encode(text),
  );
  const out = new Uint8Array(12 + sealed.byteLength);
  out.set(iv);
  out.set(new Uint8Array(sealed), 12);
  return `e2e1:${toB64(out)}`;
}

async function decryptFrom(conversation, payload) {
  const bytes = fromB64(payload.slice(5));
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: bytes.slice(0, 12), additionalData: te.encode(`c${conversation.id}`) },
    await chatKey(conversation), bytes.slice(12),
  );
  return td.decode(plain);
}

async function openText(conversation, payload) {
  if (!conversation) return "🔒 Encrypted message";
  if (!isUnlocked()) return "🔒 Unlock secret chats to read this message";
  try {
    return await decryptFrom(conversation, payload);
  } catch {
    return "⚠️ This message can't be decrypted here (a security code changed after it was sent)";
  }
}

// Every message passes through here before it's drawn (see hooks.transform in chat.js).
hooks.transform.push(async (message) => {
  if (typeof message.content !== "string") return message;
  const needsMain = message.content.startsWith("e2e1:");
  const needsQuote = typeof message.reply_to?.content === "string" && message.reply_to.content.startsWith("e2e1:");
  if (!needsMain && !needsQuote) return message;
  const conversation = findConversation(message.conversation_id);
  const out = { ...message };
  if (needsMain) {
    out.content = await openText(conversation, message.content);
    out.encrypted = true;
  }
  if (needsQuote) out.reply_to = { ...message.reply_to, content: await openText(conversation, message.reply_to.content) };
  return out;
});

// ...and every message you send in a secret chat is encrypted here first.
hooks.outgoing.push(async (conversation, text) => {
  if (conversation?.type !== "secret") return text;
  if (!isUnlocked()) {
    openUnlock();
    throw new Error("Unlock secret chats on this device to send.");
  }
  return encryptFor(conversation, text);
});

// ---------- the security code ----------
// Both people see the same 60 digits only if nobody swapped a key in between.

async function securityCode(conversation) {
  const keys = conversation.members.map((m) => m.public_key || "").sort();
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", te.encode(keys.join("|"))));
  const groups = [];
  for (let i = 0; i < 12; i++) {
    const n = ((digest[i * 2] << 16) | (digest[i * 2 + 1] << 8) | digest[(i * 2 + 7) % 32]) % 100000;
    groups.push(String(n).padStart(5, "0"));
  }
  return groups;
}

async function showSecurityCode(conversation = findConversation(state.activeId)) {
  if (!conversation || conversation.type !== "secret") return;
  const partner = partnerOf(conversation);
  const grid = el("div", "safety-code");
  for (const group of await securityCode(conversation)) grid.append(el("span", "", group));
  openModal("Security code", grid,
    el("p", "small-note", `Compare these numbers with ${partner?.username || "the other person"}, in person or on a call. If they match on both phones or laptops, your secret chat is end-to-end encrypted with nobody in the middle.`));
}

// ---------- pop-ups: set up and unlock ----------

function passphraseField(label, autocomplete) {
  const input = el("input");
  input.type = "password";
  input.autocomplete = autocomplete;
  input.maxLength = 128;
  return [field(label, input), input];
}

function openSetup(onDone) {
  const form = el("form", "stack");
  const [pf, pass] = passphraseField("Choose a passphrase", "new-password");
  const [cf, confirm] = passphraseField("Type it again", "new-password");
  const meter = el("div", "pw-meter");
  meter.setAttribute("aria-hidden", "true");
  meter.append(el("i"), el("i"), el("i"), el("i"));
  pass.addEventListener("input", () => {
    const p = pass.value;
    let score = p.length >= 8 ? 1 : 0;
    if (p.length >= 12) score++;
    if (/[^A-Za-z0-9]/.test(p) || /\s/.test(p)) score++;
    if (p.length >= 18) score++;
    meter.dataset.score = p ? String(score) : "";
  });
  const go = primaryButton("Create my keys");
  form.append(
    el("p", "small-note", "Secret chats are encrypted in your browser, so not even the server can read them. Your passphrase protects your key; you'll need it to read secret chats on a new device."),
    pf, meter, cf,
    el("p", "small-note small-note--warn", "If you forget it, you can reset, but secret messages from before can't be read again. Nobody can recover it for you."),
    go,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pass.value.length < 8) return modalError("Use at least 8 characters. A short sentence works well.");
    if (pass.value !== confirm.value) return modalError("The two passphrases don't match.");
    go.disabled = true;
    go.textContent = "Creating your keys…";
    try {
      await createKeys(pass.value);
      closeModal();
      toast("Secret chats are ready on this device 🔒");
      await afterKeysChanged();
      onDone?.();
    } catch (error) {
      modalError(error.message);
      go.disabled = false;
      go.textContent = "Create my keys";
    }
  });
  openModal("Set up secret chats", form);
}

function openUnlock(onDone) {
  if (!isSetUp()) return openSetup(onDone);
  const form = el("form", "stack");
  const [pf, pass] = passphraseField("Your secret-chat passphrase", "current-password");
  const go = primaryButton("Unlock");
  const forgot = el("button", "link-btn", "Forgot it? Reset your keys");
  forgot.type = "button";
  forgot.addEventListener("click", confirmReset);
  form.append(el("p", "small-note", "Enter your passphrase once to read and send secret chats on this device."), pf, go, forgot);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    go.disabled = true;
    go.textContent = "Unlocking…";
    try {
      await unlockWith(pass.value);
      closeModal();
      toast("Secret chats unlocked on this device");
      await afterKeysChanged();
      onDone?.();
    } catch (error) {
      modalError(error.message);
      go.disabled = false;
      go.textContent = "Unlock";
    }
  });
  openModal("Unlock secret chats", form);
}

function confirmReset() {
  const form = el("form", "stack");
  const go = el("button", "btn btn--danger-solid btn--block", "Reset my keys");
  form.append(
    el("p", "", "Resetting makes new keys. Secret messages sent before can't be read any more, by you or the other person, and everyone you have a secret chat with is told your security code changed."),
    go,
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    openSetup();
  });
  openModal("Reset secret-chat keys?", form);
}

// After unlocking or new keys: redraw everything so messages decrypt.
async function afterKeysChanged() {
  E2EE.chatKeys.clear();
  await loadConversations();
  if (state.activeId) await loadHistory();
  renderE2EESettings();
}

// ---------- starting a secret chat ----------

async function startSecretChat(username) {
  const go = async () => {
    try {
      const conversation = await api("/api/secret-chats", { method: "POST", body: JSON.stringify({ username }) });
      await loadConversations();
      state.filter = "all";
      await openConversation(conversation.id);
    } catch (error) {
      toast(error.message);
    }
  };
  if (!isSetUp()) return openSetup(go);
  if (!isUnlocked()) return openUnlock(go);
  go();
}

// ---------- Settings → Secret chats ----------

function renderE2EESettings() {
  const box = $("e2ee-settings");
  if (!box) return;
  const parts = [];
  if (!isSetUp()) {
    parts.push(el("p", "set-label", "End-to-end encrypted chats"));
    parts.push(el("p", "set-desc", "Start a 🔒 secret chat from someone's profile. Messages are encrypted in your browser and only the two of you can read them, not even the server. @ai, files and search aren't available in secret chats."));
    const setup = el("button", "btn btn--primary", "Set up secret chats");
    setup.addEventListener("click", () => openSetup());
    parts.push(setup);
  } else if (isUnlocked()) {
    parts.push(el("p", "set-label e2ee-ok", "✓ Secret chats are ready on this device"));
    parts.push(el("p", "set-desc", "Your private key is stored here in a form scripts can use but can't copy out. Lock it if this is a shared computer."));
    const row = el("div", "photo-actions");
    const lock = el("button", "btn btn--quiet btn--sm", "Lock on this device");
    lock.addEventListener("click", async () => {
      await lockHere();
      await afterKeysChanged();
      flash("Locked. Your passphrase unlocks it again.");
    });
    const reset = el("button", "btn btn--danger btn--sm", "Reset keys");
    reset.addEventListener("click", confirmReset);
    row.append(lock, reset);
    parts.push(row);
  } else {
    parts.push(el("p", "set-label", "Secret chats are locked on this device"));
    parts.push(el("p", "set-desc", "Enter your passphrase once to read and send secret chats here."));
    const row = el("div", "photo-actions");
    const unlock = el("button", "btn btn--primary btn--sm", "Unlock");
    unlock.addEventListener("click", () => openUnlock());
    const reset = el("button", "btn btn--danger btn--sm", "Forgot passphrase");
    reset.addEventListener("click", confirmReset);
    row.append(unlock, reset);
    parts.push(row);
  }
  box.replaceChildren(...parts);
}

// ---------- the chat screen ----------

hooks.headerRendered.push((conversation) => {
  const secret = conversation.type === "secret";
  $("chat-view").classList.toggle("chat--secret", secret);
  const locked = secret && !isUnlocked();
  $("secret-banner").classList.toggle("hidden", !locked);
  $("secret-banner-text").textContent = !isSetUp()
    ? "Set up secret chats on this device to read and send messages here."
    : "Unlock secret chats on this device to read and send messages here.";
  $("secret-unlock").textContent = isSetUp() ? "Unlock" : "Set up";
  if (secret) {
    $("chat-status").textContent = "🔒 End-to-end encrypted";
    $("mic-btn").disabled = true; // voice notes would go through the server unencrypted
    if (locked) $("message-input").disabled = true;
  }
  $("message-input").placeholder = secret ? "Secret message (end-to-end encrypted)" : "Message, @ai, or /imagine a picture";
});

$("secret-unlock").addEventListener("click", () => openUnlock());

hooks.init.push(async () => {
  await loadFromThisDevice();
  if (state.conversations.some((c) => c.type === "secret")) {
    await loadConversations();
    if (state.activeId) await loadHistory();
  }
});

window.startSecretChat = startSecretChat;
window.showSecurityCode = showSecurityCode;
window.renderE2EESettings = renderE2EESettings;
window.E2EE = E2EE;
