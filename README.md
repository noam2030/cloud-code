# Code Agent

Minimal web app: pick one of your GitHub repositories or create a new one, let a Claude-powered agent work on the code, then push when you ask.

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key |
| `GITHUB_TOKEN` | GitHub personal access token with `repo` scope (list, clone, push) |
| `MODEL` | optional, default `claude-opus-5-5` |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | optional, commit author |
| `GCP_PROJECT`, `GCP_REGION`, `GCP_SA_KEY_B64` | for the agent's `publish` tool: project, region, and the base64 of a deployer service account JSON key |

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

Notes: state (the cloned repo and the conversation) lives in the container's memory and disk, not in the browser. The server records every event of the conversation and runs each agent turn in the background, so refreshing the page or opening it from another device shows the current state and follows the turn live. `--min-instances 1` keeps one instance warm so it persists while idle; a restarted instance starts empty.

## Publishing an app the agent built

The first time, ask the agent to publish or deploy. It makes sure the repository has a Dockerfile, then calls its `publish` tool, which writes `.github/workflows/deploy.yml` into that repository, stores `GCP_SA_KEY` (and any app environment variables) as GitHub secrets there, commits and pushes, waits for the GitHub Actions deploy and reports the public URL.

From then on, every **Push to GitHub** triggers that workflow. The app follows the run and shows the public URL in the conversation when the deploy finishes (usually a few minutes).

This needs the GitHub CLI in the container (installed by the Dockerfile) and the `GCP_*` variables above, which the agent's own deploy workflow sets.
