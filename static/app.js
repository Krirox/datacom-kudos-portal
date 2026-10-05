const state = {
  me: null,
  feedPage: 1,
  feed: null,
  modPage: 1,
  moderation: null,
};

const loginPanel = document.querySelector("#login-panel");
const appPanel = document.querySelector("#app");
const sessionBar = document.querySelector("#session");
const who = document.querySelector("#who");
const loginMessage = document.querySelector("#login-message");
const formMessage = document.querySelector("#form-message");
const messageBox = document.querySelector("#message");
const count = document.querySelector("#count");
const recipient = document.querySelector("#recipient");
const feed = document.querySelector("#feed");
const moderation = document.querySelector("#moderation");
const moderationList = document.querySelector("#moderation-list");

document.querySelector("#login-form").addEventListener("submit", signIn);
document.querySelector("#logout").addEventListener("click", signOut);
document.querySelector("#kudos-form").addEventListener("submit", sendKudos);
messageBox.addEventListener("input", updateCount);
document.querySelector("#feed-prev").addEventListener("click", () => changeFeed(-1));
document.querySelector("#feed-next").addEventListener("click", () => changeFeed(1));
document.querySelector("#mod-prev").addEventListener("click", () => changeModeration(-1));
document.querySelector("#mod-next").addEventListener("click", () => changeModeration(1));

updateCount();
boot();

async function boot() {
  const me = await api("/api/me");
  if (!me.ok) {
    showLogin();
    return;
  }
  await showApp(me.data);
}

async function signIn(event) {
  event.preventDefault();
  loginMessage.textContent = "";
  loginMessage.className = "message";
  const result = await api("/api/login", {
    method: "POST",
    body: {
      email: document.querySelector("#email").value,
      password: document.querySelector("#password").value,
    },
  });
  if (!result.ok) {
    loginMessage.textContent = result.data.error || "Sign-in failed";
    loginMessage.classList.add("error");
    return;
  }
  await showApp(result.data);
}

async function signOut() {
  await api("/api/logout", { method: "POST", body: {} });
  state.me = null;
  showLogin();
}

async function showApp(me) {
  state.me = me;
  who.textContent = me.role === "admin" ? `${me.name} · Administrator` : me.name;
  sessionBar.hidden = false;
  loginPanel.hidden = true;
  appPanel.hidden = false;
  moderation.hidden = me.role !== "admin";
  await loadColleagues();
  state.feedPage = 1;
  state.modPage = 1;
  await loadFeed();
  if (me.role === "admin") {
    await loadModeration();
  }
}

function showLogin() {
  sessionBar.hidden = true;
  loginPanel.hidden = false;
  appPanel.hidden = true;
}

async function loadColleagues() {
  const result = await api("/api/users");
  recipient.replaceChildren(option("Select a colleague", ""));
  if (!result.ok) {
    return;
  }
  for (const person of result.data) {
    recipient.append(option(person.name, String(person.id)));
  }
}

async function sendKudos(event) {
  event.preventDefault();
  formMessage.textContent = "";
  formMessage.className = "message";
  const recipientId = Number(recipient.value);
  if (!Number.isInteger(recipientId)) {
    setFormMessage("Choose a colleague from the list", true);
    return;
  }
  const button = document.querySelector("#send");
  button.disabled = true;
  const result = await api("/api/kudos", {
    method: "POST",
    body: { recipient_id: recipientId, message: messageBox.value },
  });
  button.disabled = false;
  if (!result.ok) {
    setFormMessage(result.data.error || "Kudos could not be sent", true);
    return;
  }
  messageBox.value = "";
  updateCount();
  setFormMessage("Kudos sent.", false);
  state.feedPage = 1;
  await loadFeed();
  if (state.me.role === "admin") {
    await loadModeration();
  }
}

async function loadFeed() {
  const result = await api(`/api/kudos?page=${state.feedPage}&page_size=10`);
  if (!result.ok) {
    feed.replaceChildren(paragraph(result.data.error || "The feed could not be loaded"));
    return;
  }
  state.feed = result.data;
  renderFeed(feed, result.data.items, false);
  document.querySelector("#feed-count").textContent = `${result.data.total} visible`;
  setPager("#feed-page", "#feed-prev", "#feed-next", result.data);
}

async function loadModeration() {
  const result = await api(`/api/admin/kudos?page=${state.modPage}&page_size=10`);
  if (!result.ok) {
    moderationList.replaceChildren(paragraph(result.data.error || "Moderation could not be loaded"));
    return;
  }
  state.moderation = result.data;
  renderFeed(moderationList, result.data.items, true);
  setPager("#mod-page", "#mod-prev", "#mod-next", result.data);
}

function renderFeed(container, items, withActions) {
  container.replaceChildren();
  if (!items.length) {
    container.append(paragraph("No kudos yet."));
    return;
  }
  for (const item of items) {
    container.append(renderKudo(item, withActions));
  }
}

function renderKudo(item, withActions) {
  const article = document.createElement("article");
  article.className = "kudo";
  const header = document.createElement("header");
  const names = document.createElement("strong");
  names.textContent = `${item.sender_name} gave kudos to ${item.recipient_name}`;
  header.append(names);
  if (withActions) {
    const badge = document.createElement("span");
    badge.className = item.is_visible ? "badge" : "badge hidden";
    badge.textContent = item.is_visible ? "Visible" : "Hidden";
    header.append(badge);
  }
  const body = document.createElement("p");
  body.textContent = item.message;
  const footer = document.createElement("footer");
  const time = document.createElement("time");
  time.dateTime = item.created_at;
  time.textContent = new Date(item.created_at).toLocaleString();
  footer.append(time);
  article.append(header, body, footer);
  if (withActions) {
    article.append(moderationActions(item));
  }
  return article;
}

function moderationActions(item) {
  const row = document.createElement("div");
  row.className = "actions";
  if (item.is_visible) {
    const reason = document.createElement("input");
    reason.type = "text";
    reason.maxLength = 200;
    reason.placeholder = "Reason for hiding";
    reason.setAttribute("aria-label", `Reason for hiding kudos ${item.id}`);
    const hide = document.createElement("button");
    hide.type = "button";
    hide.textContent = "Hide";
    hide.addEventListener("click", () => moderate(item.id, "hide", reason.value));
    row.append(reason, hide);
  } else {
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "ghost";
    restore.textContent = "Restore";
    restore.addEventListener("click", () => moderate(item.id, "restore"));
    row.append(restore);
    if (item.reason_for_moderation) {
      const note = document.createElement("span");
      note.textContent = item.reason_for_moderation;
      row.append(note);
    }
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger";
  remove.textContent = "Delete";
  remove.addEventListener("click", () => moderate(item.id, "delete"));
  row.append(remove);
  return row;
}

async function moderate(id, action) {
  let result;
  if (action === "delete") {
    result = await api(`/api/kudos/${id}`, { method: "DELETE" });
  } else if (action === "restore") {
    result = await api(`/api/kudos/${id}/restore`, { method: "POST", body: {} });
  } else {
    const reason = arguments[2];
    result = await api(`/api/kudos/${id}/hide`, { method: "POST", body: { reason } });
  }
  if (!result.ok) {
    setFormMessage(result.data.error || "Moderation failed", true);
    return;
  }
  setFormMessage("", false);
  await loadFeed();
  await loadModeration();
}

function changeFeed(step) {
  if (!state.feed) return;
  const pages = Math.max(1, Math.ceil(state.feed.total / state.feed.page_size));
  state.feedPage = Math.min(pages, Math.max(1, state.feedPage + step));
  loadFeed();
}

function changeModeration(step) {
  if (!state.moderation) return;
  const pages = Math.max(1, Math.ceil(state.moderation.total / state.moderation.page_size));
  state.modPage = Math.min(pages, Math.max(1, state.modPage + step));
  loadModeration();
}

function setPager(labelId, prevId, nextId, data) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  document.querySelector(labelId).textContent = `Page ${data.page} of ${pages}`;
  document.querySelector(prevId).disabled = data.page <= 1;
  document.querySelector(nextId).disabled = data.page >= pages;
}

function updateCount() {
  const left = 500 - messageBox.value.length;
  count.textContent = `${left} characters left`;
}

function setFormMessage(text, isError) {
  formMessage.textContent = text;
  formMessage.className = isError ? "message error" : "message ok";
}

function option(label, value) {
  const element = document.createElement("option");
  element.value = value;
  element.textContent = label;
  return element;
}

function paragraph(text) {
  const element = document.createElement("p");
  element.className = "empty";
  element.textContent = text;
  return element;
}

async function api(path, options = {}) {
  const settings = {
    method: options.method || "GET",
    headers: { Accept: "application/json" },
  };
  if (options.body !== undefined) {
    settings.headers["Content-Type"] = "application/json";
    settings.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, settings);
  } catch (_error) {
    return { ok: false, data: { error: "The portal could not be reached" } };
  }
  let data = {};
  try {
    data = await response.json();
  } catch (_error) {
    data = { error: "The portal returned an unexpected response" };
  }
  return { ok: response.ok, data };
}
