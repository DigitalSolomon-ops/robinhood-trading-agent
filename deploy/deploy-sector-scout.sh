#!/usr/bin/env bash
# Deploy the Sector Scout daily email as a Cloud Run Job + Cloud Scheduler.
# ANALYSIS ONLY -- the image contains no order path (see Dockerfile.sector-scout).
# Run from agent/:  bash deploy/deploy-sector-scout.sh
set -euo pipefail

PROJECT="digitalsolomon-creator"
REGION="us-central1"
JOB="sector-scout"
REPO="scouts"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:latest"
# Reuse the EXISTING options-scout runtime SA (house pattern; the settlement
# job reuses entry-alerts-sa the same way). It already holds secretAccessor on
# massive-api / gmail-app-password and write access to the entry-alerts bucket
# the sector state rides in -- and the hands-off deploy SA cannot create SAs.
SA_NAME="options-scout-sa"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHED_JOB="${JOB}-daily"
# Daily IN SESSION (2026-09-07 brief: roughly 15:00-19:00 UTC). 13:00 ET
# keeps quotes live for the ticket half of the report; the send gate ships
# the board-only short form on any run that cannot price a structure.
CRON="0 13 * * 1-5"
TZONE="America/New_York"

echo "== APIs =="
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT" || echo "(enable skipped)"

echo "== Artifact Registry repo =="
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project "$PROJECT" \
  >/dev/null 2>&1 || gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" --project "$PROJECT"

echo "== Build =="
gcloud builds submit --config=deploy/cloudbuild.sector-scout.yaml \
  --substitutions=_IMAGE="$IMAGE" --project "$PROJECT" .

echo "== Service account =="
if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  # Guarded: the hands-off deploy identity cannot create SAs; the reused SA
  # should already exist, so a create failure here must not abort the deploy.
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" \
    --display-name="Sector Scout (analysis-only email job)" \
    || echo "  (SA create skipped -- reusing existing $SA)"
  for i in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 && break
    echo "  waiting for SA propagation ($i/12)"; sleep 5
  done
fi

echo "== Secret access =="
SECRETS="MASSIVE_API_KEY=massive-api:latest,GMAIL_APP_PASSWORD=gmail-app-password:latest"
for S in massive-api gmail-app-password finnhub; do
  if gcloud secrets describe "$S" --project "$PROJECT" >/dev/null 2>&1; then
    # Guarded: idempotent grant; the hands-off deploy identity lacks
    # setIamPolicy and the reused SA already holds these from its own deploy.
    gcloud secrets add-iam-policy-binding "$S" --project "$PROJECT" \
      --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null \
      || echo "  (binding on $S skipped -- already granted; safe to continue)"
  else
    echo "  (secret $S not present; skipping)"
  fi
done
if gcloud secrets describe finnhub --project "$PROJECT" >/dev/null 2>&1; then
  SECRETS="${SECRETS},FINNHUB_API_KEY=finnhub:latest"
fi

echo "== Cloud Run job =="
# 3600s: the stock-side entitlement is ~5 req/min, so a run with breadth
# backfill legitimately takes 25-45 minutes (a verified local run took ~29).
gcloud run jobs deploy "$JOB" \
  --image "$IMAGE" \
  --region "$REGION" --project "$PROJECT" \
  --service-account "$SA" \
  --set-env-vars="DS_VAULT_NO_GCLOUD=1,MASSIVE_MIN_INTERVAL_SECONDS=13" \
  --set-secrets="$SECRETS" \
  --max-retries=1 \
  --task-timeout=3600s \
  --memory=512Mi

gcloud run jobs add-iam-policy-binding "$JOB" \
  --region "$REGION" --project "$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null \
  || echo "  (invoker binding skipped -- already granted; safe to continue)"

echo "== Scheduler =="
gcloud scheduler jobs delete "$SCHED_JOB" --location "$REGION" --project "$PROJECT" --quiet || true
gcloud scheduler jobs create http "$SCHED_JOB" \
  --location "$REGION" --project "$PROJECT" \
  --schedule="$CRON" --time-zone="$TZONE" \
  --http-method=POST \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run" \
  --oauth-service-account-email="$SA"

echo ""
echo "Deployed. Verify with:"
echo "  gcloud run jobs execute $JOB --region $REGION --project $PROJECT"
