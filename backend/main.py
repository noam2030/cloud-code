import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from .agent import Agent  # noqa: E402

app = FastAPI()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
WORKSPACE = Path(os.environ.get("WORKSPACE", "/tmp/workspace"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

state: dict = {"repo": None, "agent": None}

MAX_EVENTS = 20_000
HEARTBEAT_SECONDS = 15


class Transcript:
    """Server-side record of the conversation so any browser can catch up and follow along.

    Every event gets an increasing sequence number. The list is cleared when a repository is
    opened. One turn (a chat message or a publish) runs at a time, in a background thread,
    independent of any HTTP request.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.seq = 0
        self.busy = False
        self.cond = threading.Condition()

    def append(self, event: dict) -> None:
        with self.cond:
            self.seq += 1
            self.events.append({"seq": self.seq, **event})
            del self.events[:-MAX_EVENTS]
            self.cond.notify_all()

    def reset(self, repo: str | None) -> None:
        with self.cond:
            self.events.clear()
            self.busy = False
        self.append({"type": "reset", "repo": repo})

    def snapshot(self) -> list[dict]:
        """All events, with consecutive text chunks merged to keep the payload small."""
        with self.cond:
            merged: list[dict] = []
            for event in self.events:
                if event["type"] == "text" and merged and merged[-1]["type"] == "text":
                    merged[-1] = {**merged[-1], "seq": event["seq"], "text": merged[-1]["text"] + event["text"]}
                else:
                    merged.append(event)
            return merged

    def follow(self, since: int) -> Iterator[dict]:
        """Yield events after `since` as they arrive, with a heartbeat while idle."""
        last = since
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.seq > last, timeout=HEARTBEAT_SECONDS)
                fresh = [e for e in self.events if e["seq"] > last]
            if not fresh:
                yield {"type": "ping"}
                continue
            for event in fresh:
                last = event["seq"]
                yield event

    def start_turn(self) -> bool:
        with self.cond:
            if self.busy:
                return False
            self.busy = True
            return True

    def run_turn(self, events: Callable[[], Iterator[dict]]) -> None:
        """Run a turn in the background, recording its events and closing it with 'done'."""
        def target() -> None:
            try:
                for event in events():
                    self.append(event)
            except Exception as exc:
                self.append({"type": "error", "text": str(exc)})
            finally:
                with self.cond:
                    self.busy = False
                self.append({"type": "done"})

        threading.Thread(target=target, daemon=True).start()


transcript = Transcript()


def require_idle() -> None:
    if transcript.busy:
        raise HTTPException(409, "the agent is busy; wait for the current turn to finish")


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(500, (result.stdout + result.stderr).strip())
    return (result.stdout + result.stderr).strip()


def current_agent() -> Agent:
    if state["agent"] is None:
        raise HTTPException(400, "select a repository first")
    return state["agent"]


class SelectBody(BaseModel):
    full_name: str


class CreateBody(BaseModel):
    name: str


class ChatBody(BaseModel):
    message: str


class PushBody(BaseModel):
    message: str = "Update from code agent"


def github(path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise HTTPException(exc.code, exc.read().decode())


def checkout(full_name: str) -> dict:
    dest = WORKSPACE / full_name.replace("/", "__")
    shutil.rmtree(dest, ignore_errors=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{full_name}.git"
    git("clone", url, str(dest), cwd=WORKSPACE)
    git("config", "user.name", os.environ.get("GIT_USER_NAME", "Code Agent"), cwd=dest)
    git("config", "user.email", os.environ.get("GIT_USER_EMAIL", "code-agent@users.noreply.github.com"), cwd=dest)
    state["repo"] = full_name
    state["agent"] = Agent(dest, full_name)
    transcript.reset(full_name)
    return {"repo": full_name}


@app.get("/api/repos")
def list_repos():
    repos = github("/user/repos?per_page=100&sort=updated")
    return [{"full_name": r["full_name"], "default_branch": r["default_branch"]} for r in repos]


@app.get("/api/status")
def status():
    return {"repo": state["repo"], "busy": transcript.busy}


@app.get("/api/transcript")
def get_transcript():
    """Current repository, whether a turn is running, and the whole conversation so far."""
    return {"repo": state["repo"], "busy": transcript.busy, "events": transcript.snapshot()}


@app.get("/api/events")
def events(since: int = 0):
    """Replay events after `since`, then keep streaming new ones (NDJSON, with pings)."""
    def stream():
        for event in transcript.follow(since):
            yield json.dumps(event) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/select")
def select_repo(body: SelectBody):
    require_idle()
    return checkout(body.full_name)


@app.post("/api/create")
def create_repo(body: CreateBody):
    """Create a new public GitHub repository with an initial commit and start working on it."""
    require_idle()
    repo = github("/user/repos", {"name": body.name, "auto_init": True, "private": False})
    return checkout(repo["full_name"])


@app.post("/api/chat", status_code=202)
def chat(body: ChatBody):
    """Start an agent turn. Its events arrive through /api/events."""
    agent = current_agent()
    if not transcript.start_turn():
        raise HTTPException(409, "the agent is busy; wait for the current turn to finish")
    transcript.append({"type": "user", "text": body.message})
    transcript.run_turn(lambda: agent.run(body.message))
    return {"ok": True}


@app.post("/api/clear")
def clear_chat():
    """Forget the conversation on the server (and the agent's memory of it). The code is untouched."""
    agent = current_agent()
    require_idle()
    agent.messages = []
    transcript.reset(state["repo"])
    return {"ok": True}


def remove_code(root: Path) -> int:
    """Delete every file in the checkout except .git and .github/workflows. Returns the file count."""
    removed = 0
    for entry in root.iterdir():
        if entry.name == ".git":
            continue
        if entry.name == ".github":
            for sub in entry.iterdir():
                if sub.name == "workflows":
                    continue
                removed += sum(1 for p in sub.rglob("*") if p.is_file()) if sub.is_dir() else 1
                shutil.rmtree(sub) if sub.is_dir() else sub.unlink()
            continue
        if entry.is_dir() and not entry.is_symlink():
            removed += sum(1 for p in entry.rglob("*") if p.is_file())
            shutil.rmtree(entry)
        else:
            removed += 1
            entry.unlink()
    return removed


@app.post("/api/reset")
def reset_repo():
    """Remove all code from the checkout, keeping the GitHub workflow, and clear the conversation.
    Nothing is pushed until the user pushes."""
    agent = current_agent()
    require_idle()
    removed = remove_code(agent.root)
    agent.messages = []
    transcript.reset(state["repo"])
    transcript.append({"type": "notice", "kind": "ok", "text": (
        f"Repository reset: removed {removed} files, kept .github/workflows. "
        "Build the new app, then push to replace the old one on GitHub."
    )})
    return {"ok": True, "removed": removed}


@app.post("/api/push", status_code=202)
def push(body: PushBody):
    """Commit and push. If the repository deploys through GitHub Actions, the deploy is followed
    and its public URL arrives through /api/events."""
    agent = current_agent()
    if not transcript.start_turn():
        raise HTTPException(409, "the agent is busy; wait for the current turn to finish")
    transcript.run_turn(lambda: agent.push_events(body.message))
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
