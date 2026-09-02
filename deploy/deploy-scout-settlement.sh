#!/usr/bin/env bash
# Deploy the Scout Settlement / accuracy engine as a Cloud Run Job + Scheduler.
# ANALYSIS ONLY -- the container reads delayed public bars and writes WIN/LOSS/
# OPEN verdicts to the shared day-state. It has no trading-lane code and can place
# no order; it never sends email.
#
# Idempotent-ish and safe to re-run. Run from the agent/ dir with gcloud authed:
#   bash deploy/deploy-scout-settlement.sh
#
# Secret (already in Secret Manager): massive-api. Injected as an env var; the
# container never calls gcloud. The shared day-state lives in the same GCS bucket
# the scouts write (ENTRY_ALERTS_BUCKET); this job reads plays and writes outcomes.
#
# The one-time setup steps (services enable, secret/bucket IAM bindings) are
# guarded so a re-deploy under a least-privilege account never aborts before the
# build -- the bindings they re-assert already exist after the first deploy.
set -euo pipefail

PROJECT="${PROJECT:-digitalsolomon-creator}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-scout-settlement}"
REPO="${REPO:-scouts}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${JOB}:latest"
# Reuse the entry-alerts runtime SA: it already has exactly what settlement needs
# -- massive-api secretAccessor + objectAdmin on the shared day-state bucket --
# and reusing it avoids needing service-account-create rights for a hands-off
# deploy. Override with SA_NAME=scout-settlement-sa (pre-created) for isolation.
SA_NAME="${SA_NAME:-entry-alerts-sa}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SCHED_JOB="${JOB}-daily"
BUCKET="${ENTRY_ALERTS_BUCKET:-digitalsolomon-entry-alerts}"
# After the close, weekdays, 18:00 America/New_York -- late enough that the day's
# EOD bars are available. Off-hours/holiday runs are cheap idempotent no-ops.
CRON="${CRON:-0 18 * * 1-5}"
TZONE="America/New_York"

echo "==> Enabling required APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com --project="$PROJECT" \
  || echo "   (skipped — APIs already enabled, or account lacks serviceusage.enable; safe to continue)"

echo "==> Artifact Registry repo ($REPO)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" --repository-format=docker \
       --location="$REGION" --project="$PROJECT" --description="Daily scout jobs"

echo "==> Build + push image (Cloud Build)"
gcloud builds submit --project="$PROJECT" \
  --config=deploy/cloudbuild.scout-settlement.yaml \
  --substitutions=_IMAGE="$IMAGE" .

echo "==> Runtime service account + access"
if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
    --display-name="Scout settlement / accuracy job"
  for _ in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 && break
    sleep 5
  done
fi
gcloud secrets add-iam-policy-binding massive-api --project="$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null \
  || echo "   (skipped massive-api binding — already present, or account lacks secret setIamPolicy; safe to continue)"

echo "==> Grant the settlement SA read/write on the day-state bucket"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --project="$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin" >/dev/null \
  || echo "   (skipped bucket binding — already present, or account lacks storage setIamPolicy; safe to continue)"

echo "==> Deploy Cloud Run Job (Massive key injected as env)"
gcloud run jobs deploy "$JOB" --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" --service-account="$SA" \
  --set-env-vars="DS_VAULT_NO_GCLOUD=1,ENTRY_ALERTS_BUCKET=${BUCKET}" \
  --set-secrets="MASSIVE_API_KEY=massive-api:latest" \
  --max-retries=1 --task-timeout=900s --memory=512Mi

echo "==> Allow the scheduler SA to run the job"
gcloud run jobs add-iam-policy-binding "$JOB" --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:${SA}" --role="roles/run.invoker" >/dev/null \
  || echo "   (skipped invoker binding — already present; safe to continue)"

echo "==> Cloud Scheduler -> settle after the close each weekday ($CRON $TZONE)"
gcloud scheduler jobs delete "$SCHED_JOB" --location="$REGION" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http "$SCHED_JOB" --project="$PROJECT" --location="$REGION" \
  --schedule="$CRON" --time-zone="$TZONE" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run" \
  --http-method=POST --oauth-service-account-email="$SA"

echo ""
echo "Deployed. Run one sweep now to confirm:"
echo "  gcloud run jobs execute $JOB --region $REGION --project $PROJECT"
