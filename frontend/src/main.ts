const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const repos = $<HTMLSelectElement>("repos");
const current = $<HTMLSpanElement>("current");
const log = $<HTMLDivElement>("log");
const input = $<HTMLTextAreaElement>("input");

type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool"; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; text: string }
  | { type: "error"; text: string }
  | { type: "done" };

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  const pw = localStorage.getItem("appPassword");
  if (pw) h["X-App-Password"] = pw;
  return h;
}

async function api(url: string, init: RequestInit = {}): Promise<Response> {
  let res = await fetch(url, { ...init, headers: headers() });
  if (res.status === 401) {
    const pw = prompt("Password");
    if (pw === null) return res;
    localStorage.setItem("appPassword", pw);
    res = await fetch(url, { ...init, headers: headers() });
  }
  return res;
}

function append(text: string, cls = ""): HTMLSpanElement {
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = text;
  log.appendChild(span);
  log.scrollTop = log.scrollHeight;
  return span;
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await api(url, { method: "POST", body: JSON.stringify(body) });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function loadRepos() {
  const { repo } = await (await api("/api/status")).json();
  const list: { full_name: string }[] = await (await api("/api/repos")).json();
  for (const r of list) repos.add(new Option(r.full_name));
  if (repo) current.textContent = `Working on ${repo}`;
}

$("select").onclick = async () => {
  current.textContent = "Cloning...";
  log.textContent = "";
  try {
    const { repo } = await post<{ repo: string }>("/api/select", { full_name: repos.value });
    current.textContent = `Working on ${repo}`;
  } catch (e) {
    current.textContent = String(e);
  }
};

$("send").onclick = async () => {
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  append(`\n> ${message}\n\n`, "tool");
  const res = await api("/api/chat", { method: "POST", body: JSON.stringify({ message }) });
  if (!res.ok || !res.body) return void append(await res.text(), "error");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let textSpan: HTMLSpanElement | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line) continue;
      const ev: AgentEvent = JSON.parse(line);
      if (ev.type === "text") {
        if (!textSpan) textSpan = append("");
        textSpan.textContent += ev.text;
      } else {
        textSpan = null;
        if (ev.type === "tool") append(`\n$ ${ev.name} ${JSON.stringify(ev.input)}\n`, "tool");
        else if (ev.type === "tool_result") append(ev.text + "\n", "tool");
        else if (ev.type === "error") append(ev.text + "\n", "error");
      }
      log.scrollTop = log.scrollHeight;
    }
  }
};

$("push").onclick = async () => {
  const message = prompt("Commit message", "Update from code agent");
  if (message === null) return;
  try {
    const { output } = await post<{ output: string }>("/api/push", { message });
    append(`\n[push] ${output}\n`, "tool");
  } catch (e) {
    append(`\n[push failed] ${e}\n`, "error");
  }
};

loadRepos();
