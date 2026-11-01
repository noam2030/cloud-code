import base64, os, shutil, subprocess, uuid
from pathlib import Path

ROOT = Path("/tmp/ws")


def _auth() -> list[str]:
    # Token passed per-command so it never lands in .git/config (readable by agents).
    b64 = base64.b64encode(f"x-access-token:{os.environ['GITHUB_TOKEN']}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {b64}"]


def git(sid: str, *args: str, auth: bool = False) -> str:
    cmd = ["git", *(_auth() if auth else []), *args]
    r = subprocess.run(cmd, cwd=path(sid), capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def path(sid: str) -> Path:
    return ROOT / sid


def clone(repo: str, branch: str | None = None) -> str:
    sid = uuid.uuid4().hex[:8]
    ROOT.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    args = ["git", *_auth(), "clone", *(["--branch", branch] if branch else []), url, str(path(sid))]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    git(sid, "config", "user.name", "cloud-code-agent")
    git(sid, "config", "user.email", "agent@cloud-code.local")
    return sid


def files(sid: str) -> list[str]:
    return git(sid, "ls-files", "-co", "--exclude-standard").splitlines()


def diff(sid: str) -> str:
    git(sid, "add", "-A")  # include new files
    return git(sid, "diff", "--cached")


def push(sid: str, message: str, branch: str = "main") -> str:
    git(sid, "add", "-A")
    if git(sid, "status", "--porcelain"):
        git(sid, "commit", "-m", message)
    git(sid, "push", "origin", f"HEAD:{branch}", auth=True)
    return branch


def reset(sid: str) -> None:
    """Delete everything except .github/ (the persistent deploy workflow) and push to main."""
    git(sid, "fetch", "origin", auth=True)
    git(sid, "reset", "--hard", "origin/main")
    for p in path(sid).iterdir():
        if p.name not in (".git", ".github"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    git(sid, "add", "-A")
    if git(sid, "status", "--porcelain"):
        git(sid, "commit", "-m", "Reset workspace")
        git(sid, "push", "origin", "HEAD:main", auth=True)


def safe_path(sid: str, rel: str) -> Path:
    base = path(sid).resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base) or ".git" in p.relative_to(base).parts:
        raise ValueError(f"path not allowed: {rel}")
    return p
