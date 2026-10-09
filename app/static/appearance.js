// Appearance: wallpaper, bubble colours and shape, font and text size.
// Saved on the server (app/appearance.py), so it follows you to every device.
// applyAppearance() turns the saved choices into CSS variables that styles.css uses.

let look = null;
let lookSaveTimer = null;

const DEFAULT_FONT = "Atkinson Hyperlegible";
const SWATCHES_MINE = ["#0e9f96", "#3a7bd5", "#7c3aed", "#df3f5c", "#e07b24", "#2a9d5c", "#10202e", "#d63384"];
const SWATCHES_THEIRS = ["#ffffff", "#eef2f6", "#fff3d9", "#e6f4ea", "#e8f0fe", "#fce7eb", "#f3e8ff", "#21476b"];
const SWATCHES_WALL = ["#f4f7f9", "#e9e4da", "#dfeee9", "#e4e9f5", "#f6e3e8", "#ece4f7", "#1b2a38", "#0f2233"];
const PATTERN_NAMES = { contours: "Contours", dots: "Dots", grid: "Grid", snow: "Snowfall" };

// Black or white text, whichever reads better on this colour.
function textOn(hex) {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return luminance > 0.36 ? "#10202e" : "#ffffff";
}

function applyAppearance(a) {
  look = a;
  const root = document.documentElement.style;
  root.setProperty("--bubble-mine", a.bubble_mine);
  root.setProperty("--bubble-mine-text", textOn(a.bubble_mine));
  root.setProperty("--bubble-theirs", a.bubble_theirs);
  root.setProperty("--bubble-theirs-text", textOn(a.bubble_theirs));
  root.setProperty("--bubble-r", { round: "20px", soft: "12px", square: "4px" }[a.bubble_shape] || "20px");
  root.setProperty("--bubble-tail", a.bubble_shape === "square" ? "4px" : "6px");
  root.setProperty("--chat-font-size", `${a.font_size}px`);
  root.setProperty("--chat-font", a.font === DEFAULT_FONT ? "var(--font-body)" : `"${a.font}", var(--font-body)`);
  const link = $("chat-font-link");
  const wanted = a.font === DEFAULT_FONT ? "data:text/css," : `https://fonts.googleapis.com/css2?family=${encodeURIComponent(a.font).replace(/%20/g, "+")}:wght@400;700&display=swap`;
  if (link.getAttribute("href") !== wanted) link.setAttribute("href", wanted);

  paintWallpaper($("messages"), a);
}

function paintWallpaper(node, a) {
  node.classList.remove("wall--dark", ...Object.keys(PATTERN_NAMES).map((p) => `wall--${p}`));
  node.style.backgroundColor = "";
  node.style.backgroundImage = "";
  if (a.wallpaper === "color") {
    node.style.backgroundColor = a.wallpaper_value;
    if (textOn(a.wallpaper_value) === "#ffffff") node.classList.add("wall--dark");
  } else if (a.wallpaper === "pattern") {
    node.classList.add(`wall--${a.wallpaper_value}`);
  } else if (a.wallpaper === "image" && a.wallpaper_url) {
    node.style.backgroundImage = `url("${a.wallpaper_url}")`;
    node.classList.add("wall--photo");
  }
}

async function saveLook(changes, quiet = false) {
  try {
    applyAppearance(await api("/api/settings/appearance", { method: "PUT", body: JSON.stringify(changes) }));
    renderAppearanceSettings();
    if (!quiet) flash("Saved");
  } catch (error) {
    flash(error.message);
  }
}

function saveLookSoon(changes) {
  clearTimeout(lookSaveTimer);
  lookSaveTimer = setTimeout(() => saveLook(changes, true), 300);
}

// ---------- the Appearance section in Settings ----------

function swatchRow(colours, current, onPick, label) {
  const row = el("div", "swatches");
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", label);
  for (const colour of colours) {
    const b = el("button", "swatch");
    b.type = "button";
    b.style.background = colour;
    b.title = colour;
    b.setAttribute("aria-label", `${label}: ${colour}`);
    b.setAttribute("aria-pressed", String(colour.toLowerCase() === (current || "").toLowerCase()));
    b.addEventListener("click", () => onPick(colour));
    row.append(b);
  }
  const custom = el("input", "swatch swatch--custom");
  custom.type = "color";
  custom.value = current && current.startsWith("#") ? current : "#888888";
  custom.title = "Pick any colour";
  custom.setAttribute("aria-label", `${label}: any colour`);
  custom.addEventListener("input", () => onPick(custom.value, true));
  row.append(custom);
  return row;
}

function segmentRow(options, current, onPick, label) {
  const row = el("div", "segments");
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", label);
  for (const [value, text] of options) {
    const b = el("button", "segment", text);
    b.type = "button";
    b.setAttribute("aria-pressed", String(value === current));
    b.addEventListener("click", () => onPick(value));
    row.append(b);
  }
  return row;
}

function renderAppearanceSettings() {
  const box = $("appearance");
  if (!box || !look) return;
  const a = look;

  // Live preview
  const preview = el("div", "look-preview");
  paintWallpaper(preview, a);
  const theirs = el("p", "bubble bubble--theirs", "Library at 6?");
  const mine = el("p", "bubble bubble--mine", "See you there 👋");
  preview.append(theirs, mine);

  const parts = [preview];

  parts.push(el("p", "set-label look-label", "Wallpaper"));
  parts.push(segmentRow([["default", "Default"], ["color", "Colour"], ["pattern", "Pattern"], ["image", "Photo"]], a.wallpaper, (kind) => {
    if (kind === "default") saveLook({ wallpaper: "default" });
    else if (kind === "color") saveLook({ wallpaper: "color", wallpaper_value: a.wallpaper === "color" ? a.wallpaper_value : SWATCHES_WALL[1] });
    else if (kind === "pattern") saveLook({ wallpaper: "pattern", wallpaper_value: a.wallpaper === "pattern" ? a.wallpaper_value : "contours" });
    else $("wallpaper-input").click();
  }, "Wallpaper"));
  if (a.wallpaper === "color") {
    parts.push(swatchRow(SWATCHES_WALL, a.wallpaper_value, (c, live) => (live ? saveLookSoon : saveLook)({ wallpaper: "color", wallpaper_value: c }), "Wallpaper colour"));
  } else if (a.wallpaper === "pattern") {
    const row = el("div", "patterns");
    for (const [name, label] of Object.entries(PATTERN_NAMES)) {
      const b = el("button", `pattern-tile wall--${name}`, label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(a.wallpaper_value === name));
      b.addEventListener("click", () => saveLook({ wallpaper: "pattern", wallpaper_value: name }));
      row.append(b);
    }
    parts.push(row);
  }
  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.accept = "image/png,image/jpeg,image/webp";
  fileInput.id = "wallpaper-input";
  fileInput.className = "hidden";
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) return flash("Wallpaper photos can be up to 5 MB.");
    const res = await fetch("/api/settings/wallpaper", { method: "POST", body: file, headers: { "Content-Type": file.type } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return flash(typeof data.detail === "string" ? data.detail : "Couldn't upload that photo.");
    applyAppearance(data);
    renderAppearanceSettings();
    flash("Wallpaper set");
  });
  parts.push(fileInput);

  parts.push(el("p", "set-label look-label", "Your bubbles"));
  parts.push(swatchRow(SWATCHES_MINE, a.bubble_mine, (c, live) => (live ? saveLookSoon : saveLook)({ bubble_mine: c }), "Your bubble colour"));
  parts.push(el("p", "set-label look-label", "Their bubbles"));
  parts.push(swatchRow(SWATCHES_THEIRS, a.bubble_theirs, (c, live) => (live ? saveLookSoon : saveLook)({ bubble_theirs: c }), "Their bubble colour"));
  parts.push(el("p", "set-label look-label", "Bubble shape"));
  parts.push(segmentRow([["round", "Round"], ["soft", "Soft"], ["square", "Square"]], a.bubble_shape, (shape) => saveLook({ bubble_shape: shape }), "Bubble shape"));

  parts.push(el("p", "set-label look-label", "Font"));
  const fontRow = el("div", "font-row");
  const select = el("select", "input select");
  select.setAttribute("aria-label", "Chat font");
  for (const name of a.choices.fonts) {
    const option = el("option", "", name);
    option.value = name;
    option.selected = name === a.font;
    select.append(option);
  }
  select.addEventListener("change", () => saveLook({ font: select.value }));
  const size = el("input", "range");
  size.type = "range";
  size.min = a.choices.font_sizes[0];
  size.max = a.choices.font_sizes[1];
  size.value = a.font_size;
  size.setAttribute("aria-label", "Text size");
  const sizeLabel = el("span", "range-value", `${a.font_size}px`);
  size.addEventListener("input", () => {
    sizeLabel.textContent = `${size.value}px`;
    document.documentElement.style.setProperty("--chat-font-size", `${size.value}px`);
    saveLookSoon({ font_size: Number(size.value) });
  });
  fontRow.append(select, size, sizeLabel);
  parts.push(fontRow);

  const reset = el("button", "btn btn--quiet btn--sm look-reset", "Reset to default look");
  reset.type = "button";
  reset.addEventListener("click", () => saveLook({
    wallpaper: "default", bubble_mine: "#0e9f96", bubble_theirs: "#ffffff", bubble_shape: "round", font: DEFAULT_FONT, font_size: 15,
  }));
  parts.push(reset);
  box.replaceChildren(...parts);
}

hooks.init.push(async () => {
  try {
    applyAppearance(await api("/api/settings/appearance"));
  } catch {}
});

window.renderAppearanceSettings = renderAppearanceSettings;
