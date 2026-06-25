#!/usr/bin/env bash
# §11.3 deployment helper. Fill in PROJECT_ID before first run, or override
# via env vars on the command line:
#
#   PROJECT_ID=my-gcp-project ./deploy.sh
#
# Pre-requisites:
#   * `gcloud` CLI authenticated against PROJECT_ID
#   * Firestore (Native mode) enabled in the same region
#   * A secret named SECRET_NAME exists in Secret Manager and holds the
#     Slack Signing Secret (created via `gcloud secrets create ... \
#         --replication-policy=automatic`)
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-CHANGE_ME}"
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-mol-slack-viewer}"
SECRET_NAME="${SECRET_NAME:-slack-signing-secret}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"

if [ "$PROJECT_ID" = "CHANGE_ME" ]; then
    echo "ERROR: set PROJECT_ID env var or edit this file." >&2
    exit 1
fi

gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --allow-unauthenticated \
    --set-env-vars "SLACK_RESPONSE_TYPE=ephemeral,FIRESTORE_COLLECTION=molecules,RETENTION_DAYS=7,MAX_ATOMS=200,OPSIN_BACKEND=local" \
    --set-secrets "SLACK_SIGNING_SECRET=${SECRET_NAME}:latest" \
    --min-instances "$MIN_INSTANCES"
