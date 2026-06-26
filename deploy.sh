#!/usr/bin/env bash
# §11.3 deployment helper. Fill in PROJECT_ID before first run, or override
# via env vars on the command line:
#
#   PROJECT_ID=my-gcp-project ./deploy.sh
#
# Pre-requisites:
#   * `gcloud` CLI authenticated against PROJECT_ID
#   * Firestore (Native mode) enabled in the same region
#   * Artifact Registry repo `cloud-run-source-deploy` already created
#     in $REGION (`gcloud run deploy --source .` creates it on the
#     first deploy of any service; if this is your very first deploy,
#     run DEPLOY.md §5 once first to seed the AR repo)
#   * A secret named SECRET_NAME exists in Secret Manager and holds the
#     Slack Signing Secret (created via `gcloud secrets create ... \
#         --replication-policy=automatic`)
#
# What this does:
#   1. `gcloud builds submit --config cloudbuild.yaml` runs the build
#      step from cloudbuild.yaml, which uses `--cache-from` against the
#      previously published image. Re-deploys whose only diff is app/
#      code finish in ~1-2 min (build step only) instead of the ~5-8
#      min that `gcloud run deploy --source .` would take with no
#      layer cache.
#   2. `gcloud run deploy ... --image <AR URI>` deploys the built
#      image. We pass `--image` (NOT `--source`) to skip a second
#      build, which would bypass the cache.
#
# Env-var policy: this script uses `--update-env-vars` (merge), NOT
# `--set-env-vars` (replace). The Phase 1 list below is overridden;
# any vars already set on the service (e.g. Phase 3's TASKS_*,
# BASE_URL per DEPLOY.md §5.6.5) are PRESERVED. This makes the script
# safe to run against a Phase 3 prod service without wiping Cloud
# Tasks config.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-CHANGE_ME}"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-mol-slack-viewer}"
SECRET_NAME="${SECRET_NAME:-slack-signing-secret}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
AR_REPO="${AR_REPO:-cloud-run-source-deploy}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}:latest"

if [ "$PROJECT_ID" = "CHANGE_ME" ]; then
    echo "ERROR: set PROJECT_ID env var or edit this file." >&2
    exit 1
fi

# Step 1: Build via Cloud Build with --cache-from (cloudbuild.yaml).
# --region: run the build worker in the same region as the AR repo
# (asia-northeast1). The default global pool runs in us-central1, which
# means every `docker pull <prev image>` and `docker push <new image>`
# crosses Asia <-> US and bills cross-region egress. Regional execution
# keeps those image bytes (~500 MB) inside asia-northeast1.
# Source staging bucket: stays as gs://<PROJECT_ID>_cloudbuild (US) by
# default; gcloud only switches to gs://<PROJECT_ID>_<REGION>_cloudbuild
# when --default-buckets-behavior=REGIONAL_USER_OWNED_BUCKET is also
# passed. The per-build source tarball is small (~100 KB) so the
# remaining cross-region download is negligible (~1e-5 USD/build), and
# the legacy US bucket must be kept (do NOT delete it).
# --suppress-logs: skip log streaming. Some accounts hit "Viewer/Owner"
# streaming permission errors even with Owner role; suppressing the
# stream sidesteps the check. Build output stays in the Cloud Build
# console.
gcloud builds submit \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --config cloudbuild.yaml \
    --substitutions "_REGION=${REGION},_REPO=${AR_REPO},_IMAGE=${SERVICE_NAME}" \
    --suppress-logs

# Step 2: Deploy from AR. We pass --image (not --source) so Cloud Run
# uses the cache-hot image we just built rather than triggering a
# second, cache-cold build.
gcloud run deploy "$SERVICE_NAME" \
    --image "$IMAGE_URI" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --allow-unauthenticated \
    --update-env-vars "SLACK_RESPONSE_TYPE=ephemeral,FIRESTORE_COLLECTION=molecules,RETENTION_DAYS=7,MAX_ATOMS=200,OPSIN_BACKEND=local" \
    --set-secrets "SLACK_SIGNING_SECRET=${SECRET_NAME}:latest" \
    --cpu-throttling \
    --min-instances "$MIN_INSTANCES"
# NOTE: --cpu-throttling is the Cloud Run default and is enumerated here
# only to make the choice explicit. Do NOT replace it with
# --no-cpu-throttling: Phase 1 runs /slack/mol synchronously, so it does
# not benefit from always-allocated CPU, and the latter mode bills CPU
# continuously while an instance is active.
