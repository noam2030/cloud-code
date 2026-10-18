"""Code agent: a Claude tool-use loop confined to one checked-out repository."""
import os
import subprocess
from pathlib import Path
from typing import Iterator

import anthropic

MODEL = os.environ.get("MODEL", "claude-opus-5-5")

SYSTEM = (
    "You are a coding agent working inside a git repository checked out at {root}. "
    "Use the bash tool to explore the code and run commands, and the text editor tool "
    "to read and modify files. Every path must be inside {root}. "
    "Do not run git commit or git push; the user pushes separately. "
    "When you finish a task, briefly summarize what you changed."
)

TOOLS = [
    {"type": "bash_20250124", "name": "bash"},
    {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
]


class Agent:
    def __init__(self, root: Path):
        self.root = root.resolve()
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

    def _run_tool(self, name: str, inp: dict) -> str:
        if name == "bash":
            return self._bash(inp)
        if name == "str_replace_based_edit_tool":
            return self._editor(inp)
        raise ValueError(f"unknown tool: {name}")

    # ---- loop --------------------------------------------------------------

    def run(self, user_message: str) -> Iterator[dict]:
        """Run one user turn. Yields events: text, tool, tool_result, error."""
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
                detail = response.stop_details.explanation if response.stop_details else ""
                yield {"type": "error", "text": f"The model declined this request. {detail}".strip()}
                return
            if response.stop_reason == "max_tokens":
                yield {"type": "error", "text": "Output was cut off at max_tokens."}
                return
            if response.stop_reason != "tool_use":
                return

            results = []
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
            self.messages.append({"role": "user", "content": results})
