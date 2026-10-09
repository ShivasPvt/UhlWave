// Profile and group photos: pick one from the device or take one with the camera,
// then drag and zoom it so the right part sits inside the round (or rounded-square)
// frame. What gets uploaded is a square 512×512 JPEG of exactly what was in the frame,
// so photos always fit their circle everywhere in the app.
// Loaded after groups.js (uses openModal, closeModal, modalError, onModalClose).

const PhotoEditor = (() => {
  const OUTPUT = 512;            // pixels per side of the saved photo
  const MAX_ZOOM = 4;
  const canUseCamera = !!navigator.mediaDevices?.getUserMedia;

  // ---- step 1: where does the photo come from? ----
  function choose({ title = "Profile photo", shape = "circle", onSave }) {
    const job = { title, shape, onSave };
    const list = el("div", "photo-source");
    list.append(
      sourceButton("🖼️", "Choose from device", "A photo from your files or gallery", () => pickFile(job)),
      sourceButton("📷", "Take a photo", canUseCamera ? "Use your camera" : "Opens your camera app", () => takePhoto(job)),
    );
    openModal(title, list);
  }

  function sourceButton(icon, label, note, onClick) {
    const b = el("button", "choice");
    b.type = "button";
    const text = el("span");
    text.append(document.createTextNode(label), el("small", "", note));
    b.append(el("span", "choice__icon", icon), text);
    b.addEventListener("click", onClick);
    return b;
  }

  function fileInput(capture) {
    const input = el("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp,image/gif,image/heic,image/heif";
    if (capture) input.setAttribute("capture", "user");
    return input;
  }

  function pickFile(job, capture = false) {
    const input = fileInput(capture);
    input.addEventListener("change", () => {
      const file = input.files[0];
      if (file) fromFile(file, job);
    });
    input.click();
  }

  // Open the cropper for a file (also used when a file is dropped onto the hidden inputs).
  async function fromFile(file, job) {
    if (!file.type.startsWith("image/") && !/\.(jpe?g|png|webp|gif|heic|heif)$/i.test(file.name)) {
      return openModal(job.title, el("p", "small-note", "That isn't a photo. Try a JPG or PNG."));
    }
    if (file.size > 25 * 1024 * 1024) {
      return openModal(job.title, el("p", "small-note", "That photo is too big (over 25 MB)."));
    }
    try {
      const image = await loadImage(file);
      crop(image, job);
    } catch {
      openModal(job.title, el("p", "small-note", "Couldn't open that photo. Try a JPG or PNG."));
    }
  }

  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve({ source: img, width: img.naturalWidth, height: img.naturalHeight });
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("bad image"));
      };
      img.src = url;
    });
  }

  // ---- step 1b: take a photo with the camera ----
  async function takePhoto(job) {
    if (!canUseCamera) return pickFile(job, true);  // old browsers: the phone's camera app
    const stage = el("div", "crop__stage");
    const video = el("video");
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    const frame = el("div", `crop__frame${job.shape === "square" ? " crop__frame--square" : ""}`);
    const flashLayer = el("div", "crop__flash");
    stage.append(video, frame, flashLayer);
    const hint = el("p", "crop__hint", "Starting the camera…");
    const shoot = el("button", "btn btn--primary", "📸 Capture");
    shoot.type = "button";
    shoot.disabled = true;
    const flip = el("button", "btn btn--quiet", "🔄 Flip");
    flip.type = "button";
    flip.classList.add("hidden");
    const actions = el("div", "crop__actions");
    actions.append(flip, shoot);
    const box = el("div", "crop");
    box.append(stage, hint, actions);
    openModal("Take a photo", box);

    let stream = null;
    let facing = "user";
    const stop = () => stream?.getTracks().forEach((t) => t.stop());
    onModalClose(stop);

    async function start() {
      stop();
      shoot.disabled = true;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 1280 } },
          audio: false,
        });
      } catch (error) {
        hint.textContent = error?.name === "NotAllowedError"
          ? "Camera blocked. Allow camera access in your browser, or choose a photo instead."
          : "Couldn't start a camera. Choose a photo from your device instead.";
        const instead = el("button", "btn btn--quiet", "Choose from device");
        instead.type = "button";
        instead.addEventListener("click", () => pickFile(job));
        actions.replaceChildren(instead);
        return;
      }
      if (!$("modal-body").contains(video)) return stop();  // closed while asking for permission
      video.srcObject = stream;
      video.style.transform = facing === "user" ? "scaleX(-1)" : "none";
      await video.play().catch(() => {});
      hint.textContent = "Fit your face in the circle, then capture.";
      shoot.disabled = false;
      const cameras = (await navigator.mediaDevices.enumerateDevices().catch(() => [])).filter((d) => d.kind === "videoinput");
      flip.classList.toggle("hidden", cameras.length < 2);
    }

    flip.addEventListener("click", () => {
      facing = facing === "user" ? "environment" : "user";
      start();
    });
    shoot.addEventListener("click", () => {
      const w = video.videoWidth;
      const h = video.videoHeight;
      if (!w || !h) return;
      const still = document.createElement("canvas");
      still.width = w;
      still.height = h;
      const ctx = still.getContext("2d");
      if (facing === "user") {
        // Keep the photo the way the preview looked (like a mirror), as phones do for selfies.
        ctx.translate(w, 0);
        ctx.scale(-1, 1);
      }
      ctx.drawImage(video, 0, 0, w, h);
      flashLayer.classList.add("crop__flash--on");
      requestAnimationFrame(() => flashLayer.classList.remove("crop__flash--on"));
      setTimeout(() => crop({ source: still, width: w, height: h }, job, () => takePhoto(job)), 180);
    });
    start();
  }

  // ---- step 2: drag and zoom into the frame ----
  function crop(image, job, retake = null) {
    const stage = el("div", "crop__stage");
    const canvas = el("canvas");
    const frame = el("div", `crop__frame${job.shape === "square" ? " crop__frame--square" : ""}`);
    stage.append(canvas, frame);
    stage.tabIndex = 0;
    stage.setAttribute("role", "img");
    stage.setAttribute("aria-label", "Photo preview. Drag to move it, use the slider or arrow keys to adjust.");

    const zoomOut = el("button", "crop__zoom", "−");
    const zoomIn = el("button", "crop__zoom", "+");
    const rotate = el("button", "btn btn--quiet btn--sm", "⟳ Rotate");
    for (const b of [zoomOut, zoomIn, rotate]) b.type = "button";
    zoomOut.setAttribute("aria-label", "Zoom out");
    zoomIn.setAttribute("aria-label", "Zoom in");
    const slider = el("input");
    slider.type = "range";
    slider.min = "1";
    slider.max = String(MAX_ZOOM);
    slider.step = "0.01";
    slider.value = "1";
    slider.setAttribute("aria-label", "Zoom");
    const tools = el("div", "crop__tools");
    tools.append(zoomOut, slider, zoomIn, rotate);

    const save = el("button", "btn btn--primary", "Save photo");
    save.type = "button";
    const back = el("button", "btn btn--quiet", retake ? "Retake" : "Choose another");
    back.type = "button";
    const actions = el("div", "crop__actions");
    actions.append(back, save);
    const box = el("div", "crop");
    box.append(stage, el("p", "crop__hint", "Drag to move · scroll or pinch to zoom"), tools, actions);
    openModal(job.title, box);

    // View state: zoom (1 = photo just covers the frame), offset of the photo's centre
    // from the frame's centre (in frame pixels), and rotation in quarter turns.
    let zoom = 1;
    let x = 0;
    let y = 0;
    let turns = 0;
    let size = 0;  // frame size in CSS pixels

    const rotated = () => turns % 2 === 1;
    const dims = () => (rotated() ? [image.height, image.width] : [image.width, image.height]);
    const baseScale = () => {
      const [w, h] = dims();
      return Math.max(size / w, size / h);
    };
    function clamp() {
      const [w, h] = dims();
      const s = baseScale() * zoom;
      const maxX = Math.max(0, (w * s - size) / 2);
      const maxY = Math.max(0, (h * s - size) / 2);
      x = Math.min(maxX, Math.max(-maxX, x));
      y = Math.min(maxY, Math.max(-maxY, y));
    }
    function paint(ctx, side) {
      const k = side / size;
      ctx.save();
      ctx.fillStyle = "#0b1620";
      ctx.fillRect(0, 0, side, side);
      ctx.translate(side / 2 + x * k, side / 2 + y * k);
      ctx.rotate((turns * Math.PI) / 2);
      const s = baseScale() * zoom * k;
      ctx.scale(s, s);
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(image.source, -image.width / 2, -image.height / 2);
      ctx.restore();
    }
    function draw() {
      size = stage.clientWidth || 300;
      const dpr = window.devicePixelRatio || 1;
      const side = Math.round(size * dpr);
      if (canvas.width !== side) {
        canvas.width = side;
        canvas.height = side;
      }
      clamp();
      paint(canvas.getContext("2d"), side);
    }
    function setZoom(value, aroundX = 0, aroundY = 0) {
      const next = Math.min(MAX_ZOOM, Math.max(1, value));
      // Zoom towards the pointer, so the spot under your finger stays put.
      const ratio = next / zoom;
      x = aroundX + (x - aroundX) * ratio;
      y = aroundY + (y - aroundY) * ratio;
      zoom = next;
      slider.value = String(zoom);
      draw();
    }

    // Dragging with mouse, pen or one finger; pinching with two.
    const pointers = new Map();
    let pinch = null;
    stage.addEventListener("pointerdown", (event) => {
      stage.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), zoom };
      }
    });
    stage.addEventListener("pointermove", (event) => {
      const last = pointers.get(event.pointerId);
      if (!last) return;
      const now = { x: event.clientX, y: event.clientY };
      pointers.set(event.pointerId, now);
      if (pointers.size === 2 && pinch) {
        const [a, b] = [...pointers.values()];
        setZoom(pinch.zoom * (Math.hypot(a.x - b.x, a.y - b.y) / pinch.distance));
      } else if (pointers.size === 1) {
        x += now.x - last.x;
        y += now.y - last.y;
        draw();
      }
    });
    const lift = (event) => {
      pointers.delete(event.pointerId);
      if (pointers.size < 2) pinch = null;
    };
    stage.addEventListener("pointerup", lift);
    stage.addEventListener("pointercancel", lift);
    stage.addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = stage.getBoundingClientRect();
      const ax = event.clientX - rect.left - rect.width / 2;
      const ay = event.clientY - rect.top - rect.height / 2;
      setZoom(zoom * Math.exp(-event.deltaY * 0.0015), ax, ay);
    }, { passive: false });
    stage.addEventListener("keydown", (event) => {
      const step = event.shiftKey ? 30 : 8;
      const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
      if (moves[event.key]) {
        event.preventDefault();
        x += moves[event.key][0];
        y += moves[event.key][1];
        draw();
      } else if (event.key === "+" || event.key === "=") setZoom(zoom * 1.1);
      else if (event.key === "-") setZoom(zoom / 1.1);
    });

    slider.addEventListener("input", () => setZoom(Number(slider.value)));
    zoomIn.addEventListener("click", () => setZoom(zoom * 1.2));
    zoomOut.addEventListener("click", () => setZoom(zoom / 1.2));
    rotate.addEventListener("click", () => {
      turns = (turns + 1) % 4;
      [x, y] = [-y, x];
      draw();
    });
    back.addEventListener("click", () => (retake ? retake() : choose(job)));

    save.addEventListener("click", async () => {
      save.disabled = true;
      save.textContent = "Saving…";
      try {
        const out = document.createElement("canvas");
        out.width = OUTPUT;
        out.height = OUTPUT;
        paint(out.getContext("2d"), OUTPUT);
        const blob = await new Promise((resolve) => out.toBlob(resolve, "image/jpeg", 0.9));
        if (!blob) throw new Error("Couldn't make the photo.");
        await job.onSave(blob);
        closeModal();
      } catch (error) {
        modalError(error.message || "Couldn't save that photo.");
        save.disabled = false;
        save.textContent = "Save photo";
      }
    });

    const resize = () => draw();
    window.addEventListener("resize", resize);
    onModalClose(() => window.removeEventListener("resize", resize));
    requestAnimationFrame(draw);
  }

  // Upload a finished photo. Throws with the server's message if it's refused.
  async function upload(url, blob) {
    const res = await fetch(url, { method: "POST", body: blob, headers: { "Content-Type": "image/jpeg" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Couldn't upload that photo.");
    return data;
  }

  return { choose, fromFile, upload };
})();

window.PhotoEditor = PhotoEditor;
