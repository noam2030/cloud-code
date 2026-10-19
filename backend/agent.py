"""Code agent: a Claude tool-use loop confined to one checked-out repository."""
import base64
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import anthropic

MODEL = os.environ.get("MODEL", "claude-opus-5-5")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
GCP_SA_KEY_B64 = os.environ.get("GCP_SA_KEY_B64", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"
DEPLOY_TIMEOUT = 15 * 60  # seconds to wait for GitHub Actions to finish a deploy
BASH_TIMEOUT = int(os.environ.get("BASH_TIMEOUT", "300"))

SYSTEM = (
    "You are a coding agent working inside a git repository checked out at {root}. "
    "Use the bash tool to explore the code and run commands, and the text editor tool "
    "to read and modify files. Every path must be inside {root}. "
    "Do not run git commit or git push yourself; the user pushes separately. "
    "The bash tool runs each command in a fresh non-interactive /bin/sh with no job control, "
    "so job specs like %1 do not work; use $! to reference a background process. A command "
    "ends when its output closes, so to keep a server running across commands start it with "
    "nohup and redirect its output to a file (nohup python app.py > server.log 2>&1 &); "
    "background processes still attached to the output are killed when the command returns. "
    "When the user asks to publish or deploy the app, make sure the repository has a "
    "Dockerfile at its root that serves HTTP on the PORT environment variable, then call "
    "the publish tool, which pushes the branch, deploys through GitHub Actions, waits for "
    "the deploy to finish and returns the public URL. Always tell the user that URL. "
    "When you finish a task, briefly summarize what you changed."
)

PUBLISH_TOOL = {
    "name": "publish",
    "description": (
        "Publish this repository as a public web app on Google Cloud Run through a GitHub "
        "Actions workflow. Writes .github/workflows/deploy.yml, stores the required GitHub "
        "secrets on the repository, commits and pushes the current branch. The repository "
        "must contain a Dockerfile at its root that serves HTTP on $PORT. Waits for the "
        "deploy to finish (a few minutes) and returns the public URL of the app."
    ),
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "service_name": {
                "type": "string",
                "description": "Cloud Run service name: lowercase letters, digits and dashes.",
            },
            "env": {
                "type": "object",
                "description": "Environment variables for the deployed app. Values are stored "
                               "as GitHub secrets and never committed.",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["service_name"],
    },
}

TOOLS = [
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
    PUBLISH_TOOL,
]

WORKFLOW = """name: Deploy to Cloud Run

on:
  push:
    branches: [{branch}]
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{{{ secrets.GCP_SA_KEY }}}}

      - uses: google-github-actions/setup-gcloud@v2

      - name: Deploy
        run: |
          gcloud run deploy {service} --source . --region {region} --project {project} \\
            --allow-unauthenticated --quiet{env_flag}
"""


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip())
    return (result.stdout + result.stderr).strip()


def commit_and_push(root: Path, message: str) -> str:
    run_git(root, "add", "-A")
    if not run_git(root, "status", "--porcelain"):
        return "nothing to commit"
    run_git(root, "commit", "-m", message)
    return run_git(root, "push")


def github_api(path: str, body: dict | None = None) -> dict | list | None:
    req = urllib.request.Request(
        GITHUB_API + path,
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
            return json.load(resp) if resp.status != 204 else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API {path}: {exc.code} {exc.read().decode()[:300]}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_job_log(repo: str, job_id: int) -> str:
    """Fetch a job's log. GitHub answers with a redirect to a signed URL that must be
    fetched without the Authorization header."""
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(req) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location")
        if exc.code in (301, 302, 307, 308) and location:
            with urllib.request.urlopen(location) as resp:
                return resp.read().decode(errors="replace")
        raise RuntimeError(f"GitHub job log: {exc.code}")


class Agent:
    def __init__(self, root: Path, repo: str):
        self.root = root.resolve()
        self.repo = repo  # "owner/name"
        self.messages: list[dict] = []
        self.client = anthropic.Anthropic()

    # ---- tools -------------------------------------------------------------

    def _safe_path(self, raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"path is outside the repository: {raw}")
        return path

    def _bash(self, inp: dict) -> str:
        if inp.get("restart"):
            return "shell restarted"
        proc = subprocess.Popen(
            inp["command"], shell=True, cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group so stray children can be killed together
        )
        chunks: list[bytes] = []

        def pump() -> None:
            while data := proc.stdout.read1(65536):
                chunks.append(data)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        note = ""
        try:
            proc.wait(timeout=BASH_TIMEOUT)
        except subprocess.TimeoutExpired:
            note = f"[timed out after {BASH_TIMEOUT} seconds; the command and its background processes were killed]"
        # Output closes only when every process holding it has exited. Give background
        # processes a moment, then kill the whole group so the tool never hangs on them.
        reader.join(timeout=0 if note else 2)
        if reader.is_alive():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            reader.join(timeout=5)
            if not note:
                note = ("[background processes still attached to the output were killed; to keep a "
                        "server running across commands start it with nohup and redirect its output "
                        "to a file, e.g. nohup python app.py > server.log 2>&1 &]")
        returncode = proc.wait()
        out = b"".join(chunks).decode(errors="replace").rstrip("\n")
        if returncode != 0:
            out += f"\n[exit code {returncode}]"
        if note:
            out += f"\n{note}"
        return out[-50_000:].strip("\n") or "(no output)"

    def _editor(self, inp: dict) -> str:
        cmd = inp["command"]
        path = self._safe_path(inp["path"])
        if cmd == "view":
            if path.is_dir():
                return "\n".join(sorted(str(p.relative_to(self.root)) for p in path.iterdir()))
            lines = path.read_text().splitlines()
            start, end = inp.get("view_range") or (1, len(lines))
            if end == -1:
                end = len(lines)
            return "\n".join(f"{i}\t{line}" for i, line in enumerate(lines[start - 1:end], start))
        if cmd == "create":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inp["file_text"])
            return f"created {path.relative_to(self.root)}"
        if cmd == "str_replace":
            text = path.read_text()
            count = text.count(inp["old_str"])
            if count != 1:
                raise ValueError(f"old_str found {count} times, expected exactly 1")
            path.write_text(text.replace(inp["old_str"], inp["new_str"]))
            return "replaced"
        if cmd == "insert":
            lines = path.read_text().splitlines(keepends=True)
            text = inp["insert_text"]
            if not text.endswith("\n"):
                text += "\n"
            lines.insert(inp["insert_line"], text)
            path.write_text("".join(lines))
            return "inserted"
        raise ValueError(f"unknown editor command: {cmd}")

    def _set_secret(self, name: str, value: str) -> None:
        result = subprocess.run(
            ["gh", "secret", "set", name, "--repo", self.repo],
            input=value, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gh secret set {name}: {result.stderr.strip()}")

    def _push_deploy_workflow(self, service: str, env: dict) -> tuple[str, str]:
        """Write the deploy workflow, store secrets, commit and push. Returns (branch, push output)."""
        if not GCP_SA_KEY_B64 or not GCP_PROJECT:
            raise RuntimeError("GCP_SA_KEY_B64 and GCP_PROJECT must be set on this service")
        if not (self.root / "Dockerfile").exists():
            raise FileNotFoundError("no Dockerfile at the repository root; create one first")

        branch = run_git(self.root, "branch", "--show-current")
        env_flag = ""
        if env:
            pairs = ",".join(f"{k}=${{{{ secrets.{k} }}}}" for k in env)
            env_flag = f' \\\n            --set-env-vars "{pairs}"'
        workflow = self.root / ".github" / "workflows" / "deploy.yml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(WORKFLOW.format(
            branch=branch, service=service, region=GCP_REGION, project=GCP_PROJECT, env_flag=env_flag,
        ))

        self._set_secret("GCP_SA_KEY", base64.b64decode(GCP_SA_KEY_B64).decode())
        for key, value in env.items():
            self._set_secret(key, value)

        pushed = commit_and_push(self.root, f"Deploy {service} to Cloud Run via GitHub Actions")
        return branch, pushed

    def deploy_events(self, service: str, env: dict | None = None) -> Iterator[dict]:
        """Publish (write workflow, push) and follow the GitHub Actions deploy until it finishes.

        Yields {"type": "status", "text"} events, then {"type": "url", "url"} on success.
        Raises RuntimeError when the deploy fails or times out.
        """
        started = datetime.now(timezone.utc)
        branch, pushed = self._push_deploy_workflow(service, env or {})
        sha = run_git(self.root, "rev-parse", "HEAD")
        if pushed == "nothing to commit":
            github_api(f"/repos/{self.repo}/actions/workflows/deploy.yml/dispatches", {"ref": branch})
            yield {"type": "status", "text": f"No new changes on {branch}; triggered the deploy workflow."}
        else:
            yield {"type": "status", "text": f"Pushed {sha[:7]} to {branch}. Waiting for GitHub Actions to deploy '{service}'…"}
        yield from self.follow_deploy(sha, started)

    def push_events(self, message: str) -> Iterator[dict]:
        """Commit and push. If the repository deploys through GitHub Actions, follow that deploy."""
        started = datetime.now(timezone.utc)
        output = commit_and_push(self.root, message)
        yield {"type": "notice", "kind": "ok", "text": output or "Pushed to GitHub."}
        if output == "nothing to commit" or not (self.root / ".github" / "workflows" / "deploy.yml").exists():
            return
        sha = run_git(self.root, "rev-parse", "HEAD")
        yield {"type": "progress", "text": f"GitHub Actions is deploying {sha[:7]}…"}
        yield from self.follow_deploy(sha, started)

    def follow_deploy(self, sha: str, started: datetime) -> Iterator[dict]:
        """Wait for the deploy workflow run of `sha` (created after `started`) and report its URL."""
        deadline = time.monotonic() + DEPLOY_TIMEOUT
        run = None
        while run is None:
            if time.monotonic() > deadline:
                raise RuntimeError(f"No deploy workflow run appeared for {sha[:7]}. See https://github.com/{self.repo}/actions")
            runs = github_api(f"/repos/{self.repo}/actions/runs?head_sha={sha}&per_page=10")["workflow_runs"]
            for candidate in runs:
                created = datetime.fromisoformat(candidate["created_at"].replace("Z", "+00:00"))
                if candidate["path"].endswith("deploy.yml") and created >= started.replace(microsecond=0):
                    run = candidate
                    break
            else:
                time.sleep(5)
        yield {"type": "status", "text": f"Deploy started: {run['html_url']}"}

        last_status = None
        while run["status"] != "completed":
            if time.monotonic() > deadline:
                raise RuntimeError(f"Deploy is still running after {DEPLOY_TIMEOUT // 60} minutes: {run['html_url']}")
            if run["status"] != last_status:
                last_status = run["status"]
                yield {"type": "status", "text": f"Deploy {run['status'].replace('_', ' ')}…"}
            time.sleep(10)
            run = github_api(f"/repos/{self.repo}/actions/runs/{run['id']}")
        if run["conclusion"] != "success":
            raise RuntimeError(f"Deploy {run['conclusion']}: {run['html_url']}")

        jobs = github_api(f"/repos/{self.repo}/actions/runs/{run['id']}/jobs")["jobs"]
        url = None
        for job in jobs:
            match = re.search(r"Service URL: (https://\S+)", github_job_log(self.repo, job["id"]))
            if match:
                url = match.group(1)
                break
        if not url:
            raise RuntimeError(f"Deploy succeeded but no service URL was found in the log: {run['html_url']}")
        yield {"type": "status", "text": "Deployed to Cloud Run."}
        yield {"type": "url", "url": url}

    def _publish(self, inp: dict) -> str:
        service = inp.get("service_name")
        env = inp.get("env") or {}
        if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", service):
            raise ValueError("service_name must be lowercase letters, digits and dashes")
        if not isinstance(env, dict) or not all(
            re.fullmatch(r"[A-Z_][A-Z0-9_]*", k) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("env must map UPPER_CASE names to string values")
        lines = []
        for event in self.deploy_events(service, env):
            if event["type"] == "url":
                lines.append(f"Public URL: {event['url']}")
            else:
                lines.append(event["text"])
        return "\n".join(lines)

    def _run_tool(self, name: str, inp: dict) -> str:
        if name == "bash":
            return self._bash(inp)
        if name == "str_replace_based_edit_tool":
            return self._editor(inp)
        if name == "publish":
            return self._publish(inp)
        raise ValueError(f"unknown tool: {name}")

    # ---- loop --------------------------------------------------------------

    def _close_open_tool_uses(self) -> None:
        """Ensure every tool_use in the last assistant turn has a tool_result.

        The API rejects the whole conversation otherwise. A turn can end without
        results when the client disconnects mid-turn or output hits max_tokens.
        """
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return
        ids = [b.id for b in self.messages[-1]["content"] if getattr(b, "type", None) == "tool_use"]
        if ids:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i, "is_error": True,
                 "content": "tool execution was interrupted"} for i in ids
            ]})

    def run(self, user_message: str) -> Iterator[dict]:
        """Run one user turn. Yields events: text, tool, tool_result, error."""
        self._close_open_tool_uses()
        self.messages.append({"role": "user", "content": user_message})
        while True:
            with self.client.beta.messages.stream(
                model=MODEL,
                max_tokens=64_000,
                system=SYSTEM.format(root=self.root),
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": "high"},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        yield {"type": "text", "text": event.text}
                response = stream.get_final_message()

            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                self._close_open_tool_uses()
                detail = response.stop_details.explanation if response.stop_details else ""
                yield {"type": "error", "text": f"The model declined this request. {detail}".strip()}
                return
            if response.stop_reason == "max_tokens":
                self._close_open_tool_uses()
                yield {"type": "error", "text": "Output was cut off at max_tokens."}
                return
            if response.stop_reason != "tool_use":
                return

            results: list[dict] = []
            try:
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    yield {"type": "tool", "name": block.name, "input": block.input}
                    try:
                        output = self._run_tool(block.name, block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                    except Exception as exc:  # report to the model so it can recover
                        output = f"error: {exc}"
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": output, "is_error": True})
                    yield {"type": "tool_result", "text": output[:2000]}
            finally:
                # Always record a result for every tool_use, even if the client
                # disconnected mid-turn and this generator was closed.
                done = {r["tool_use_id"] for r in results}
                for block in response.content:
                    if block.type == "tool_use" and block.id not in done:
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": "tool execution was interrupted", "is_error": True})
                self.messages.append({"role": "user", "content": results})
