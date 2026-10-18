import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

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


class ChatBody(BaseModel):
    message: str


class PushBody(BaseModel):
    message: str = "Update from code agent"


@app.get("/api/repos")
def list_repos():
    req = urllib.request.Request(
        "https://api.github.com/user/repos?per_page=100&sort=updated",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as resp:
        repos = json.load(resp)
    return [{"full_name": r["full_name"], "default_branch": r["default_branch"]} for r in repos]


@app.get("/api/status")
def status():
    return {"repo": state["repo"]}


@app.post("/api/select")
def select_repo(body: SelectBody):
    dest = WORKSPACE / body.full_name.replace("/", "__")
    shutil.rmtree(dest, ignore_errors=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{body.full_name}.git"
    git("clone", url, str(dest), cwd=WORKSPACE)
    git("config", "user.name", os.environ.get("GIT_USER_NAME", "Code Agent"), cwd=dest)
    git("config", "user.email", os.environ.get("GIT_USER_EMAIL", "code-agent@users.noreply.github.com"), cwd=dest)
    state["repo"] = body.full_name
    state["agent"] = Agent(dest)
    return {"repo": body.full_name}


@app.post("/api/chat")
def chat(body: ChatBody):
    agent = current_agent()

    def events():
        try:
            for event in agent.run(body.message):
                yield json.dumps(event) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "text": str(exc)}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/push")
def push(body: PushBody):
    root = current_agent().root
    git("add", "-A", cwd=root)
    if not git("status", "--porcelain", cwd=root):
        return {"output": "nothing to commit"}
    git("commit", "-m", body.message, cwd=root)
    return {"output": git("push", cwd=root)}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
