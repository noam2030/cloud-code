import { useEffect, useState } from "react";
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
  const [panel, setPanel] = useState("");
  const [busy, setBusy] = useState(false);

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

  const send = () => run(async () => {
    const text = input;
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

  const doPush = () => run(async () => {
    const r = await api.push(sid);
    alert(`Pushed to ${r.branch}: "${r.message}"`);
  });

  const doReset = () => {
    if (!confirm(`Delete ALL code in ${repo} (except .github/) and push to main?`)) return;
    run(async () => {
      await api.reset(sid);
      setPanel("");
      await connect(); // fresh clone + fresh agent memory
    });
  };

  return (
    <div style={{ maxWidth: 900, margin: "auto", fontFamily: "system-ui" }}>
      <h2>Cloud Code</h2>
      <div>
        {sid ? <span>{repo} · session {sid} </span> : <span>Connecting… </span>}
        <button disabled={busy} onClick={connect}>New session</button>
        {DEPLOY_URL && <a href={DEPLOY_URL} target="_blank" rel="noreferrer" style={{ marginLeft: 12 }}>Open deployed app ↗</a>}
      </div>
      {sid && (
        <>
          <div style={{ margin: "12px 0" }}>
            {AGENTS.map((a) => (
              <label key={a} style={{ marginRight: 12 }}>
                <input type="radio" checked={agent === a} onChange={() => setAgent(a)} /> {a}
              </label>
            ))}
            <button disabled={busy} onClick={() => run(async () => setPanel((await api.getFiles(sid)).files.join("\n") || "no files"))}>Files</button>
            <button disabled={busy} onClick={() => run(async () => setPanel((await api.getDiff(sid)).diff || "no changes"))}>Diff</button>
            {panel && <button onClick={() => setPanel("")}>Hide</button>}
            <button disabled={busy} onClick={doPush}>Push</button>
            <button disabled={busy} onClick={doReset} style={{ color: "crimson" }}>Reset repo</button>
          </div>
          <div style={{ border: "1px solid #ccc", padding: 8, height: 450, overflow: "auto" }}>
            {msgs.map((m, i) => (
              <pre key={i} style={{ whiteSpace: "pre-wrap" }}><b>{m.role}:</b> {m.text}</pre>
            ))}
          </div>
          <textarea
            rows={3} style={{ width: "100%" }} value={input} disabled={busy}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && input.trim() && (e.preventDefault(), send())}
          />
          {panel && <pre style={{ background: "#f4f4f4", padding: 8, overflow: "auto" }}>{panel}</pre>}
        </>
      )}
    </div>
  );
}
