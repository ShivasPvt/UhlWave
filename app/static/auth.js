// Login / sign-up page. Switches between the two modes, posts the form,
// and shows the "Continue with Google" button when the server has it set up.
const form = document.getElementById("auth-form");
const errorBox = document.getElementById("form-error");
const submitBtn = document.getElementById("submit-btn");
let mode = "login"; // or "register"
let googleReady = false;

const COPY = {
  login: { title: "Log in", hint: "Welcome back.", button: "Log in", switchText: "New here?", switchBtn: "Create an account", google: "continue_with" },
  register: { title: "Create an account", hint: "Use Google, or pick a username people will find you by.", button: "Create account", switchText: "Already have an account?", switchBtn: "Log in", google: "signup_with" },
};

function setMode(next) {
  mode = next;
  const c = COPY[mode];
  document.getElementById("form-title").textContent = c.title;
  document.getElementById("form-hint").textContent = c.hint;
  document.getElementById("switch-text").textContent = c.switchText;
  document.getElementById("switch-mode").textContent = c.switchBtn;
  submitBtn.textContent = c.button;
  document.getElementById("password").autocomplete = mode === "login" ? "current-password" : "new-password";
  document.getElementById("pw-help").classList.toggle("hidden", mode !== "register");
  document.getElementById("confirm-wrap").classList.toggle("hidden", mode !== "register");
  errorBox.textContent = "";
  nameCheck.reset();
  if (mode === "register" && form.username.value.trim()) nameCheck.run(form.username.value);
  checkPassword();
  renderGoogleButton();
}

document.getElementById("switch-mode").addEventListener("click", () => {
  setMode(mode === "login" ? "register" : "login");
});

form.addEventListener("input", () => {
  errorBox.textContent = "";
  checkPassword();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = form.username.value.trim();
  const password = form.password.value;

  // Check the obvious things before bothering the server.
  if (mode === "register") {
    if (!/^[A-Za-z0-9_]{3,30}$/.test(username)) {
      errorBox.textContent = "Usernames are 3–30 letters, numbers or underscores.";
      return;
    }
    const nameState = await nameCheck.result(username);
    if (nameState && !nameState.available) {
      errorBox.textContent = nameState.reason;
      shake(document.getElementById("username-check"));
      form.username.focus();
      return;
    }
    const check = checkPassword();
    if (!check.ok) {
      errorBox.textContent = check.firstProblem;
      shake(document.getElementById("pw-help"));
      return;
    }
    if (form.confirm.value !== password) {
      errorBox.textContent = "The two passwords don't match.";
      shake(document.getElementById("confirm-wrap"));
      return;
    }
  } else if (!username || !password) {
    errorBox.textContent = "Enter your username and password.";
    return;
  }

  submitBtn.disabled = true;
  try {
    const res = await fetch(`/api/auth/${mode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (res.ok) {
      window.location.href = afterLogin();
      return;
    }
    const data = await res.json().catch(() => ({}));
    errorBox.textContent = typeof data.detail === "string" ? data.detail : "Check your details and try again.";
    if (mode === "register" && res.status === 409) nameCheck.run(username, true);
  } catch {
    errorBox.textContent = "Can't reach the server. Check your connection.";
  } finally {
    submitBtn.disabled = false;
  }
});

// ---------- Google sign-in ----------
// Google's script shows its own button. When someone picks an account, Google
// hands us a signed "ID token" (credential). We pass it to our server, which
// checks it with Google and logs the person in with the usual cookie.

async function onGoogleCredential({ credential }) {
  errorBox.textContent = "";
  try {
    const res = await fetch("/api/auth/google", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ credential }),
    });
    if (res.ok) {
      window.location.href = afterLogin();
      return;
    }
    const data = await res.json().catch(() => ({}));
    errorBox.textContent = typeof data.detail === "string" ? data.detail : "Google sign-in failed. Please try again.";
  } catch {
    errorBox.textContent = "Can't reach the server. Check your connection.";
  }
}

function renderGoogleButton() {
  if (!googleReady) return;
  const box = document.getElementById("google-btn");
  box.replaceChildren();
  google.accounts.id.renderButton(box, {
    type: "standard",
    theme: "outline",
    size: "large",
    shape: "pill",
    text: COPY[mode].google,
    logo_alignment: "left",
    width: Math.min(box.clientWidth || 320, 400),
  });
}

function showSetupNote(text) {
  // Only developers running it locally see this; real visitors just don't get the button.
  if (!["localhost", "127.0.0.1"].includes(location.hostname)) return;
  const note = document.getElementById("google-setup-note");
  note.textContent = text;
  note.classList.remove("hidden");
}

(async function setupGoogle() {
  let clientId = null;
  try {
    clientId = (await (await fetch("/api/auth/config")).json()).google_client_id;
  } catch {
    return;
  }
  if (!clientId) {
    showSetupNote("Google sign-in is off. Add GOOGLE_CLIENT_ID to .env and restart to switch it on.");
    return;
  }

  const script = document.createElement("script");
  script.src = "https://accounts.google.com/gsi/client";
  script.async = true;
  script.onload = () => {
    google.accounts.id.initialize({ client_id: clientId, callback: onGoogleCredential, ux_mode: "popup" });
    document.getElementById("google-area").classList.remove("hidden");
    googleReady = true;
    renderGoogleButton();
  };
  script.onerror = () => showSetupNote("Couldn't load Google's sign-in script. Check your connection.");
  document.head.append(script);
})();

// Where to go after logging in: back to a group invite link if that's how you got here.
function afterLogin() {
  const next = new URLSearchParams(location.search).get("next") || "";
  return /^\/join\/[A-Za-z0-9_-]{4,40}$/.test(next) ? next : "/chat";
}


// ---------- password strength (when creating an account) ----------
// The same required rules are checked on the server (password_problem in app/auth.py).

const COMMON = new Set([
  "password", "password1", "password123", "passw0rd", "12345678", "123456789", "1234567890", "qwerty123",
  "qwertyuiop", "iloveyou", "11111111", "abc12345", "abcd1234", "letmein1", "welcome1", "admin123",
  "football1", "princess1", "sunshine1", "monkey123", "india123", "iitmandi", "iitmandi123", "uhlwave1",
]);
const LEVELS = ["Too weak", "Weak", "Okay", "Strong", "Excellent"];

function checkPassword() {
  const password = form.password.value;
  const username = form.username.value.trim().toLowerCase();
  const lowered = password.toLowerCase();
  const rules = {
    length: password.length >= 8,
    mix: /[A-Za-z]/.test(password) && /\d/.test(password),
    name: !(username.length >= 3 && lowered.includes(username)),
    common: password.length > 0 && !COMMON.has(lowered) && new Set(password).size >= 4,
  };
  const messages = {
    length: "Use at least 8 characters.",
    mix: "Use both letters and numbers.",
    name: "Don't put your username in your password.",
    common: "That password is too easy to guess.",
  };
  const ok = Object.values(rules).every(Boolean);

  // Strength: the rules, then extra points for length, mixed case and symbols.
  let score = 0;
  if (ok) {
    score = 1;
    if (password.length >= 12) score++;
    if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
    if (/[^A-Za-z0-9]/.test(password)) score++;
    if (password.length >= 16 && score < 4) score++;
  }
  const tips = [];
  if (ok && password.length < 12) tips.push("12+ characters");
  if (ok && !(/[a-z]/.test(password) && /[A-Z]/.test(password))) tips.push("upper and lower case");
  if (ok && !/[^A-Za-z0-9]/.test(password)) tips.push("a symbol like - or !");

  if (mode === "register") {
    for (const li of document.querySelectorAll(".pw-rules li")) {
      const pass = rules[li.dataset.rule];
      if (li.classList.contains("pw-rule--ok") !== pass) {
        li.classList.toggle("pw-rule--ok", pass);
        if (pass) {
          li.classList.remove("pw-rule--pop");
          void li.offsetWidth; // restart the little pop animation
          li.classList.add("pw-rule--pop");
        }
      }
    }
    const meter = document.querySelector(".pw-meter");
    meter.dataset.score = password ? String(score) : "";
    document.getElementById("pw-meter-label").textContent = password ? `Strength: ${LEVELS[score]}` : "Type a password";
    document.getElementById("pw-tips").textContent = tips.length && score < 4 ? `Make it stronger with ${tips.join(", ")}.` : "";
    const confirm = form.confirm.value;
    const note = document.getElementById("confirm-note");
    note.textContent = !confirm ? "" : confirm === password ? "✓ Passwords match" : "Doesn't match yet";
    note.classList.toggle("field__hint--ok", Boolean(confirm) && confirm === password);
  }
  const firstProblem = Object.entries(rules).find(([, pass]) => !pass);
  return { ok, score, firstProblem: firstProblem ? messages[firstProblem[0]] : "" };
}

function shake(node) {
  node.classList.remove("shake");
  void node.offsetWidth;
  node.classList.add("shake");
}

// Show / hide what you're typing.
document.getElementById("pw-toggle").addEventListener("click", () => {
  const button = document.getElementById("pw-toggle");
  const show = button.getAttribute("aria-pressed") !== "true";
  button.setAttribute("aria-pressed", String(show));
  button.setAttribute("aria-label", show ? "Hide password" : "Show password");
  for (const id of ["password", "confirm"]) document.getElementById(id).type = show ? "text" : "password";
});

// A memorable passphrase: three words, a number and a symbol, e.g. "Cedar-Monsoon-Yak-42!".
const WORDS = [
  "cedar", "monsoon", "yak", "glacier", "deodar", "river", "kettle", "lantern", "maple", "comet", "pebble",
  "saffron", "tundra", "falcon", "orchid", "summit", "breeze", "ember", "willow", "quartz", "mango", "otter",
];
document.getElementById("pw-suggest").addEventListener("click", () => {
  const pick = () => WORDS[crypto.getRandomValues(new Uint32Array(1))[0] % WORDS.length];
  const cap = (w) => w[0].toUpperCase() + w.slice(1);
  const number = 10 + (crypto.getRandomValues(new Uint32Array(1))[0] % 90);
  const phrase = `${cap(pick())}-${cap(pick())}-${cap(pick())}-${number}!`;
  form.password.value = phrase;
  form.confirm.value = phrase;
  document.getElementById("pw-toggle").setAttribute("aria-pressed", "false");
  document.getElementById("pw-toggle").click(); // show it so they can note it down
  checkPassword();
});


// ---------- is this username free? ----------
// Usernames are one per person and stored in lowercase ("Meera" and "meera" are
// the same name). While you type on the sign-up form we ask the server, after a
// short pause, and show the answer right under the box, with a few free
// alternatives you can tap if it's taken.
const nameCheck = (() => {
  const input = document.getElementById("username");
  const box = document.getElementById("username-check");
  const note = document.getElementById("username-note");
  const suggest = document.getElementById("username-suggest");
  const cache = new Map();   // lowercase name -> server answer
  let timer = null;
  let latest = "";

  function show(kind, text) {
    box.classList.remove("hidden");
    note.className = `field__hint${kind ? ` field__hint--${kind}` : ""}`;
    note.textContent = text;
    input.classList.toggle("input--ok", kind === "ok");
    input.classList.toggle("input--bad", kind === "bad");
    input.setAttribute("aria-invalid", String(kind === "bad"));
  }

  function showSuggestions(list) {
    suggest.replaceChildren();
    suggest.classList.toggle("hidden", !list?.length);
    if (!list?.length) return;
    suggest.append(document.createTextNode("Free:"));
    for (const name of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = `@${name}`;
      b.addEventListener("click", () => {
        input.value = name;
        run(name);
        input.focus();
      });
      suggest.append(b);
    }
  }

  function localProblem(name) {
    if (name.length < 3) return "At least 3 characters.";
    if (name.length > 30) return "At most 30 characters.";
    if (!/^[a-z0-9_]+$/.test(name)) return "Only letters, numbers and underscores.";
    return null;
  }

  async function ask(name, fresh = false) {
    if (!fresh && cache.has(name)) return cache.get(name);
    const res = await fetch(`/api/auth/username-available?username=${encodeURIComponent(name)}`);
    if (!res.ok) throw new Error(res.status === 429 ? "rate" : "fail");
    const data = await res.json();
    cache.set(name, data);
    return data;
  }

  function render(data, typed) {
    if (data.available) {
      show("ok", typed !== data.username ? `✓ Available. You'll be @${data.username} (usernames are lowercase)` : `✓ @${data.username} is available`);
      showSuggestions([]);
    } else {
      show("bad", `✗ ${data.reason}`);
      showSuggestions(data.suggestions);
    }
  }

  function run(raw, fresh = false) {
    clearTimeout(timer);
    const typed = raw.trim();
    const name = typed.toLowerCase();
    latest = name;
    if (mode !== "register") return reset();
    if (!name) return reset();
    const problem = localProblem(name);
    if (problem) {
      show("bad", problem);
      showSuggestions([]);
      return;
    }
    if (!fresh && cache.has(name)) return render(cache.get(name), typed);
    show("busy", "Checking…");
    timer = setTimeout(async () => {
      try {
        const data = await ask(name, fresh);
        if (latest === name) render(data, typed);
      } catch (error) {
        if (latest === name) show("busy", error.message === "rate" ? "Checking paused for a moment…" : "Couldn't check right now; we'll check when you sign up.");
      }
    }, fresh ? 0 : 350);
  }

  function reset() {
    clearTimeout(timer);
    box.classList.add("hidden");
    input.classList.remove("input--ok", "input--bad");
    input.removeAttribute("aria-invalid");
    showSuggestions([]);
  }

  // The answer for this exact name (asks the server if we don't know yet).
  async function result(raw) {
    const name = raw.trim().toLowerCase();
    try {
      return await ask(name);
    } catch {
      return null;  // the server will check again when the form is sent
    }
  }

  input.addEventListener("input", () => run(input.value));
  return { run, reset, result };
})();
