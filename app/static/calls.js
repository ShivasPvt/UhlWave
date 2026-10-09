// Voice and video calls (one-to-one chats).
//
// How a call connects:
//   1. The caller's browser asks for the mic (and camera) and sends "call_invite".
//   2. The other person sees the incoming-call card and taps Accept.
//   3. The caller makes a WebRTC "offer"; the other browser answers. Both swap
//      network "candidates" until they find a route to each other.
//   4. Sound and video then flow directly between the two browsers.
// The server only relays those setup messages (see app/calls.py).
// Browsers only allow the camera and mic on https:// pages or http://localhost.

const call = {
  id: null, conversationId: null, video: false, role: null, peer: null,
  local: null, pendingCandidates: [], startedAt: null, timer: null, ring: null, incoming: null, iceServers: null,
};

const END_REASONS = {
  declined: "Call declined",
  no_answer: "No answer",
  busy: "They're on another call",
  busy_self: "You're already in a call",
  offline: "They're offline. Try again when they're online.",
  hangup: "Call ended",
  disconnected: "Call dropped",
  answered_elsewhere: "Answered on another device",
  not_allowed: "You can't call in this chat",
  gone: "Call ended",
};

function newCallId() {
  return window.crypto?.randomUUID ? crypto.randomUUID() : `call-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

async function iceServers() {
  call.iceServers ||= (await api("/api/calls/config").catch(() => ({ iceServers: [] }))).iceServers;
  return call.iceServers;
}

async function getMedia(video) {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error("Calls need a secure page. Open the app at http://localhost:8000 or its https:// address.");
  }
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: true, video: video ? { width: { ideal: 1280 }, height: { ideal: 720 } } : false });
  } catch (error) {
    if (video) {
      // No camera, or it's blocked: carry on with voice only.
      try {
        const audioOnly = await navigator.mediaDevices.getUserMedia({ audio: true });
        toast("Camera isn't available, so this is a voice call.");
        return audioOnly;
      } catch {}
    }
    throw new Error(error.name === "NotAllowedError"
      ? "Microphone access is blocked. Allow it for this site in your browser, then try again."
      : "Couldn't find a microphone.");
  }
}

// ---------- screens ----------

function showCallScreen(person, statusText) {
  $("call").classList.remove("hidden");
  $("call").classList.toggle("call--video", call.video);
  const pic = avatar({ ...person, online: false });
  pic.classList.add("avatar--xl");
  $("call-avatar").replaceChildren(pic);
  $("call-name").textContent = person.username;
  setCallStatus(statusText);
  $("call-camera").classList.toggle("hidden", !call.video);
  $("call-mute").setAttribute("aria-pressed", "false");
  $("call-camera").setAttribute("aria-pressed", "false");
  $("call-hangup").focus();
}

function setCallStatus(text) {
  $("call-status").textContent = text;
}

function showLocalVideo() {
  const hasVideo = call.local?.getVideoTracks().length > 0;
  $("call-local").classList.toggle("hidden", !hasVideo);
  if (hasVideo) $("call-local").srcObject = call.local;
}

function startTimer() {
  call.startedAt = Date.now();
  clearInterval(call.timer);
  call.timer = setInterval(() => {
    const s = Math.floor((Date.now() - call.startedAt) / 1000);
    setCallStatus(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
  }, 500);
}

// A soft two-tone ring, made with the Web Audio API (no sound file needed).
function startRinging(outgoing) {
  stopRinging();
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const beep = () => {
      const now = ctx.currentTime;
      for (const [freq, at] of outgoing ? [[440, 0], [480, 0]] : [[660, 0], [880, 0.18]]) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + at);
        gain.gain.exponentialRampToValueAtTime(outgoing ? 0.05 : 0.12, now + at + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + at + (outgoing ? 1 : 0.16));
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + at);
        osc.stop(now + at + 1.05);
      }
    };
    beep();
    call.ring = { ctx, interval: setInterval(beep, outgoing ? 3000 : 1500) };
  } catch {}
}

function stopRinging() {
  if (!call.ring) return;
  clearInterval(call.ring.interval);
  call.ring.ctx.close().catch(() => {});
  call.ring = null;
}

// ---------- the connection ----------

async function makePeer() {
  const peer = new RTCPeerConnection({ iceServers: await iceServers() });
  call.peer = peer;
  for (const track of call.local.getTracks()) peer.addTrack(track, call.local);
  peer.addEventListener("icecandidate", (event) => {
    if (event.candidate) send({ type: "call_signal", call_id: call.id, data: { candidate: event.candidate.toJSON() } });
  });
  peer.addEventListener("track", (event) => {
    const stream = event.streams[0] || new MediaStream([event.track]);
    $("call-remote-audio").srcObject = stream;
    if (event.track.kind === "video") {
      $("call-remote").srcObject = stream;
      $("call").classList.add("call--has-video");
    }
  });
  peer.addEventListener("connectionstatechange", () => {
    if (peer.connectionState === "connected" && !call.startedAt) startTimer();
    if (peer.connectionState === "failed") {
      setCallStatus("Couldn't connect. Your network may block direct calls.");
      setTimeout(() => hangUp(), 2500);
    }
  });
  return peer;
}

async function onSignal(data) {
  const peer = call.peer;
  if (!peer) return;
  if (data.sdp) {
    await peer.setRemoteDescription(data.sdp);
    for (const c of call.pendingCandidates.splice(0)) await peer.addIceCandidate(c).catch(() => {});
    if (data.sdp.type === "offer") {
      const answer = await peer.createAnswer();
      await peer.setLocalDescription(answer);
      send({ type: "call_signal", call_id: call.id, data: { sdp: peer.localDescription.toJSON() } });
    }
  } else if (data.candidate) {
    if (peer.remoteDescription) await peer.addIceCandidate(data.candidate).catch(() => {});
    else call.pendingCandidates.push(data.candidate);
  }
}

// ---------- starting, answering, ending ----------

async function startCall(video) {
  const conversation = findConversation(state.activeId);
  if (!conversation || conversation.type !== "direct" || call.id) return;
  const other = otherMember(conversation);
  try {
    call.local = await getMedia(video);
  } catch (error) {
    return toast(error.message);
  }
  Object.assign(call, { id: newCallId(), conversationId: conversation.id, video: call.local.getVideoTracks().length > 0, role: "caller" });
  showCallScreen(other, "Calling…");
  showLocalVideo();
  if (!send({ type: "call_invite", call_id: call.id, conversation_id: conversation.id, video: call.video })) {
    toast("You're offline. Reconnecting…");
    return cleanUp();
  }
}

async function acceptCall() {
  const incoming = call.incoming;
  if (!incoming) return;
  stopRinging();
  $("incoming").classList.add("hidden");
  try {
    call.local = await getMedia(incoming.video);
  } catch (error) {
    send({ type: "call_decline", call_id: incoming.call_id });
    call.incoming = null;
    return toast(error.message);
  }
  Object.assign(call, { id: incoming.call_id, conversationId: incoming.conversation_id, video: incoming.video, role: "callee", incoming: null });
  showCallScreen(incoming.from, "Connecting…");
  showLocalVideo();
  await makePeer();
  send({ type: "call_accept", call_id: call.id });
  if (state.activeId !== call.conversationId) openConversation(call.conversationId);
}

function declineCall() {
  if (!call.incoming) return;
  send({ type: "call_decline", call_id: call.incoming.call_id });
  call.incoming = null;
  stopRinging();
  $("incoming").classList.add("hidden");
}

function hangUp() {
  if (call.id) send({ type: "call_end", call_id: call.id });
  cleanUp();
}

function cleanUp(message) {
  stopRinging();
  clearInterval(call.timer);
  call.peer?.close();
  call.local?.getTracks().forEach((t) => t.stop());
  $("call-remote").srcObject = null;
  $("call-remote-audio").srcObject = null;
  $("call-local").srcObject = null;
  $("call").classList.add("hidden");
  $("call").classList.remove("call--has-video", "call--video");
  Object.assign(call, { id: null, conversationId: null, peer: null, local: null, pendingCandidates: [], startedAt: null, role: null });
  if (message) toast(message);
}

// ---------- events from the server ----------

hooks.event.push(async (event) => {
  if (!event.type?.startsWith("call_")) return;
  if (event.type === "call_incoming") {
    if (call.id) {
      send({ type: "call_decline", call_id: event.call_id }); // already on a call here
      return;
    }
    call.incoming = event;
    const pic = avatar({ ...event.from, online: false });
    $("incoming-avatar").replaceChildren(pic);
    $("incoming-name").textContent = event.from.username;
    $("incoming-kind").textContent = event.video ? "Incoming video call" : "Incoming voice call";
    $("incoming").classList.remove("hidden");
    $("incoming-accept").focus();
    startRinging(false);
    if (localPrefs.notify && "Notification" in window && Notification.permission === "granted" && !pageVisible()) {
      new Notification(`${event.from.username} is calling`, { body: event.video ? "Video call" : "Voice call", tag: "call" });
    }
    return;
  }
  if (event.type === "call_ended" && call.incoming?.call_id === event.call_id) {
    call.incoming = null;
    stopRinging();
    $("incoming").classList.add("hidden");
    return;
  }
  if (event.call_id !== call.id) return;
  if (event.type === "call_ringing") {
    setCallStatus("Ringing…");
    startRinging(true);
  } else if (event.type === "call_accepted") {
    stopRinging();
    setCallStatus("Connecting…");
    const peer = await makePeer();
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    send({ type: "call_signal", call_id: call.id, data: { sdp: peer.localDescription.toJSON() } });
  } else if (event.type === "call_signal") {
    await onSignal(event.data);
  } else if (event.type === "call_ended") {
    cleanUp(END_REASONS[event.reason] || "Call ended");
  }
});

// ---------- buttons ----------

$("call-voice").addEventListener("click", () => startCall(false));
$("call-video").addEventListener("click", () => startCall(true));
$("call-hangup").addEventListener("click", hangUp);
$("incoming-accept").addEventListener("click", acceptCall);
$("incoming-decline").addEventListener("click", declineCall);

$("call-mute").addEventListener("click", () => {
  const track = call.local?.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  $("call-mute").setAttribute("aria-pressed", String(!track.enabled));
  $("call-mute").setAttribute("aria-label", track.enabled ? "Mute microphone" : "Unmute microphone");
});

$("call-camera").addEventListener("click", () => {
  const track = call.local?.getVideoTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  $("call-camera").setAttribute("aria-pressed", String(!track.enabled));
  $("call-camera").setAttribute("aria-label", track.enabled ? "Turn camera off" : "Turn camera on");
  $("call-local").classList.toggle("call__local--off", !track.enabled);
});

// Closing the tab mid-call hangs up properly.
window.addEventListener("pagehide", () => {
  if (call.id) send({ type: "call_end", call_id: call.id });
});

window.startCall = startCall;
