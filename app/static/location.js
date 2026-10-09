// Sharing a location: the pin button in the message bar.
//
// Opens a map with your current location if the browser can find it. If it
// can't (location turned off in Windows, blocked for this site, the page was
// opened over plain http://, or it's just slow), you can still search for a
// place or tap the map, then send exactly the spot you picked.
//
// The map is Leaflet (in /static/vendor) with OpenStreetMap tiles.

const loc = { map: null, marker: null, picked: null, label: "", accuracy: null, searchTimer: null, nameRun: 0 };

const LOCATION_HELP = {
  insecure: "Your browser only shares location on secure pages. Open the app at http://localhost:8000 on this computer, or the https:// address once it's deployed. Meanwhile, search for a place or tap the map.",
  1: "Location is blocked for this site. Click the lock or site icon next to the address bar, allow Location, then try again. Or search for a place below.",
  2: "Your device couldn't work out where you are. On Windows, turn on Settings → Privacy & security → Location (and \"Let desktop apps access your location\"). Or search for a place below.",
  3: "Finding your location took too long. Try again, or search for a place below.",
  none: "This browser can't share its location. Search for a place or tap the map instead.",
};

function openLocationPicker() {
  if (!state.activeId) return;
  const wrap = el("div", "loc-picker");
  const status = el("p", "loc-status", "");
  status.id = "loc-status";
  const mine = el("button", "btn btn--quiet btn--sm", "Use my current location");
  mine.type = "button";
  mine.addEventListener("click", () => locateMe(true));

  const search = el("input", "input");
  search.type = "search";
  search.placeholder = "Search for a place, e.g. North Campus library";
  search.setAttribute("aria-label", "Search for a place");
  const results = el("div", "loc-results hidden");
  search.addEventListener("input", () => {
    clearTimeout(loc.searchTimer);
    const q = search.value.trim();
    if (q.length < 2) return results.classList.add("hidden");
    loc.searchTimer = setTimeout(() => findPlaces(q, results), 450);
  });

  const mapBox = el("div", "loc-mapbox");
  mapBox.id = "loc-map";
  const picked = el("p", "loc-picked", "Tap the map to drop a pin.");
  picked.id = "loc-picked";
  const send = el("button", "btn btn--primary btn--block", "Send this location");
  send.id = "loc-send";
  send.disabled = true;
  send.addEventListener("click", sendPickedLocation);

  wrap.append(mine, status, search, results, mapBox, picked, send);
  openModal("Share a location", wrap);
  loc.picked = null;
  loc.label = "";
  setupMap(mapBox);
  locateMe(false);
}

function setupMap(box) {
  if (loc.map) {
    loc.map.remove();
    loc.map = null;
    loc.marker = null;
  }
  if (!window.L) {
    box.replaceChildren(el("p", "small-note", "The map couldn't load. You can still use your current location or search."));
    return;
  }
  // Start on the campus until we know better.
  loc.map = L.map(box, { zoomControl: true, attributionControl: true }).setView([31.776, 76.986], 14);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap",
  }).addTo(loc.map);
  loc.map.on("click", (event) => pickSpot(event.latlng.lat, event.latlng.lng, null, null));
  // The pop-up animates open; tell Leaflet its real size once it has.
  setTimeout(() => loc.map?.invalidateSize(), 350);
}

function pickSpot(lat, lon, label, accuracy) {
  loc.picked = { latitude: lat, longitude: lon };
  loc.accuracy = accuracy;
  loc.label = label || "";
  if (loc.map) {
    if (!loc.marker) {
      loc.marker = L.marker([lat, lon], { draggable: true }).addTo(loc.map);
      loc.marker.on("dragend", () => {
        const p = loc.marker.getLatLng();
        pickSpot(p.lat, p.lng, null, null);
      });
    } else {
      loc.marker.setLatLng([lat, lon]);
    }
    loc.map.setView([lat, lon], Math.max(loc.map.getZoom(), accuracy && accuracy > 1500 ? 13 : 16));
  }
  $("loc-send").disabled = false;
  drawPicked();
  if (!label) nameSpot(lat, lon);
}

function drawPicked() {
  const p = loc.picked;
  if (!p) return;
  let text = `${loc.label || "Dropped pin"} · ${p.latitude.toFixed(5)}, ${p.longitude.toFixed(5)}`;
  if (loc.accuracy) text += ` · accurate to about ${loc.accuracy >= 1000 ? `${(loc.accuracy / 1000).toFixed(1)} km` : `${Math.round(loc.accuracy)} m`}`;
  $("loc-picked").textContent = text;
}

async function nameSpot(lat, lon) {
  const run = ++loc.nameRun;
  try {
    const { label } = await api(`/api/places/name?lat=${lat}&lon=${lon}`);
    if (run === loc.nameRun && label) {
      loc.label = label;
      drawPicked();
    }
  } catch {}
}

function locateMe(userAsked) {
  const status = $("loc-status");
  if (!status) return;
  if (!window.isSecureContext) {
    status.textContent = LOCATION_HELP.insecure;
    status.classList.add("loc-status--warn");
    return;
  }
  if (!navigator.geolocation) {
    status.textContent = LOCATION_HELP.none;
    status.classList.add("loc-status--warn");
    return;
  }
  status.classList.remove("loc-status--warn");
  status.textContent = "Finding your location…";
  // Fast first: a network-based fix (Wi-Fi), which works on laptops without GPS.
  // Then try for a more precise one in the background.
  const got = (position, precise) => {
    if (!$("loc-status")) return;
    const { latitude, longitude, accuracy } = position.coords;
    if (!loc.picked || loc.accuracy) pickSpot(latitude, longitude, null, accuracy);
    status.textContent = accuracy > 1000
      ? "This is roughly where you are. Drag the pin or tap the map to fix it."
      : precise ? "Found you. Drag the pin if it's slightly off." : "Found you. Improving accuracy…";
  };
  navigator.geolocation.getCurrentPosition(
    (position) => {
      got(position, false);
      navigator.geolocation.getCurrentPosition((p) => {
        if (p.coords.accuracy < (loc.accuracy || Infinity)) got(p, true);
        else if ($("loc-status")) status.textContent = "Found you. Drag the pin if it's slightly off.";
      }, () => {
        if ($("loc-status")) status.textContent = "Found you. Drag the pin if it's slightly off.";
      }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
    },
    (error) => {
      if (!$("loc-status")) return;
      // Only shout about it if they pressed the button; on opening, a quiet hint is enough.
      status.textContent = LOCATION_HELP[error.code] || LOCATION_HELP[2];
      status.classList.toggle("loc-status--warn", userAsked || error.code === 1);
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 5 * 60 * 1000 },
  );
}

async function findPlaces(q, box) {
  let places = [];
  const near = loc.picked || (loc.map ? { latitude: loc.map.getCenter().lat, longitude: loc.map.getCenter().lng } : null);
  const nearQuery = near ? `&lat=${near.latitude}&lon=${near.longitude}` : "";
  try {
    places = await api(`/api/places?q=${encodeURIComponent(q)}${nearQuery}`);
  } catch (error) {
    box.replaceChildren(el("p", "picker__none", error.message));
    box.classList.remove("hidden");
    return;
  }
  box.replaceChildren(...(places.length ? places : [null]).map((place) => {
    if (!place) return el("p", "picker__none", "No places found. Try a nearby town or landmark.");
    const b = el("button", "picker__option loc-option");
    b.type = "button";
    b.append(el("span", "loc-option__name", place.name), el("span", "loc-option__addr", place.address || ""));
    b.addEventListener("click", () => {
      pickSpot(place.latitude, place.longitude, place.name, null);
      box.classList.add("hidden");
    });
    return b;
  }));
  box.classList.remove("hidden");
}

async function sendPickedLocation() {
  const conversationId = state.activeId;
  if (!loc.picked || !conversationId) return;
  const send = $("loc-send");
  send.disabled = true;
  try {
    const message = await api(`/api/conversations/${conversationId}/location`, {
      method: "POST",
      body: JSON.stringify({ ...loc.picked, label: loc.label || null }),
    });
    closeModal();
    // It also arrives over the WebSocket; only draw it if it hasn't.
    if (conversationId === state.activeId && !document.querySelector(`[data-message-id="${message.id}"]`)) {
      appendMessage(renderMessage(message));
    }
  } catch (error) {
    modalError(error.message);
    send.disabled = false;
  }
}

$("loc-btn").addEventListener("click", openLocationPicker);
