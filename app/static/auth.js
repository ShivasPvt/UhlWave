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
