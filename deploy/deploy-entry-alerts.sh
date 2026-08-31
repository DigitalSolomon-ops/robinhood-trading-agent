#!/usr/bin/env bash
# Deploy the Entry-Hit Alerter as a Cloud Run Job + Cloud Scheduler.
# NOTIFICATION ONLY -- the container has no trading-lane code and can place no
# order. It reads delayed prices and sends alert emails.
#
# Idempotent-ish and safe to re-run. Run from the agent/ dir with gcloud authed:
#   bash deploy/deploy-entry-alerts.sh
#
# Secrets (already in Secret Manager): massive-api, gmail-app-password.
# They are injected as env vars; the container never calls gcloud. The shared
# day-state lives in a GCS bucket (ENTRY_ALERTS_BUCKET) written by the scouts.
#
# NOTE: deploying is an operator-gated step and is NOT run as part of the build.
set -euo pipefail

PROJECT="${PROJECT:-digitalsolomon-creator}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-entry-alerts}"
REPO="${REPO:-scouts}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:latest"
SA_NAME="entry-alerts-sa"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHED_JOB="${JOB}-poll"
BUCKET="${ENTRY_ALERTS_BUCKET:-digitalsolomon-entry-alerts}"
# Every 10 minutes, weekdays, during the US regular session (09:30-16:00 ET).
# The in-container market-hours guard makes any off-hours firing a cheap no-op,
# so an approximate window here is safe; this just avoids needless invocations.
CRON="${CRON:-*/10 9-16 * * 1-5}"
TZONE="America/New_York"

echo "==> Enabling required APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com --project="$PROJECT"

echo "==> Artifact Registry repo ($REPO)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker \
       --location="$REGION" --project="$PROJECT" --description="Daily scout jobs"

echo "==> GCS bucket for the shared day-state ($BUCKET)"
gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET}" --project="$PROJECT" --location="$REGION"

echo "==> Build + push image (Cloud Build)"
gcloud builds submit --project="$PROJECT" \
  --config=deploy/cloudbuild.entry-alerts.yaml \
  --substitutions=_IMAGE="$IMAGE" .

echo "==> Runtime service account + secret access"
if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
    --display-name="Entry-Hit Alerter poll job"
  # A brand-new SA is not immediately usable in IAM bindings (eventual
  # consistency); wait until it resolves before binding secrets/bucket to it.
  for _ in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 && break
    sleep 5
  done
fi
for S in massive-api gmail-app-password; do
  gcloud secrets add-iam-policy-binding "$S" --project="$PROJECT" \
    --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
done

echo "==> Grant the alerter SA read/write on the day-state bucket"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --project="$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin" >/dev/null

echo "==> Deploy Cloud Run Job (secrets injected as env; ~10-min task timeout)"
gcloud run jobs deploy "$JOB" --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" --service-account="$SA" \
  --set-env-vars="DS_VAULT_NO_GCLOUD=1,ENTRY_ALERTS_BUCKET=${BUCKET}" \
  --set-secrets="MASSIVE_API_KEY=massive-api:latest,GMAIL_APP_PASSWORD=gmail-app-password:latest" \
  --max-retries=1 --task-timeout=600s --memory=512Mi

echo "==> Allow the scheduler SA to run the job"
gcloud run jobs add-iam-policy-binding "$JOB" --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null

echo "==> Cloud Scheduler -> poll every ~10 min during market hours ($CRON $TZONE)"
gcloud scheduler jobs delete "$SCHED_JOB" --location="$REGION" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http "$SCHED_JOB" --project="$PROJECT" --location="$REGION" \
  --schedule="$CRON" --time-zone="$TZONE" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run" \
  --http-method=POST --oauth-service-account-email="$SA"

echo ""
echo "Deployed. Run one cycle now to confirm:"
echo "  gcloud run jobs execute $JOB --region $REGION --project $PROJECT"
