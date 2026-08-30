#!/usr/bin/env bash
# Deploy the Options Scout daily email as a Cloud Run Job + Cloud Scheduler.
# ANALYSIS ONLY -- the container has no trading-lane code and can place no order.
#
# Idempotent-ish and safe to re-run. Run from the agent/ dir with gcloud authed:
#   bash deploy/deploy-options-scout.sh
#
# Secrets (already in Secret Manager): massive-api, gmail-app-password.
# They are injected as env vars; the container never calls gcloud.
set -euo pipefail

PROJECT="${PROJECT:-digitalsolomon-creator}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-options-scout}"
REPO="${REPO:-scouts}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:latest"
SA_NAME="options-scout-sa"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHED_JOB="${JOB}-daily"
# Pre-market, weekdays, 08:00 America/New_York.
CRON="${CRON:-0 8 * * 1-5}"
TZONE="America/New_York"

echo "==> Enabling required APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com --project="$PROJECT"

echo "==> Artifact Registry repo ($REPO)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker \
       --location="$REGION" --project="$PROJECT" --description="Daily scout jobs"

echo "==> Build + push image (Cloud Build)"
gcloud builds submit --project="$PROJECT" \
  --config=deploy/cloudbuild.options-scout.yaml \
  --substitutions=_IMAGE="$IMAGE" .

echo "==> Runtime service account + secret access"
gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
       --display-name="Options Scout daily job"
for S in massive-api gmail-app-password; do
  gcloud secrets add-iam-policy-binding "$S" --project="$PROJECT" \
    --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
done

echo "==> Deploy Cloud Run Job (secrets injected as env)"
gcloud run jobs deploy "$JOB" --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" --service-account="$SA" \
  --set-env-vars="DS_VAULT_NO_GCLOUD=1" \
  --set-secrets="MASSIVE_API_KEY=massive-api:latest,GMAIL_APP_PASSWORD=gmail-app-password:latest" \
  --max-retries=1 --task-timeout=900s --memory=512Mi

echo "==> Allow the scheduler SA to run the job"
gcloud run jobs add-iam-policy-binding "$JOB" --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null

echo "==> Cloud Scheduler -> run the job daily pre-market ($CRON $TZONE)"
gcloud scheduler jobs delete "$SCHED_JOB" --location="$REGION" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http "$SCHED_JOB" --project="$PROJECT" --location="$REGION" \
  --schedule="$CRON" --time-zone="$TZONE" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run" \
  --http-method=POST --oauth-service-account-email="$SA"

echo ""
echo "Deployed. Run it once now to confirm:"
echo "  gcloud run jobs execute $JOB --region $REGION --project $PROJECT"
