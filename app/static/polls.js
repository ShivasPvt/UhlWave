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

async function castVote(message, optionId) {
  const poll = message.poll;
  const mine = poll.options.filter((o) => o.voters.includes(state.me.id)).map((o) => o.id);
  let next;
  if (poll.multiple) next = mine.includes(optionId) ? mine.filter((id) => id !== optionId) : [...mine, optionId];
  else next = mine.length === 1 && mine[0] === optionId ? [] : [optionId];
  try {
    await api(`/api/polls/${poll.id}/vote`, { method: "POST", body: JSON.stringify({ option_ids: next }) });
  } catch (error) {
    showError(error.message);
  }
}

function showVoters(message) {
  const conversation = findConversation(message.conversation_id);
  const name = (id) => (id === state.me.id ? "You" : conversation?.members.find((m) => m.id === id)?.username || "Someone who left");
  const parts = message.poll.options.map((o) => {
    const box = el("div", "voters");
    box.append(el("p", "set-label", `${o.text} · ${o.voters.length}`));
    box.append(el("p", "small-note", o.voters.length ? o.voters.map(name).join(", ") : "No votes yet"));
    return box;
  });
  openModal(message.content, ...parts);
}

function renderPoll(message, mine, kind) {
  const poll = message.poll;
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
    const end = el("button", "poll__link", "End poll");
    end.type = "button";
    end.addEventListener("click", async (event) => {
      event.stopPropagation();
      await api(`/api/polls/${poll.id}/close`, { method: "POST" }).catch((e) => showError(e.message));
    });
    footer.append(end);
  }
  card.append(footer);
  return card;
}

window.renderPoll = renderPoll;
window.openNewPoll = openNewPoll;
