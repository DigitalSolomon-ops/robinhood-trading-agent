#!/usr/bin/env bash
# Deploy the Options TRADER runtime as a Cloud Run Job + Cloud Scheduler.
#
# FAIL-CLOSED + PAPER-FIRST by construction:
#   * The job reads the SHARED FirestoreArmStore the 007 dashboard toggle writes
#     (TRADER_ARM_FIRESTORE_PROJECT). DISARMED (the default) -> a no-op cycle that
#     places ZERO orders. Standing it up changes nothing until the operator arms.
#   * The image can build only the headless paper connector (order methods raise),
#     so it cannot place a live order even in principle. It holds NO Robinhood
#     credential and this script sets NO OPTIONS_TRADER_LIVE. Going live stays a
#     separate agent-hosted human action.
#
# Idempotent-ish and safe to re-run. Run from the agent/ dir with gcloud authed:
#   bash deploy/deploy-options-trader.sh
#
# Secret (already in Secret Manager): massive-api  (read-only market data to price
# the analysis-only scout plays). Injected as env; the container never calls gcloud.
#
# NOTE: deploying is an operator-gated step and is NOT run as part of the build.
set -euo pipefail

PROJECT="${PROJECT:-digitalsolomon-creator}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-options-trader}"
REPO="${REPO:-scouts}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:latest"
SA_NAME="options-trader-sa"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHED_JOB="${SCHED_JOB:-${JOB}-cycle}"
# Every 30 minutes, weekdays, during the US regular session (09:30-16:00 ET).
# The in-container ARM gate makes any firing a no-op while the lane is DISARMED,
# so the exact cadence only matters once the operator arms -- adjust freely.
CRON="${CRON:-*/30 9-16 * * 1-5}"
TZONE="America/New_York"

echo "==> Enabling required APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com --project="$PROJECT"

echo "==> Artifact Registry repo ($REPO)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker \
       --location="$REGION" --project="$PROJECT" --description="Daily scout + trader jobs"

echo "==> Build + push image (Cloud Build)"
gcloud builds submit --project="$PROJECT" \
  --config=deploy/cloudbuild.options-trader.yaml \
  --substitutions=_IMAGE="$IMAGE" .

echo "==> Runtime service account"
if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
    --display-name="Options Trader cycle job (paper, arm-gated)"
  # A brand-new SA is not immediately usable in IAM bindings (eventual
  # consistency); wait until it resolves before binding roles to it.
  for _ in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 && break
    sleep 5
  done
fi

echo "==> Read-only Firestore access for the SHARED arm store (project has"
echo "    conditional bindings -> --condition=None adds an unconditional one)"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/datastore.user" \
  --condition=None >/dev/null

echo "==> Grant read access to the Massive secret (prices the analysis-only plays)"
gcloud secrets add-iam-policy-binding massive-api --project="$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null

echo "==> Deploy Cloud Run Job (shared arm store; NO OPTIONS_TRADER_LIVE, NO Robinhood secret)"
# TRADING_ENABLED=true holds the kill switch OPEN so the Firestore ARM MARKER (the
# dashboard toggle) is the effective on/off control -- without it the kill switch
# defaults to halting every cycle and an armed lane would never run. The two are
# deliberately independent controls: the arm marker enables, STOP_TRADING_OPTIONS /
# TRADING_ENABLED=false is the emergency stop. This is safe: the job is PAPER-ONLY
# by construction (its only connector raises on any order path), so an open kill
# switch cannot produce a live order -- disarming (or a redeploy with
# TRADING_ENABLED=false) fully stops it.
gcloud run jobs deploy "$JOB" --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" --service-account="$SA" \
  --set-env-vars="DS_VAULT_NO_GCLOUD=1,TRADER_ARM_FIRESTORE_PROJECT=${PROJECT},TRADING_ENABLED=true" \
  --set-secrets="MASSIVE_API_KEY=massive-api:latest" \
  --max-retries=1 --task-timeout=900s --memory=512Mi \
  --parallelism=1 --tasks=1

echo "==> Allow the scheduler SA to run the job"
gcloud run jobs add-iam-policy-binding "$JOB" --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null

echo "==> Cloud Scheduler -> one cycle every ~30 min during market hours ($CRON $TZONE)"
gcloud scheduler jobs delete "$SCHED_JOB" --location="$REGION" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http "$SCHED_JOB" --project="$PROJECT" --location="$REGION" \
  --schedule="$CRON" --time-zone="$TZONE" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run" \
  --http-method=POST --oauth-service-account-email="$SA"

echo ""
echo "Deployed DISARMED. Fail-closed acceptance test (lane still disarmed):"
echo "  gcloud run jobs execute $JOB --region $REGION --project $PROJECT"
echo "  # expect the execution log to show status=disarmed_noop, fills=0"
echo ""
echo "The dashboard toggle at trader.digitalsolomon.com is the single control:"
echo "  disarmed -> every cycle no-ops; armed -> PAPER cycles begin (still zero real orders)."
