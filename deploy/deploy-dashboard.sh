#!/usr/bin/env bash
# Deploy the 007 trader dashboard as a Cloud Run SERVICE (control surface +
# per-lane arm toggle). Arm state is shared via Firestore so the toggle reaches a
# co-located trader that reads the same store. Fronted by the IAP creator-lb;
# the host rule + IAP binding are wired separately (see DEPLOY-RUNBOOK.md).
#
# Run from agent/ with gcloud authed:  bash deploy/deploy-dashboard.sh
set -euo pipefail

PROJECT="${PROJECT:-digitalsolomon-creator}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-ds-trader}"
REPO="${REPO:-scouts}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:latest"
SA_NAME="ds-trader-sa"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "==> Enable APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com firestore.googleapis.com --project="$PROJECT"

echo "==> Artifact Registry repo ($REPO)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker \
       --location="$REGION" --project="$PROJECT" --description="DS Cloud Run images"

echo "==> Build + push image (Cloud Build)"
gcloud builds submit --project="$PROJECT" \
  --config=deploy/cloudbuild.dashboard.yaml \
  --substitutions=_IMAGE="$IMAGE" .

echo "==> Runtime service account + Firestore access"
gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
       --display-name="007 dashboard runtime"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/datastore.user" >/dev/null

echo "==> Deploy Cloud Run service ($SERVICE)"
gcloud run deploy "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" --service-account="$SA" \
  --no-allow-unauthenticated --port=8080 \
  --set-env-vars="TRADER_ARM_FIRESTORE_PROJECT=${PROJECT},DS_VAULT_NO_GCLOUD=1" \
  --memory=512Mi --cpu=1 --min-instances=0 --max-instances=2

echo "==> Service URL:"
gcloud run services describe "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --format="value(status.url)"
