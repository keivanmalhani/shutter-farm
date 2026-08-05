#!/bin/bash
# Deploy the farm as a Cloud Run Job against a Filestore or GCS-backed volume.
#
# Cloud Run Jobs is the closest managed thing to what this container wants:
# run to completion, exit honestly, let the platform own the schedule and
# the retries. No cluster to keep alive between nightly sweeps.
#
# The media itself still never leaves the customer's own storage. That is
# the point worth making: local-first is a promise about where data lives,
# not a claim that the software cannot be operated on real infrastructure.
#
#   ./deploy/cloud-run-job.sh my-project us-central1

set -euo pipefail

PROJECT="${1:?usage: cloud-run-job.sh PROJECT REGION}"
REGION="${2:?usage: cloud-run-job.sh PROJECT REGION}"
JOB="${JOB:-shutter-farm}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT}/shutter/shutter-farm:0.1.0}"

# The media share and the state share are separate on purpose: the archive
# can be mounted read-only, the ledger cannot.
MEDIA_INSTANCE="${MEDIA_INSTANCE:-archive}"
MEDIA_SHARE="${MEDIA_SHARE:-photos}"
STATE_BUCKET="${STATE_BUCKET:-${PROJECT}-shutter-farm-state}"
VPC_CONNECTOR="${VPC_CONNECTOR:-shutter-connector}"

echo "==> Building and pushing ${IMAGE}"
gcloud builds submit --project "${PROJECT}" --tag "${IMAGE}" .

echo "==> Creating or updating the job"
VERB=create
gcloud run jobs describe "${JOB}" --project "${PROJECT}" --region "${REGION}" \
  >/dev/null 2>&1 && VERB=update

gcloud run jobs "${VERB}" "${JOB}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --args="run" \
  --cpu=4 \
  --memory=8Gi \
  --max-retries=1 \
  --task-timeout=6h \
  --parallelism=1 \
  --set-env-vars="FARM_ROOT=/media,FARM_STATE=/state/shutter-farm-state.json,FARM_WRITE=false,FARM_TIMEOUT=3600" \
  --network-interface="network=default,subnet=default" \
  --vpc-connector="${VPC_CONNECTOR}" \
  --add-volume="name=media,type=nfs,location=${MEDIA_INSTANCE}:/${MEDIA_SHARE}" \
  --add-volume-mount="volume=media,mount-path=/media" \
  --add-volume="name=state,type=cloud-storage,bucket=${STATE_BUCKET}" \
  --add-volume-mount="volume=state,mount-path=/state"

echo "==> Scheduling it nightly"
gcloud scheduler jobs create http "${JOB}-nightly" \
  --project "${PROJECT}" \
  --location "${REGION}" \
  --schedule="0 3 * * *" \
  --time-zone="America/Mexico_City" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/${JOB}:run" \
  --http-method=POST \
  --oauth-service-account-email="shutter-farm@${PROJECT}.iam.gserviceaccount.com" \
  2>/dev/null || echo "    schedule already exists, leaving it alone"

cat <<EOF

Done.

  Run it now:     gcloud run jobs execute ${JOB} --region ${REGION}
  Watch it:       gcloud run jobs executions list --job ${JOB} --region ${REGION}
  Read the logs:  gcloud logging read 'resource.type=cloud_run_job AND jsonPayload.service=shutter-farm' --limit 50

The logs are already structured, so jsonPayload.event and jsonPayload.folder
are queryable fields with no parser and no agent.
EOF
