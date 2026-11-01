#!/bin/sh
# Deploy to Cloud Run using the same values as backend/.env (secrets go to Secret Manager).
# Usage: FRONTEND_ORIGIN=https://<app>.vercel.app ./deploy.sh
set -e
cd "$(dirname "$0")"
ORIGIN=${FRONTEND_ORIGIN:-http://localhost:5173}
set -a; . ./.env; set +a
PROJECT=$(gcloud config get-value project)
REGION=${REGION:-us-central1}
SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

put_secret() {  # name value
  gcloud secrets describe "$1" >/dev/null 2>&1 || gcloud secrets create "$1" --replication-policy=automatic
  printf %s "$2" | gcloud secrets versions add "$1" --data-file=-
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
}
put_secret openai-key "$OPENAI_API_KEY"
put_secret github-token "$GITHUB_TOKEN"
put_secret app-token "$APP_TOKEN"

gcloud run deploy cloud-code --source . --region "$REGION" --allow-unauthenticated \
  --max-instances=1 --timeout=3600 --memory=2Gi \
  --set-secrets=OPENAI_API_KEY=openai-key:latest,GITHUB_TOKEN=github-token:latest,APP_TOKEN=app-token:latest \
  --set-env-vars="WORK_REPO=$WORK_REPO,OPENAI_MODEL=$OPENAI_MODEL,FRONTEND_ORIGIN=$ORIGIN"
