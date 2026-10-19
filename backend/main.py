import json
import os
import shutil
import subprocess
import urllib.error
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

from .agent import Agent, commit_and_push, default_service_name  # noqa: E402

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


class CreateBody(BaseModel):
    name: str


class ChatBody(BaseModel):
    message: str


class PushBody(BaseModel):
    message: str = "Update from code agent"


class PublishBody(BaseModel):
    service_name: str | None = None
    env: dict[str, str] = {}


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
    return {"repo": full_name}


@app.get("/api/repos")
def list_repos():
    repos = github("/user/repos?per_page=100&sort=updated")
    return [{"full_name": r["full_name"], "default_branch": r["default_branch"]} for r in repos]


@app.get("/api/status")
def status():
    return {"repo": state["repo"]}


@app.post("/api/select")
def select_repo(body: SelectBody):
    return checkout(body.full_name)


@app.post("/api/create")
def create_repo(body: CreateBody):
    """Create a new public GitHub repository with an initial commit and start working on it."""
    repo = github("/user/repos", {"name": body.name, "auto_init": True, "private": False})
    return checkout(repo["full_name"])


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
    try:
        return {"output": commit_and_push(root, body.message)}
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/publish")
def publish(body: PublishBody):
    """Deploy the repository to Cloud Run and stream progress until the public URL is known."""
    agent = current_agent()
    service = body.service_name or default_service_name(state["repo"])
    if not (agent.root / "Dockerfile").exists():
        raise HTTPException(409, "no_dockerfile")

    def events():
        try:
            for event in agent.deploy_events(service, body.env):
                yield json.dumps(event) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "text": str(exc)}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
