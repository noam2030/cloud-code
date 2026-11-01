import { useEffect, useRef, useState } from "react";
import * as api from "./api";

const DEPLOY_URL = import.meta.env.VITE_DEPLOY_URL as string | undefined;
const AGENTS = ["coder", "reviewer", "expert"] as const;
type Msg = { role: string; text: string };

export default function App() {
  const [repo, setRepo] = useState("");
  const [sid, setSid] = useState("");
  const [agent, setAgent] = useState<string>("coder");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [panel, setPanel] = useState<{ title: string; body: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const chatEnd = useRef<HTMLDivElement>(null);

  useEffect(() => { chatEnd.current?.scrollIntoView({ block: "end" }); }, [msgs]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); } catch (e) { alert((e as Error).message); } finally { setBusy(false); }
  };

  const connect = () => run(async () => {
    const s = await api.newSession();
    setSid(s.session_id);
    setRepo(s.repo);
    setMsgs([]);
  });

  useEffect(() => { connect(); }, []);

  const send = () => {
    const text = input.trim();
    if (!text || busy) return;
    run(async () => {
      setInput("");
      setMsgs((m) => [...m, { role: "you", text }, { role: agent, text: "" }]);
      const append = (s: string) =>
        setMsgs((m) => [...m.slice(0, -1), { ...m[m.length - 1], text: m[m.length - 1].text + s }]);
      await api.chat(sid, agent, text, (e) => {
        if (e.text) append(e.text);
        if (e.tool) append(`\n[${e.tool}]\n`);
        if (e.error) append(`\nError: ${e.error}`);
      });
    });
  };

  const show = (title: string, load: () => Promise<string>) => run(async () => setPanel({ title, body: await load() }));

  const doPush = () => run(async () => {
    const r = await api.push(sid);
    alert(`Pushed to ${r.branch}: "${r.message}"`);
  });

  const doReset = () => {
    if (!confirm(`Delete ALL code in ${repo} (except .github/) and push to main?`)) return;
    run(async () => {
      await api.reset(sid);
      await connect(); // fresh clone + fresh agent memory
    });
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Cloud Code</h1>
        <div className="meta">
          <span>{sid ? `${repo} · ${sid}` : "Connecting…"}</span>
          {DEPLOY_URL && <a href={DEPLOY_URL} target="_blank" rel="noreferrer">Open app ↗</a>}
        </div>
      </header>

      <nav className="tabs">
        {AGENTS.map((a) => (
          <button key={a} className={agent === a ? "active" : ""} onClick={() => setAgent(a)}>{a}</button>
        ))}
      </nav>

      <div className="toolbar">
        <button disabled={busy || !sid} onClick={() => show("Files", async () => (await api.getFiles(sid)).files.join("\n") || "no files")}>Files</button>
        <button disabled={busy || !sid} onClick={() => show("Diff", async () => (await api.getDiff(sid)).diff || "no changes")}>Diff</button>
        <button className="primary" disabled={busy || !sid} onClick={doPush}>Push</button>
        <button disabled={busy} onClick={connect}>New session</button>
        <button className="danger" disabled={busy || !sid} onClick={doReset}>Reset repo</button>
      </div>

      <main className="chat">
        {msgs.length === 0 && <p className="empty">Ask the {agent} something.</p>}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role === "you" ? "you" : ""}`}>
            {m.role !== "you" && <div className="role">{m.role}</div>}
            <pre>{m.text || "…"}</pre>
          </div>
        ))}
        <div ref={chatEnd} />
      </main>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <textarea
          rows={2} value={input} placeholder={`Message ${agent}…`} enterKeyHint="send"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && window.matchMedia("(pointer: fine)").matches && (e.preventDefault(), send())}
        />
        <button className="primary" disabled={busy || !sid || !input.trim()}>Send</button>
      </form>

      {panel && (
        <div className="sheet" onClick={() => setPanel(null)}>
          <div onClick={(e) => e.stopPropagation()}>
            <div className="bar">{panel.title}<button onClick={() => setPanel(null)}>Close</button></div>
            <pre>{panel.body}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
