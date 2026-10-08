// Login / sign-up page. Switches between the two modes and posts the form.
const form = document.getElementById("auth-form");
const errorBox = document.getElementById("form-error");
const submitBtn = document.getElementById("submit-btn");
let mode = "login"; // or "register"

const COPY = {
  login: { title: "Log in", hint: "Welcome back.", button: "Log in", switchText: "New here?", switchBtn: "Create an account" },
  register: { title: "Create an account", hint: "Pick a username people will find you by.", button: "Create account", switchText: "Already have an account?", switchBtn: "Log in" },
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
  errorBox.textContent = "";
}

document.getElementById("switch-mode").addEventListener("click", () => {
  setMode(mode === "login" ? "register" : "login");
});

form.addEventListener("input", () => (errorBox.textContent = ""));

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
    if (password.length < 6) {
      errorBox.textContent = "Passwords need at least 6 characters.";
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
      window.location.href = "/chat";
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
