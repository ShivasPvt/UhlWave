// Polls: make one from the paperclip menu; vote by tapping an option.
// Every vote arrives live for everyone (a "message_updated" event), and the bars
// slide to their new size.

function openNewPoll() {
  const conversation = findConversation(state.activeId);
  if (!conversation) return;
  const form = el("form", "stack");
  const question = el("input");
  question.maxLength = 300;
  question.placeholder = "e.g. Where should we eat after the quiz?";
  const options = el("div", "poll-edit");
  const addOption = (value = "") => {
    if (options.children.length >= 12) return;
    const row = el("div", "poll-edit__row");
    const input = el("input", "input");
    input.maxLength = 100;
    input.value = value;
    input.placeholder = `Option ${options.children.length + 1}`;
    input.setAttribute("aria-label", input.placeholder);
    const remove = el("button", "close-btn", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove option");
    remove.addEventListener("click", () => {
      if (options.children.length > 2) row.remove();
    });
    // Typing in the last option adds a fresh empty one, so you never hunt for a button.
    input.addEventListener("input", () => {
      if (row === options.lastElementChild && input.value.trim()) addOption();
    });
    row.append(input, remove);
    options.append(row);
  };
  addOption();
  addOption();

  const multiple = el("label", "reason");
  const multiBox = el("input");
  multiBox.type = "checkbox";
  multiple.append(multiBox, el("span", "", "Let people pick more than one"));
  const create = primaryButton("Send poll");
  form.append(field("Question", question), el("p", "field__label", "Options"), options, multiple, create);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = [...options.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean);
    if (!question.value.trim()) return modalError("Ask a question.");
    if (values.length < 2) return modalError("Add at least two options.");
    create.disabled = true;
    try {
      await api(`/api/conversations/${conversation.id}/polls`, {
        method: "POST",
        body: JSON.stringify({ question: question.value, options: values, multiple: multiBox.checked }),
      });
      closeModal();
    } catch (error) {
      modalError(error.message);
    } finally {
      create.disabled = false;
    }
  });
  openModal("New poll", form);
}

// ---------- voting ----------
// Your tap shows straight away (we don't wait for the server), votes for one poll are
// sent one at a time so quick taps can't trip over each other, and the server's reply
// redraws the poll even if the live connection is slow or has dropped.
const pollLatest = new Map();   // poll id -> newest message we've seen (from the server)
const pollWanted = new Map();   // poll id -> option ids you picked that the server hasn't confirmed yet
const pollBusy = new Set();     // poll ids with a vote on its way

function myVotes(poll) {
  if (pollWanted.has(poll.id)) return pollWanted.get(poll.id);
  return poll.options.filter((o) => o.voters.includes(state.me.id)).map((o) => o.id);
}

// The poll as it should look right now: the server's counts, with your own
// not-yet-confirmed choice swapped in.
function pollAsShown(poll) {
  if (!pollWanted.has(poll.id)) return poll;
  const mine = new Set(pollWanted.get(poll.id));
  const me = state.me.id;
  const options = poll.options.map((o) => {
    const others = o.voters.filter((id) => id !== me);
    return { ...o, voters: mine.has(o.id) ? [...others, me] : others };
  });
  const voters = new Set(options.flatMap((o) => o.voters));
  return { ...poll, options, voter_count: voters.size };
}

function redrawPoll(message) {
  const row = document.querySelector(`[data-message-id="${message.id}"]`);
  if (row && message.conversation_id === state.activeId) row.replaceWith(renderMessage(message));
}

function castVote(message, optionId) {
  const latest = pollLatest.get(message.poll.id) || message;
  const poll = latest.poll;
  if (poll.closed) return;
  const mine = myVotes(poll);
  let next;
  if (poll.multiple) next = mine.includes(optionId) ? mine.filter((id) => id !== optionId) : [...mine, optionId];
  else next = mine.length === 1 && mine[0] === optionId ? [] : [optionId];  // tap your pick again to take it back
  pollWanted.set(poll.id, next);
  if (navigator.vibrate) navigator.vibrate(8);
  redrawPoll(latest);
  sendVote(latest);
}

async function sendVote(message) {
  const pollId = message.poll.id;
  if (pollBusy.has(pollId)) return;  // the vote on its way will pick up the newest choice when it lands
  pollBusy.add(pollId);
  try {
    for (;;) {
      const wanted = pollWanted.get(pollId);
      const updated = await api(`/api/polls/${pollId}/vote`, { method: "POST", body: JSON.stringify({ option_ids: wanted }) });
      if (pollWanted.get(pollId) === wanted) {
        pollWanted.delete(pollId);   // the server now has exactly what you picked
        onMessageUpdated(updated);
        break;
      }
      // You tapped again while that was sending: send the newer choice too.
    }
  } catch (error) {
    pollWanted.delete(pollId);       // go back to what the server has
    showError(error.message || "Couldn't save your vote.");
    try {
      onMessageUpdated(await api(`/api/polls/${pollId}`));  // e.g. it ended and we missed that
    } catch {
      const latest = pollLatest.get(pollId);
      if (latest) redrawPoll(latest);
    }
  } finally {
    pollBusy.delete(pollId);
  }
}

function showVoters(message) {
  message = pollLatest.get(message.poll.id) || message;
  const conversation = findConversation(message.conversation_id);
  const name = (id) => (id === state.me.id ? "You" : conversation?.members.find((m) => m.id === id)?.username || "Someone who left");
  const parts = pollAsShown(message.poll).options.map((o) => {
    const box = el("div", "voters");
    box.append(el("p", "set-label", `${o.text} · ${o.voters.length}`));
    box.append(el("p", "small-note", o.voters.length ? o.voters.map(name).join(", ") : "No votes yet"));
    return box;
  });
  openModal(message.content, ...parts);
}

function renderPoll(message, mine, kind) {
  pollLatest.set(message.poll.id, message);
  const poll = pollAsShown(message.poll);
  const card = el("div", `bubble ${kind} poll${poll.closed ? " poll--closed" : ""}`);
  card.append(el("p", "poll__question", message.content));
  card.append(el("p", "poll__hint", poll.closed ? "Poll ended · final results" : poll.multiple ? "Pick one or more" : "Pick one"));
  const total = poll.options.reduce((n, o) => n + o.voters.length, 0);
  const top = Math.max(0, ...poll.options.map((o) => o.voters.length));
  for (const option of poll.options) {
    const picked = option.voters.includes(state.me.id);
    const share = total ? Math.round((option.voters.length / total) * 100) : 0;
    const b = el("button", `poll__option${picked ? " poll__option--picked" : ""}${poll.closed && option.voters.length === top && top > 0 ? " poll__option--winner" : ""}`);
    b.type = "button";
    b.disabled = poll.closed;
    b.setAttribute("aria-pressed", String(picked));
    const bar = el("span", "poll__bar");
    // Start from the old width (if this poll was on screen) so the bar slides.
    const previous = document.querySelector(`[data-poll-option="${option.id}"]`)?.style.getPropertyValue("--share");
    bar.style.setProperty("--share", previous || "0%");
    bar.dataset.pollOption = option.id;
    requestAnimationFrame(() => requestAnimationFrame(() => bar.style.setProperty("--share", `${share}%`)));
    const check = el("span", "poll__check", picked ? "✓" : "");
    b.append(bar, check, el("span", "poll__text", option.text), el("span", "poll__count", `${option.voters.length}`));
    b.addEventListener("click", (event) => {
      event.stopPropagation();
      castVote(message, option.id);
    });
    card.append(b);
  }
  const footer = el("div", "poll__footer");
  const who = el("button", "poll__link", `${poll.voter_count} ${poll.voter_count === 1 ? "person" : "people"} voted`);
  who.type = "button";
  who.addEventListener("click", (event) => {
    event.stopPropagation();
    showVoters(message);
  });
  footer.append(who);
  if (mine && !poll.closed) {
    // Two taps to end, so it can't happen by accident (it sits right next to the vote count).
    const end = el("button", "poll__link", "End poll");
    end.type = "button";
    let armed = null;
    end.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (!armed) {
        end.textContent = "Tap again to end";
        end.classList.add("poll__link--warn");
        armed = setTimeout(() => {
          armed = null;
          end.textContent = "End poll";
          end.classList.remove("poll__link--warn");
        }, 3000);
        return;
      }
      clearTimeout(armed);
      end.disabled = true;
      try {
        onMessageUpdated(await api(`/api/polls/${poll.id}/close`, { method: "POST" }));
      } catch (error) {
        end.disabled = false;
        showError(error.message);
      }
    });
    footer.append(end);
  }
  card.append(footer);
  return card;
}

window.renderPoll = renderPoll;
window.openNewPoll = openNewPoll;
