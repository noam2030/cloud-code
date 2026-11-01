# Cloud Code

Three LangChain + OpenAI agents work on a GitHub repo that is cloned into an ephemeral container:
- **coder**: reads, writes and runs code
- **reviewer**: reviews the pending diff
- **expert**: answers questions about the code and its architecture

Changes are pushed to an ``main` only when you click **Push**.

## Config
All backend config lives in `backend/.env`, which is gitignored:
`OPENAI_API_KEY`, `GITHUB_TOKEN`, `APP_TOKEN`, `OPENAI_MODEL`, `WORK_REPO`, `FRONTEND_ORIGIN`.

`GITHUB_TOKEN` should be a fine-grained PAT with **Contents: read & write** on the `WORK_REPO` repo only.

## Local
```sh
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt && ./start.sh
cd frontend && npm install && npm run dev   # .env.local: VITE_API_URL, VITE_APP_TOKEN
```

## Deploy
```sh
FRONTEND_ORIGIN=https://<your-app>.vercel.app backend/deploy.sh
```
The script copies the keys from `backend/.env` into Secret Manager and deploys to Cloud Run with `--max-instances=1`, because sessions live on one instance.

Frontend (Vercel): set the root directory to `frontend`, then set `VITE_API_URL` to the Cloud Run URL and `VITE_APP_TOKEN` to the same `APP_TOKEN`.

## Work repo auto-deploy
`infra/setup-work-dir.sh` is a one-time setup. It lets GitHub Actions in `WORK_REPO` deploy to the Cloud Run service `cloud-code-work-dir` on every push to `main`, using Workload Identity Federation (no keys). It also pushes `infra/deploy.yml` to the repo as `.github/workflows/deploy.yml`.

`.github/` is permanent: agents can't modify it, and **Reset repo** deletes everything else.
