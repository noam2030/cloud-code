const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const repos = $<HTMLSelectElement>("repos");
const statusEl = $<HTMLDivElement>("status");
const statusText = $<HTMLSpanElement>("status-text");
const log = $<HTMLDivElement>("log");
const empty = $<HTMLDivElement>("empty");
const input = $<HTMLTextAreaElement>("input");
const composer = $<HTMLFormElement>("composer");
const sendBtn = $<HTMLButtonElement>("send");
const pushBtn = $<HTMLButtonElement>("push");
const selectBtn = $<HTMLButtonElement>("select");
const createBtn = $<HTMLButtonElement>("create");
const newName = $<HTMLInputElement>("newname");
const pushDialog = $<HTMLDialogElement>("push-dialog");
const commitMessage = $<HTMLInputElement>("commit-message");
const publishBtn = $<HTMLButtonElement>("publish");
const publishDialog = $<HTMLDialogElement>("publish-dialog");
const serviceName = $<HTMLInputElement>("service-name");

type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool"; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; text: string }
  | { type: "error"; text: string }
  | { type: "done" };

type PublishEvent =
  | { type: "status"; text: string }
  | { type: "url"; url: string }
  | { type: "error"; text: string }
  | { type: "done" };

let repo: string | null = null;
let busy = false;

// ---------- Status ----------

function setStatus(kind: "idle" | "ready" | "busy" | "error", html: string) {
  statusEl.className = `status status-${kind}`;
  statusText.innerHTML = html;
}

function showRepo(name: string | null) {
  repo = name;
  if (name) setStatus("ready", `Working on <strong>${escapeHtml(name)}</strong>`);
  else setStatus("idle", "No repository selected");
  updateControls();
}

function setBusy(value: boolean) {
  busy = value;
  updateControls();
}

function updateControls() {
  sendBtn.disabled = busy || !repo;
  pushBtn.disabled = busy || !repo;
  publishBtn.disabled = busy || !repo;
  selectBtn.disabled = busy;
  createBtn.disabled = busy;
  input.disabled = busy || !repo;
  input.placeholder = repo ? "Describe what you want the agent to do…" : "Open or create a repository to get started";
}

// ---------- Rendering ----------

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function inline(s: string): string {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[^"'>=])(https?:\/\/[^\s<"]+?)([.,;:!?)\]]*)(?=\s|$|<)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>$3');
}

/** Small markdown renderer: fenced code, headings, lists, paragraphs. Everything is escaped first. */
function renderMarkdown(src: string): string {
  const out: string[] = [];
  const lines = src.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      const cls = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : "";
      out.push(`<pre><code${cls}>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)/);
    if (heading) {
      out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`);
      i++;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, ""));
      out.push(`<ul>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+\.\s+/, ""));
      out.push(`<ol>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</ol>`);
      continue;
    }
    if (line.trim() === "") {
      i++;
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && !/^(```|#{1,3}\s|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i])) para.push(lines[i++]);
    out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("");
}

function scrollToBottom() {
  log.scrollTop = log.scrollHeight;
}

function add<T extends HTMLElement>(el: T): T {
  empty.hidden = true;
  log.appendChild(el);
  scrollToBottom();
  return el;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function addUserMessage(text: string) {
  const msg = el("div", "msg msg-user");
  msg.append(el("div", "msg-label", "You"), el("div", "bubble", text));
  add(msg);
}

function addAssistantMessage() {
  const msg = el("div", "msg msg-assistant");
  const bubble = el("div", "bubble cursor");
  msg.append(el("div", "msg-label", "Agent"), bubble);
  add(msg);
  let raw = "";
  return {
    append(text: string) {
      raw += text;
      bubble.innerHTML = renderMarkdown(raw);
      scrollToBottom();
    },
    finish() {
      bubble.classList.remove("cursor");
      if (!raw.trim()) msg.remove();
    },
  };
}

function toolPreview(name: string, args: Record<string, unknown>): string {
  const first = (keys: string[]) => keys.map((k) => args[k]).find((v) => typeof v === "string" && v) as string | undefined;
  if (name === "bash") return first(["command"]) ?? "";
  if (name === "str_replace_based_edit_tool") return `${args.command ?? ""} ${args.path ?? ""}`.trim();
  return first(["path", "name", "message"]) ?? JSON.stringify(args);
}

function addToolCall(name: string, args: Record<string, unknown>) {
  const details = el("details", "tool tool-running");
  const summary = el("summary");
  summary.append(el("span", "tool-name", name), el("span", "tool-preview", toolPreview(name, args).split("\n")[0]));
  const body = el("div", "tool-body");
  body.append(el("div", "tool-section", "Input"), el("pre", "", JSON.stringify(args, null, 2)));
  details.append(summary, body);
  add(details);
  return {
    result(text: string) {
      details.classList.remove("tool-running");
      body.append(el("div", "tool-section", "Output"), el("pre", "", text.trim() || "(no output)"));
      scrollToBottom();
    },
    done() {
      details.classList.remove("tool-running");
    },
  };
}

function addNotice(text: string, kind: "error" | "ok" | "" = "") {
  add(el("div", `notice${kind ? ` notice-${kind}` : ""}`, text));
}

function addProgress(text: string) {
  const box = add(el("div", "notice notice-progress"));
  const line = (text: string) => {
    const node = el("div", "notice-line");
    node.innerHTML = inline(text);
    return node;
  };
  box.append(line(text));
  return {
    step(text: string) {
      box.append(line(text));
      scrollToBottom();
    },
    done() {
      box.classList.add("notice-done");
    },
  };
}

function addUrlNotice(label: string, url: string) {
  const box = el("div", "notice notice-ok notice-url");
  const link = el("a", "", url) as HTMLAnchorElement;
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  box.append(`${label} `, link);
  add(box);
}

/** Read a newline-delimited JSON stream, calling onEvent for each object. */
async function readNdjson<T>(body: ReadableStream<Uint8Array>, onEvent: (ev: T) => void) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) if (line) onEvent(JSON.parse(line));
  }
}

// ---------- Repositories ----------

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function clearConversation() {
  for (const child of Array.from(log.children)) if (child !== empty) child.remove();
  empty.hidden = false;
}

async function loadRepos() {
  try {
    const list: { full_name: string }[] = await (await fetch("/api/repos")).json();
    for (const r of list) repos.add(new Option(r.full_name));
    const status = await (await fetch("/api/status")).json();
    if (status.repo) {
      repos.value = status.repo;
      showRepo(status.repo);
    } else showRepo(null);
  } catch (e) {
    setStatus("error", `Could not load repositories: ${escapeHtml(String(e))}`);
  }
}

selectBtn.onclick = async () => {
  if (!repos.value) return;
  setBusy(true);
  setStatus("busy", `Cloning <strong>${escapeHtml(repos.value)}</strong>…`);
  clearConversation();
  try {
    const { repo } = await post<{ repo: string }>("/api/select", { full_name: repos.value });
    showRepo(repo);
    input.focus();
  } catch (e) {
    setStatus("error", escapeHtml(String(e)));
  } finally {
    setBusy(false);
  }
};

createBtn.onclick = async () => {
  const name = newName.value.trim();
  if (!name) return void newName.focus();
  setBusy(true);
  setStatus("busy", `Creating <strong>${escapeHtml(name)}</strong>…`);
  clearConversation();
  try {
    const { repo } = await post<{ repo: string }>("/api/create", { name });
    repos.add(new Option(repo), 0);
    repos.value = repo;
    newName.value = "";
    showRepo(repo);
    input.focus();
  } catch (e) {
    setStatus("error", escapeHtml(String(e)));
  } finally {
    setBusy(false);
  }
};

// ---------- Chat ----------

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 240)}px`;
}
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

composer.onsubmit = (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message || busy || !repo) return;
  input.value = "";
  autosize();
  runChat(message);
};

async function runChat(message: string) {
  if (!repo) return;
  addUserMessage(message);
  setBusy(true);
  setStatus("busy", `Working on <strong>${escapeHtml(repo)}</strong>…`);

  let assistant = null as ReturnType<typeof addAssistantMessage> | null;
  let tool = null as ReturnType<typeof addToolCall> | null;
  let toolName = "";
  const closeAssistant = () => {
    assistant?.finish();
    assistant = null;
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!res.ok || !res.body) {
      addNotice(await res.text(), "error");
      return;
    }
    await readNdjson<AgentEvent>(res.body, (ev) => {
      if (ev.type === "text") {
        if (!assistant) assistant = addAssistantMessage();
        assistant.append(ev.text);
      } else if (ev.type === "tool") {
        closeAssistant();
        tool?.done();
        tool = addToolCall(ev.name, ev.input);
        toolName = ev.name;
      } else if (ev.type === "tool_result") {
        if (tool) tool.result(ev.text);
        else addNotice(ev.text);
        tool = null;
        const published = toolName === "publish" && ev.text.match(/Public URL: (https:\/\/\S+)/);
        if (published) addUrlNotice("Your app is live at", published[1]);
      } else if (ev.type === "error") {
        closeAssistant();
        tool?.done();
        tool = null;
        addNotice(ev.text, "error");
      }
    });
  } catch (err) {
    addNotice(`Connection lost: ${err}`, "error");
  } finally {
    closeAssistant();
    tool?.done();
    setBusy(false);
    showRepo(repo);
    input.focus();
  }
}

// ---------- Push ----------

pushBtn.onclick = () => {
  if (!repo || busy) return;
  pushDialog.showModal();
  commitMessage.select();
};

pushDialog.querySelector<HTMLButtonElement>("[data-close]")!.onclick = () => pushDialog.close("cancel");

pushDialog.querySelector("form")!.onsubmit = async (e) => {
  e.preventDefault();
  const message = commitMessage.value.trim() || "Update from code agent";
  pushDialog.close();
  setBusy(true);
  setStatus("busy", `Pushing <strong>${escapeHtml(repo!)}</strong>…`);
  try {
    const { output } = await post<{ output: string }>("/api/push", { message });
    addNotice(output.trim() || "Pushed to GitHub.", "ok");
  } catch (err) {
    addNotice(`Push failed: ${err}`, "error");
  } finally {
    setBusy(false);
    showRepo(repo);
  }
};

// ---------- Publish ----------

function defaultServiceName(fullName: string): string {
  let name = fullName.split("/").pop()!.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!/^[a-z]/.test(name)) name = `app-${name}`.replace(/-+$/, "");
  return name.slice(0, 49).replace(/-+$/, "");
}

publishBtn.onclick = () => {
  if (!repo || busy) return;
  serviceName.value = defaultServiceName(repo);
  publishDialog.showModal();
  serviceName.select();
};

publishDialog.querySelector<HTMLButtonElement>("[data-close]")!.onclick = () => publishDialog.close("cancel");

publishDialog.querySelector("form")!.onsubmit = async (e) => {
  e.preventDefault();
  const service = serviceName.value.trim();
  if (!service || !repo) return;
  publishDialog.close();
  setBusy(true);
  setStatus("busy", `Publishing <strong>${escapeHtml(service)}</strong>…`);
  const progress = addProgress(`Publishing ${service} to Cloud Run…`);
  let url = null as string | null;
  try {
    const res = await fetch("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service_name: service }),
    });
    if (res.status === 409) {
      // No Dockerfile yet: let the agent add one and publish through its tool.
      progress.step("The repository has no Dockerfile. Asking the agent to add one and publish…");
      progress.done();
      setBusy(false);
      await runChat(
        `Publish this app as the Cloud Run service "${service}": add a Dockerfile at the repository root if it is missing, then call the publish tool and tell me the public URL.`,
      );
      return;
    }
    if (!res.ok || !res.body) {
      addNotice(await res.text(), "error");
      return;
    }
    await readNdjson<PublishEvent>(res.body, (ev) => {
      if (ev.type === "status") progress.step(ev.text);
      else if (ev.type === "url") url = ev.url;
      else if (ev.type === "error") addNotice(`Publish failed: ${ev.text}`, "error");
    });
    if (url) addUrlNotice("Your app is live at", url);
  } catch (err) {
    addNotice(`Publish failed: ${err}`, "error");
  } finally {
    progress.done();
    setBusy(false);
    showRepo(repo);
  }
};

updateControls();
loadRepos();
