// Group chats: the "New group" and "Group info" windows.
// Loaded after chat.js, so it uses $, el, api, state, avatar and friends from there.

let modalLastFocus = null;

// Something to run when the current pop-up goes away (e.g. turn the camera off).
let modalCleanup = null;
function onModalClose(fn) {
  modalCleanup = fn;
}
function runModalCleanup() {
  const fn = modalCleanup;
  modalCleanup = null;
  try { fn?.(); } catch (error) { console.warn(error); }
}

function openModal(title, ...content) {
  runModalCleanup();
  modalLastFocus = document.activeElement;
  $("modal-title").textContent = title;
  $("modal-body").replaceChildren(...content);
  $("modal-error").textContent = "";
  $("modal").classList.remove("hidden");
  $("modal").querySelector("input, button:not(#close-modal)")?.focus();
}

function closeModal() {
  runModalCleanup();
  $("modal").classList.add("hidden");
  $("modal-body").replaceChildren();
  modalLastFocus?.focus();
}

function modalError(text) {
  $("modal-error").textContent = text;
}

$("close-modal").addEventListener("click", closeModal);
$("modal-backdrop").addEventListener("click", closeModal);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("modal").classList.contains("hidden")) closeModal();
});

function field(labelText, input) {
  const wrap = el("label", "field");
  wrap.append(el("span", "field__label", labelText), input);
  input.classList.add("input");
  return wrap;
}

function primaryButton(text) {
  const b = el("button", "btn btn--primary btn--block", text);
  b.type = "submit";
  return b;
}

// A username box that suggests people as you type. Calls onPick(user).
function personPicker(onPick, exclude = () => new Set()) {
  const wrap = el("div", "picker");
  const input = el("input");
  input.placeholder = "Search by username";
  input.autocomplete = "off";
  const list = el("div", "picker__list hidden");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) return list.classList.add("hidden");
    timer = setTimeout(async () => {
      const users = (await api(`/api/users?q=${encodeURIComponent(q)}`)).filter((u) => !exclude().has(u.id));
      list.replaceChildren(...(users.length ? users : [null]).map((user) => {
        if (!user) return el("p", "picker__none", "No one found.");
        const option = el("button", "picker__option");
        option.type = "button";
        option.append(avatar({ ...user, online: false }, true), el("span", "", user.username));
        option.addEventListener("click", () => {
          onPick(user);
          input.value = "";
          list.classList.add("hidden");
          input.focus();
        });
        return option;
      }));
      list.classList.remove("hidden");
    }, 200);
  });
  wrap.append(field("Add people", input), list);
  return wrap;
}

// ---------- New group ----------

function openNewGroup() {
  const chosen = new Map(); // id -> user
  const form = el("form", "stack");
  const name = el("input");
  name.maxLength = 60;
  name.placeholder = "e.g. DBMS study squad";
  name.required = true;

  const chips = el("div", "chips");
  function drawChips() {
    if (chosen.size === 0) return chips.replaceChildren(el("p", "chips__empty", "Nobody added yet. You can add people later too."));
    chips.replaceChildren(...[...chosen.values()].map((user) => {
      const chip = el("button", "chip", user.username);
      chip.type = "button";
      chip.title = `Remove ${user.username}`;
      chip.append(el("span", "", "×"));
      chip.addEventListener("click", () => {
        chosen.delete(user.id);
        drawChips();
      });
      return chip;
    }));
  }
  drawChips();

  const picker = personPicker((user) => {
    chosen.set(user.id, user);
    drawChips();
  }, () => new Set(chosen.keys()));

  const create = primaryButton("Create group");
  form.append(field("Group name", name), picker, chips, create);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    create.disabled = true;
    try {
      const group = await api("/api/groups", {
        method: "POST",
        body: JSON.stringify({ name: name.value, usernames: [...chosen.values()].map((u) => u.username) }),
      });
      closeModal();
      await loadConversations();
      await openConversation(group.id);
    } catch (error) {
      modalError(error.message);
    } finally {
      create.disabled = false;
    }
  });
  openModal("New group", form);
}

$("new-group").addEventListener("click", openNewGroup);

// ---------- Group info ----------

async function groupAction(promise, reopen = true) {
  try {
    await promise;
    await loadConversations();
    if (reopen) openGroupInfo();
  } catch (error) {
    modalError(error.message);
  }
}

function openGroupInfo() {
  const group = findConversation(state.activeId);
  if (!group || group.type !== "group") return;
  const iAmAdmin = group.i_am_admin;
  const parts = [];

  // Photo
  const top = el("div", "group-top");
  const pic = conversationAvatar(group);
  pic.classList.add("avatar--xl");
  const photoButtons = el("div", "photo-actions");
  const change = el("button", "btn btn--quiet btn--sm", group.avatar_url ? "Change photo" : "Add group photo");
  const fileInput = el("input");
  fileInput.type = "file";
  fileInput.accept = "image/png,image/jpeg,image/webp,image/gif";
  fileInput.className = "hidden";
  const groupPhotoJob = {
    title: "Group photo",
    shape: "square",
    onSave: async (blob) => {
      await PhotoEditor.upload(`/api/groups/${group.id}/photo`, blob);
      await loadConversations();
      setTimeout(openGroupInfo);  // after the editor closes, show the group again
    },
  };
  change.addEventListener("click", () => PhotoEditor.choose(groupPhotoJob));
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (file) PhotoEditor.fromFile(file, groupPhotoJob);
  });
  photoButtons.append(change, fileInput);
  if (group.avatar_url) {
    const remove = el("button", "btn btn--danger btn--sm", "Remove");
    remove.addEventListener("click", () => groupAction(api(`/api/groups/${group.id}/photo`, { method: "DELETE" })));
    photoButtons.append(remove);
  }
  top.append(pic, photoButtons);
  parts.push(top);

  // Name and description
  const infoForm = el("form", "stack");
  const name = el("input");
  name.value = group.title;
  name.maxLength = 60;
  const description = el("textarea");
  description.value = group.description || "";
  description.maxLength = 500;
  description.rows = 3;
  description.placeholder = "What's this group for? Rules, links, meeting times…";
  description.classList.add("input--area");
  const save = el("button", "btn btn--primary", "Save");
  infoForm.append(field("Group name", name), field("Description", description), save);
  infoForm.addEventListener("submit", (event) => {
    event.preventDefault();
    groupAction(api(`/api/groups/${group.id}`, { method: "PATCH", body: JSON.stringify({ name: name.value, description: description.value }) }));
  });
  parts.push(infoForm, el("hr", "rule"));

  // Invite link (admins)
  if (iAmAdmin) {
    const box = el("div", "invite");
    box.append(el("p", "set-label", "Invite link"));
    if (group.invite_code) {
      const url = `${location.origin}/join/${group.invite_code}`;
      const link = el("input", "input invite__url");
      link.value = url;
      link.readOnly = true;
      link.setAttribute("aria-label", "Invite link");
      link.addEventListener("focus", () => link.select());
      const copy = el("button", "btn btn--primary btn--sm", "Copy link");
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(url);
          copy.textContent = "Copied";
        } catch {
          link.select();
          copy.textContent = "Press Ctrl+C";
        }
        setTimeout(() => (copy.textContent = "Copy link"), 2000);
      });
      const reset = el("button", "btn btn--quiet btn--sm", "Reset link");
      reset.title = "Make a new link. The old one stops working.";
      reset.addEventListener("click", () => groupAction(api(`/api/groups/${group.id}/invite`, { method: "POST" })));
      const off = el("button", "btn btn--danger btn--sm", "Turn off");
      off.addEventListener("click", () => groupAction(api(`/api/groups/${group.id}/invite`, { method: "DELETE" })));
      const buttons = el("div", "photo-actions");
      buttons.append(copy, reset, off);
      box.append(link, buttons, el("p", "small-note", "Anyone with this link can join. Only admins can see, reset or turn it off."));
    } else {
      const make = el("button", "btn btn--quiet btn--sm", "Create invite link");
      make.addEventListener("click", () => groupAction(api(`/api/groups/${group.id}/invite`, { method: "POST" })));
      box.append(el("p", "small-note", "Share a link so people can join without being added one by one."), make);
    }
    parts.push(box, el("hr", "rule"));
  }

  // Members, with search
  parts.push(el("p", "members__count", plural(group.members.length, "member")));
  const filter = el("input", "input");
  filter.type = "search";
  filter.placeholder = "Search members";
  filter.setAttribute("aria-label", "Search members");
  const list = el("div", "members");
  const drawMembers = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = group.members.filter((m) => !q || m.username.includes(q) || (m.tag || "").toLowerCase().includes(q));
    if (!shown.length) return list.replaceChildren(el("p", "small-note", "No members match."));
    list.replaceChildren(...shown.map((member) => memberRow(group, member, iAmAdmin)));
  };
  filter.addEventListener("input", drawMembers);
  drawMembers();
  if (group.members.length > 5) parts.push(filter);
  parts.push(list);

  // Add people
  parts.push(personPicker((user) => groupAction(api(`/api/groups/${group.id}/members`, { method: "POST", body: JSON.stringify({ username: user.username }) })),
    () => new Set(group.members.map((m) => m.id))));
  parts.push(el("p", "small-note", iAmAdmin
    ? "You're an admin: you can remove people, choose other admins, set anyone's tag and manage the invite link."
    : "Anyone can add people and edit the name, photo and description. Admins can remove people."));

  // Leave (two clicks)
  const leave = el("button", "btn btn--danger btn--block", "Leave group");
  leave.addEventListener("click", async () => {
    if (!leave.dataset.armed) {
      leave.dataset.armed = "1";
      leave.textContent = "Tap again to leave";
      return;
    }
    try {
      await api(`/api/groups/${group.id}/members/${state.me.id}`, { method: "DELETE" });
      closeModal();
      await loadConversations();
      closeConversation();
    } catch (error) {
      modalError(error.message);
    }
  });
  parts.push(leave);
  openModal("Group info", ...parts);
}

function memberRow(group, member, iAmAdmin) {
  const me = member.id === state.me.id;
  const row = el("div", "member");
  const who = el("button", "member__who");
  who.type = "button";
  who.title = `View ${member.username}'s profile`;
  const label = el("span", "member__text");
  label.append(el("span", "member__name", me ? `${member.username} (you)` : member.username));
  const badges = el("span", "member__badges");
  if (member.role === "admin") badges.append(el("span", "member__role", "Admin"));
  if (member.tag) badges.append(el("span", "member-tag", member.tag));
  label.append(badges);
  who.append(avatar(member), label);
  if (!member.is_bot) who.addEventListener("click", () => window.openProfile?.(member.username));
  row.append(who);

  const actions = el("div", "member__actions");
  if (me || iAmAdmin) {
    const tag = el("button", "act-btn", member.tag ? "Edit tag" : "Add tag");
    tag.title = "A short label shown next to the name, like Class rep";
    tag.addEventListener("click", () => editTag(group, member));
    actions.append(tag);
  }
  if (iAmAdmin && !member.is_bot) {
    const role = el("button", "act-btn", member.role === "admin" ? (me ? "Step down" : "Dismiss admin") : "Make admin");
    role.addEventListener("click", () => groupAction(api(`/api/groups/${group.id}/members/${member.id}/role`, {
      method: "PUT", body: JSON.stringify({ role: member.role === "admin" ? "member" : "admin" }),
    })));
    actions.append(role);
  }
  if (iAmAdmin && !me) {
    const remove = el("button", "act-btn act-btn--danger", "Remove");
    remove.addEventListener("click", () => groupAction(api(`/api/groups/${group.id}/members/${member.id}`, { method: "DELETE" })));
    actions.append(remove);
  }
  row.append(actions);
  return row;
}

function editTag(group, member) {
  const form = el("form", "stack");
  const input = el("input");
  input.value = member.tag || "";
  input.maxLength = 30;
  input.placeholder = "e.g. Class rep, Treasurer, Goalkeeper";
  const save = primaryButton("Save tag");
  form.append(field(`Tag for ${member.id === state.me.id ? "you" : member.username}`, input), save,
    el("p", "small-note", "Shown next to the name in this group. Leave empty to remove it."));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    groupAction(api(`/api/groups/${group.id}/members/${member.id}/tag`, { method: "PUT", body: JSON.stringify({ tag: input.value }) }));
  });
  openModal("Member tag", form);
}

$("group-info").addEventListener("click", openGroupInfo);
