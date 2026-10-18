"""Code agent: a Claude tool-use loop confined to one checked-out repository."""
import base64
import os
import re
import subprocess
from pathlib import Path
from typing import Iterator

import anthropic

MODEL = os.environ.get("MODEL", "claude-opus-5-5")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
GCP_SA_KEY_B64 = os.environ.get("GCP_SA_KEY_B64", "")

SYSTEM = (
    "You are a coding agent working inside a git repository checked out at {root}. "
    "Use the bash tool to explore the code and run commands, and the text editor tool "
    "to read and modify files. Every path must be inside {root}. "
    "Do not run git commit or git push yourself; the user pushes separately. "
    "When the user asks to publish or deploy the app, make sure the repository has a "
    "Dockerfile at its root that serves HTTP on the PORT environment variable, then call "
    "the publish tool, which pushes the branch and deploys through GitHub Actions. "
    "When you finish a task, briefly summarize what you changed."
)

PUBLISH_TOOL = {
    "name": "publish",
    "description": (
        "Publish this repository as a public web app on Google Cloud Run through a GitHub "
        "Actions workflow. Writes .github/workflows/deploy.yml, stores the required GitHub "
        "secrets on the repository, commits and pushes the current branch. The repository "
        "must contain a Dockerfile at its root that serves HTTP on $PORT. Returns the "
        "GitHub Actions URL where the deploy can be watched."
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
        result = subprocess.run(
            inp["command"], shell=True, cwd=self.root,
            capture_output=True, text=True, timeout=300,
        )
        out = result.stdout + result.stderr
        if result.returncode != 0:
            out += f"\n[exit code {result.returncode}]"
        return out[-50_000:] or "(no output)"

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

    def _publish(self, inp: dict) -> str:
        service = inp.get("service_name")
        env = inp.get("env") or {}
        if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", service):
            raise ValueError("service_name must be lowercase letters, digits and dashes")
        if not isinstance(env, dict) or not all(
            re.fullmatch(r"[A-Z_][A-Z0-9_]*", k) and isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("env must map UPPER_CASE names to string values")
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
        return (
            f"Pushed to {branch} ({pushed}). GitHub Actions is deploying service '{service}'. "
            f"Watch it at https://github.com/{self.repo}/actions; the Deploy step prints the public URL."
        )

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
