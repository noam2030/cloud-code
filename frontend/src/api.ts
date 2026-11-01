const URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const headers = { "Content-Type": "application/json", Authorization: `Bearer ${import.meta.env.VITE_APP_TOKEN}` };

async function call<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(URL + path, { method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
  return r.json();
}

export const newSession = () => call<{ session_id: string; repo: string }>("/sessions", {});
export const getFiles = (sid: string) => call<{ files: string[] }>(`/sessions/${sid}/files`);
export const getDiff = (sid: string) => call<{ diff: string }>(`/sessions/${sid}/diff`);
export const push = (sid: string) => call<{ branch: string; message: string }>(`/sessions/${sid}/push`, {});

export const reset = (sid: string) => call<{ ok: boolean }>(`/sessions/${sid}/reset`, {});

export type Event = { text?: string; tool?: string; error?: string };

export async function chat(session_id: string, agent: string, message: string, onEvent: (e: Event) => void) {
  const r = await fetch(URL + "/chat", { method: "POST", headers, body: JSON.stringify({ session_id, agent, message }) });
  if (!r.ok || !r.body) throw new Error(r.statusText);
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value;
    const parts = buf.split("\n\n");
    buf = parts.pop()!;
    for (const p of parts) {
      const data = p.replace(/^data: /, "");
      if (data !== "[DONE]") onEvent(JSON.parse(data));
    }
  }
}
