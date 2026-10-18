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

function append(text: string, cls = ""): HTMLSpanElement {
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = text;
  log.appendChild(span);
  log.scrollTop = log.scrollHeight;
  return span;
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function loadRepos() {
  const list: { full_name: string }[] = await (await fetch("/api/repos")).json();
  for (const r of list) repos.add(new Option(r.full_name));
  const { repo } = await (await fetch("/api/status")).json();
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

$("create").onclick = async () => {
  const name = $<HTMLInputElement>("newname").value.trim();
  if (!name) return;
  current.textContent = "Creating...";
  log.textContent = "";
  try {
    const { repo } = await post<{ repo: string }>("/api/create", { name });
    repos.add(new Option(repo), 0);
    repos.value = repo;
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
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
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
