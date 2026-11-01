import os, subprocess
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
import workspace as ws

MAX_OUT = 20_000
SECRETS = {"GITHUB_TOKEN", "OPENAI_API_KEY", "APP_TOKEN"}

PROMPTS = {
    "coder": "You are a senior software engineer working in a git repository. Explore the code before changing it, "
    "make focused edits with write_file, and verify with run_shell (tests, linters) when possible. "
    "Summarize what you changed at the end.",
    "reviewer": "You are a strict code reviewer. Use git_diff to see pending changes and read surrounding code for context. "
    "Report bugs, security issues, and design problems, ordered by severity, with file:line references. "
    "Be concise; skip nitpicks unless asked.",
    "expert": "You are an expert on this codebase. Answer questions about its implementation and architecture by "
    "reading the actual code. Cite file paths. Do not guess; say so if something isn't in the code.",
}

_model = ChatOpenAI(model=os.environ.get("OPENAI_MODEL", "gpt-5"))
_memory = InMemorySaver()
_agents: dict[tuple[str, str], object] = {}


def _clip(s: str) -> str:
    return s if len(s) <= MAX_OUT else s[:MAX_OUT] + "\n...[truncated]"


def _tools(sid: str) -> dict:
    root = ws.path(sid)

    @tool
    def list_files(dir: str = ".") -> str:
        """List tracked and untracked files under a directory (relative to repo root)."""
        ws.safe_path(sid, dir)
        return _clip(ws.git(sid, "ls-files", "-co", "--exclude-standard", dir))

    @tool
    def read_file(path: str) -> str:
        """Read a file (path relative to repo root)."""
        return _clip(ws.safe_path(sid, path).read_text(errors="replace"))

    @tool
    def search(pattern: str) -> str:
        """Search the repo for a regex pattern (git grep). Returns file:line:match."""
        try:
            return _clip(ws.git(sid, "grep", "-nIE", "--untracked", pattern)) or "no matches"
        except RuntimeError:
            return "no matches"

    @tool
    def git_diff() -> str:
        """Show all uncommitted changes in the repo."""
        return _clip(ws.diff(sid)) or "no changes"

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file with the full given content (path relative to repo root)."""
        p = ws.safe_path(sid, path)
        if p.relative_to(ws.path(sid).resolve()).parts[:1] == (".github",):
            return ".github/ is protected and cannot be modified"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {path}"

    @tool
    def run_shell(command: str) -> str:
        """Run a shell command in the repo root (e.g. tests, build). 120s timeout."""
        env = {k: v for k, v in os.environ.items() if k not in SECRETS}
        try:
            r = subprocess.run(command, shell=True, cwd=root, env=env, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return "timed out"
        return _clip(f"exit {r.returncode}\n{r.stdout}{r.stderr}")

    return {t.name: t for t in [list_files, read_file, search, git_diff, write_file, run_shell]}


TOOLSETS = {
    "coder": ["list_files", "read_file", "search", "git_diff", "write_file", "run_shell"],
    "reviewer": ["list_files", "read_file", "search", "git_diff"],
    "expert": ["list_files", "read_file", "search"],
}


def get_agent(sid: str, name: str):
    if (sid, name) not in _agents:
        tools = _tools(sid)
        _agents[sid, name] = create_agent(
            _model, [tools[t] for t in TOOLSETS[name]], system_prompt=PROMPTS[name], checkpointer=_memory
        )
    return _agents[sid, name]


def commit_message(diff: str) -> str:
    prompt = "Write a one-line git commit message (max 72 chars, imperative mood) for this diff. Reply with the message only.\n\n"
    try:
        return _model.invoke(prompt + _clip(diff)).text.strip().strip('"')[:72] or "Update code"
    except Exception:
        return "Update code"
