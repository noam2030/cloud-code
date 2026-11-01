#!/bin/sh
# One-time setup: lets GitHub Actions in WORK_REPO deploy to Cloud Run (keyless, via Workload Identity Federation),
# then pushes the deploy workflow to the repo. Safe to re-run.
set -e
P=${GCP_PROJECT:-$(gcloud config get-value project)}
REGION=${REGION:-us-central1}
REPO=${WORK_REPO:-noam2030/cloud-code-work-dir}
N=$(gcloud projects describe "$P" --format='value(projectNumber)')
SA=github-deployer@$P.iam.gserviceaccount.com
PROVIDER=projects/$N/locations/global/workloadIdentityPools/github/providers/github

echo "== APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com iam.googleapis.com --project "$P"

echo "== Deployer service account + roles"
gcloud iam service-accounts describe "$SA" --project "$P" >/dev/null 2>&1 ||
  gcloud iam service-accounts create github-deployer --display-name="GitHub Actions deployer" --project "$P"
for r in run.admin cloudbuild.builds.editor artifactregistry.admin storage.admin iam.serviceAccountUser serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$P" --member="serviceAccount:$SA" --role="roles/$r" --condition=None >/dev/null
done

echo "== Workload Identity pool (trusts only $REPO)"
gcloud iam workload-identity-pools describe github --location=global --project "$P" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create github --location=global --display-name="GitHub" --project "$P"
gcloud iam workload-identity-pools providers describe github --workload-identity-pool=github --location=global --project "$P" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools providers create-oidc github --workload-identity-pool=github --location=global --project "$P" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='$REPO'"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$P" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$N/locations/global/workloadIdentityPools/github/attribute.repository/$REPO" >/dev/null

echo "== GitHub repo variables"
gh variable set GCP_PROJECT --repo "$REPO" --body "$P"
gh variable set GCP_REGION --repo "$REPO" --body "$REGION"
gh variable set GCP_SA --repo "$REPO" --body "$SA"
gh variable set WIF_PROVIDER --repo "$REPO" --body "$PROVIDER"

echo "== Push workflow"
TMP=$(mktemp -d)
gh repo clone "$REPO" "$TMP" -- -q
mkdir -p "$TMP/.github/workflows"
cp "$(dirname "$0")/deploy.yml" "$TMP/.github/workflows/deploy.yml"
cd "$TMP"
git add .github
git diff --cached --quiet || { git commit -qm "Add Cloud Run deploy workflow"; git -c credential.helper= -c credential.helper="!gh auth git-credential" push -q origin HEAD:main; }
rm -rf "$TMP"
echo "Done. Runs: https://github.com/$REPO/actions"
