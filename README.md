# Code Agent

Minimal web app: pick one of your GitHub repositories, let a Claude-powered agent work on the code, then push when you ask.

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key |
| `GITHUB_TOKEN` | GitHub personal access token with `repo` scope (list, clone, push) |
| `MODEL` | optional, default `claude-opus-5-5` |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | optional, commit author |

Put the values in `.env` at the project root (it is git-ignored and never copied into the Docker image):

```
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
```

## Run locally

```bash
cd frontend && npm install && npm run build && cd ..
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/uvicorn backend.main:app --port 8000
```

Open http://localhost:8000.

## Deploy to Google Cloud Run

```bash
gcloud run deploy code-agent --source . --region us-central1 \
  --allow-unauthenticated --timeout 3600 --memory 2Gi --min-instances 1 \
  --set-env-vars "$(grep -v '^#' .env | grep = | paste -sd, -)"
```

The `--set-env-vars` line reads the values from `.env` and passes them to the service.

Notes: state (the cloned repo and the conversation) lives in the container's memory and disk. `--min-instances 1` keeps one instance warm so it persists while idle; a restarted instance starts empty.
