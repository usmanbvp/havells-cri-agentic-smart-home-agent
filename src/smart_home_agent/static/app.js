const historyKey = "havells-home-chat";
const conversation = document.querySelector("#conversation-scroll");
const welcome = document.querySelector("#welcome-block");
const messageList = document.querySelector("#message-list");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const providerLabel = document.querySelector("#provider-label");

let history = loadHistory();
let busy = false;

function loadHistory() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(historyKey) || "[]");
    if (!Array.isArray(saved)) return [];
    return saved.filter(
      (item) =>
        item &&
        (item.role === "user" || item.role === "assistant") &&
        typeof item.content === "string",
    ).slice(-12);
  } catch (error) {
    console.warn("Could not restore the chat history for this tab.", error);
    return [];
  }
}

function saveHistory() {
  sessionStorage.setItem(historyKey, JSON.stringify(history.slice(-12)));
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /\[([^\]]+)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)|\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|`([^`]+)`|\*(.+?)\*|_([^_]+)_/g;
  let lastIndex = 0;

  for (const match of text.matchAll(pattern)) {
    if (match.index > lastIndex) {
      parent.append(document.createTextNode(text.slice(lastIndex, match.index)));
    }

    if (match[1] !== undefined) {
      const label = match[1];
      let url;
      try {
        url = new URL(match[2], window.location.href);
      } catch {
        parent.append(document.createTextNode(label));
        lastIndex = match.index + match[0].length;
        continue;
      }

      if (["http:", "https:", "mailto:"].includes(url.protocol)) {
        const link = element("a", "", label);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } else {
        parent.append(document.createTextNode(label));
      }
    } else if (match[3] !== undefined || match[4] !== undefined) {
      const strong = element("strong");
      appendInlineMarkdown(strong, match[3] ?? match[4]);
      parent.append(strong);
    } else if (match[5] !== undefined) {
      const del = element("del");
      appendInlineMarkdown(del, match[5]);
      parent.append(del);
    } else if (match[6] !== undefined) {
      parent.append(element("code", "", match[6]));
    } else {
      const emphasis = element("em");
      appendInlineMarkdown(emphasis, match[7] ?? match[8]);
      parent.append(emphasis);
    }
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parent.append(document.createTextNode(text.slice(lastIndex)));
  }
}

function markdownCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isMarkdownTableDivider(line) {
  const cells = markdownCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderMarkdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s*```/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const code = element("code", "", codeLines.join("\n"));
      const pre = element("pre", "markdown-code");
      pre.append(code);
      fragment.append(pre);
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      const headingElement = element(`h${heading[1].length}`, "markdown-heading");
      appendInlineMarkdown(headingElement, heading[2]);
      fragment.append(headingElement);
      index += 1;
      continue;
    }

    if (/^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)) {
      fragment.append(element("hr", "markdown-divider"));
      index += 1;
      continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
        index += 1;
      }
      const quote = element("blockquote", "markdown-quote");
      quote.append(renderMarkdown(quoteLines.join("\n")));
      fragment.append(quote);
      continue;
    }

    if (index + 1 < lines.length && line.includes("|") && isMarkdownTableDivider(lines[index + 1])) {
      const table = element("table", "markdown-table");
      const header = element("thead");
      const headerRow = element("tr");
      for (const cell of markdownCells(line)) {
        const th = element("th");
        appendInlineMarkdown(th, cell);
        headerRow.append(th);
      }
      header.append(headerRow);
      table.append(header);
      index += 2;

      const body = element("tbody");
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = element("tr");
        for (const cell of markdownCells(lines[index])) {
          const td = element("td");
          appendInlineMarkdown(td, cell);
          row.append(td);
        }
        body.append(row);
        index += 1;
      }
      table.append(body);
      const wrapper = element("div", "markdown-table-wrap");
      wrapper.append(table);
      fragment.append(wrapper);
      continue;
    }

    const listMatch = line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = element(ordered ? "ol" : "ul", "markdown-list");
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
        if (!itemMatch || /^\d/.test(itemMatch[1]) !== ordered) break;
        const item = element("li");
        appendInlineMarkdown(item, itemMatch[2]);
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim()) {
      if (
        /^\s{0,3}(?:#{1,6}\s|```|>|[-+*]\s|\d+[.)]\s)/.test(lines[index]) ||
        /^\s{0,3}(?:[-*_]\s*){3,}$/.test(lines[index]) ||
        (lines[index].includes("|") && index + 1 < lines.length && isMarkdownTableDivider(lines[index + 1]))
      ) {
        break;
      }
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    const paragraph = element("p", "markdown-paragraph");
    appendInlineMarkdown(paragraph, paragraphLines.join(" "));
    fragment.append(paragraph);
  }

  return fragment;
}

function formatState(device) {
  const state = device.state;
  if (!state.power) return "Powered off";
  const details = [];
  if (device.type === "air_conditioner") {
    details.push(`${state.temperature}°C`, state.mode);
  } else {
    details.push(`Speed ${state.fan_speed}`);
  }
  return `On · ${details.join(" · ")}`;
}

function deviceIcon(device) {
  if (device.type === "fan") {
    return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M11.4 9.9C8.1 5.3 9.7 3 12 3c2.5 0 3 3.7 1.3 7.1M13.9 11.5c5.7-1.4 7.3.6 6.1 2.7-1.2 2.1-4.9.8-7.1-1M12.7 13.7c.7 5.7-1.6 7-3.6 5.7-2.1-1.4-.2-5 2.4-6.9" stroke="currentColor" stroke-width="1.45" stroke-linecap="round"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3v18m-5.8-15 11.6 12m0-12L6.2 18M4 12h16" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="12" cy="12" r="2" stroke="currentColor" stroke-width="1.4"/></svg>';
}

function renderDevices(devices) {
  const list = document.querySelector("#device-list");
  list.replaceChildren();

  for (const device of devices) {
    const card = element("article", "device-card");
    const icon = element("span", `device-icon${device.state.power ? " is-on" : ""}`);
    icon.innerHTML = deviceIcon(device);
    const copy = element("span", "device-copy");
    const detail = device.state.power
      ? `${device.room} · ${device.type === "fan" ? `Speed ${device.state.fan_speed}` : `${device.state.temperature}°C · ${device.state.mode}`}`
      : `${device.room} · ${device.type === "fan" ? "Fan" : "Air conditioner"}`;
    copy.append(
      element("span", "device-name", device.name),
      element("span", "device-detail", detail),
    );
    const status = element("span", `device-status${device.state.power ? " is-on" : ""}`);
    status.append(element("span"), document.createTextNode(device.state.power ? "On" : "Off"));
    card.append(icon, copy, status);
    card.title = formatState(device);
    list.append(card);
  }

  const rooms = new Set(devices.map((device) => device.room));
  document.querySelector("#device-count").textContent = String(devices.length).padStart(2, "0");
  document.querySelector("#room-count").textContent = String(rooms.size).padStart(2, "0");
  document.querySelector("#active-count").textContent = String(
    devices.filter((device) => device.state.power).length,
  ).padStart(2, "0");
  document.querySelector("#device-section-count").textContent = String(devices.length).padStart(2, "0");
}

function formatRule(rule) {
  if (rule.kind === "phrase") return `Say “${rule.trigger_phrase}”`;
  if (rule.kind === "time") return `After ${rule.after_time} local time`;
  return "When a device is turned on";
}

function renderRules(rules) {
  const list = document.querySelector("#rule-list");
  list.replaceChildren();
  document.querySelector("#rule-section-count").textContent = String(rules.length).padStart(2, "0");
  if (!rules.length) {
    list.append(element("div", "empty-rule", "Tell your assistant what to remember and your routines will show up here."));
    return;
  }

  const symbols = { default: "↻", phrase: "⌁", time: "◷" };
  for (const rule of rules) {
    const card = element("article", `rule-card${rule.enabled ? "" : " rule-disabled"}`);
    const symbol = element("span", "rule-symbol", symbols[rule.kind] || "•");
    const copy = element("span", "rule-copy");
    copy.append(
      element("span", "rule-name", rule.name),
      element("span", "rule-detail", `${formatRule(rule)}${rule.enabled ? "" : " · Paused"}`),
    );
    card.append(symbol, copy);
    list.append(card);
  }
}

async function refreshHome() {
  const [healthResponse, devicesResponse, rulesResponse] = await Promise.all([
    fetch("/api/health"),
    fetch("/api/devices"),
    fetch("/api/rules"),
  ]);
  for (const response of [healthResponse, devicesResponse, rulesResponse]) {
    if (!response.ok) throw new Error(`Home data could not be loaded (HTTP ${response.status}).`);
  }
  const [health, deviceData, ruleData] = await Promise.all([
    healthResponse.json(),
    devicesResponse.json(),
    rulesResponse.json(),
  ]);
  const providerNames = Array.isArray(health.providers) && health.providers.length
    ? health.providers.map((name) => name[0].toUpperCase() + name.slice(1)).join(" → ")
    : health.model;
  providerLabel.textContent = `${providerNames} · ${health.timezone}`;
  renderDevices(deviceData.devices);
  renderRules(ruleData.rules);
}

function renderMessage(role, content, options = {}) {
  welcome.hidden = true;
  welcome.style.display = "none";
  const message = element(
    "article",
    `message ${role === "user" ? "user-message" : "assistant-message"}${options.error ? " message-error" : ""}`,
  );
  if (options.error) message.setAttribute("role", "alert");

  if (role !== "user") {
    message.append(element("span", "message-avatar", "H"));
  }

  const contentArea = role === "user" ? message : element("div", "assistant-message-content");
  const bubble = element("div", "message-bubble");
  if (role !== "user" && !options.error) {
    bubble.append(renderMarkdown(content));
  } else {
    bubble.textContent = content;
  }
  contentArea.append(bubble);
  if (role !== "user") {
    const label = options.error ? "Temporarily unavailable" : "Home assistant";
    contentArea.append(element("div", "message-meta", label));
  }
  if (role !== "user" && options.actions?.length) {
    const details = element("details", "tool-details");
    details.append(element("summary", "", `${options.actions.length} agent action${options.actions.length === 1 ? "" : "s"}`));
    const output = element("pre");
    output.textContent = JSON.stringify(options.actions, null, 2);
    details.append(output);
    contentArea.append(details);
  }
  if (role !== "user") message.append(contentArea);
  messageList.append(message);
  conversation.scrollTop = conversation.scrollHeight;
  return message;
}

function showTyping() {
  const message = element("article", "message assistant-message");
  message.dataset.typing = "true";
  message.append(element("span", "message-avatar", "H"));
  const content = element("div", "assistant-message-content");
  const indicator = element("div", "typing-indicator");
  indicator.setAttribute("aria-label", "Assistant is thinking");
  indicator.append(element("span"), element("span"), element("span"));
  content.append(indicator);
  message.append(content);
  messageList.append(message);
  conversation.scrollTop = conversation.scrollHeight;
  return message;
}

async function sendMessage(text) {
  const message = text.trim();
  if (!message || busy) return;
  busy = true;
  input.value = "";
  resizeInput();
  sendButton.disabled = true;
  renderMessage("user", message);
  const typing = showTyping();
  const priorHistory = history.slice(-12);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: priorHistory }),
    });
    const data = await response.json();
    typing.remove();
    if (!response.ok) {
      const detail = response.status === 503
        ? "I’m having trouble responding right now. If you asked me to change a device, please check its status before trying again."
        : typeof data.detail === "string"
          ? data.detail
          : "I couldn’t complete that request. Please try again.";
      renderMessage("assistant", detail, { error: true });
      try {
        await refreshHome();
      } catch (error) {
        console.warn("The dashboard could not refresh after the chat error.", error);
      }
      return;
    }

    renderMessage("assistant", data.reply, { actions: data.actions });
    history = [...priorHistory, { role: "user", content: message }, { role: "assistant", content: data.reply }].slice(-12);
    saveHistory();
    try {
      await refreshHome();
    } catch (error) {
      providerLabel.textContent = "Home overview unavailable";
      console.warn("The reply succeeded, but the dashboard could not refresh.", error);
    }
  } catch (error) {
    typing.remove();
    console.warn("The chat request could not reach the home service.", error);
    renderMessage(
      "assistant",
      "I can’t reach the home service right now. Please try again in a little while. If you asked me to change a device, check its status first.",
      { error: true },
    );
  } finally {
    busy = false;
    sendButton.disabled = false;
    input.focus();
  }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
}

function restoreMessages() {
  for (const item of history) renderMessage(item.role, item.content);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value);
});

input.addEventListener("input", resizeInput);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.prompt || ""));
});

document.querySelector("#clear-chat").addEventListener("click", () => {
  if (busy) return;
  history = [];
  sessionStorage.removeItem(historyKey);
  messageList.replaceChildren();
  welcome.hidden = false;
  welcome.style.display = "";
  input.focus();
});

restoreMessages();
refreshHome().catch((error) => {
  providerLabel.textContent = "Home service unavailable";
  document.querySelector("#device-list").replaceChildren(
    element("div", "loading-card", error.message),
  );
});
