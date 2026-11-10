#!/bin/sh
set -eu

secret_file=/run/secrets/approver_secret
if [ ! -r "$secret_file" ]; then
    printf '%s\n' "Approver secret is unavailable." >&2
    exit 1
fi

APPROVER_SECRET="$(cat "$secret_file")"
if [ "${#APPROVER_SECRET}" -lt 32 ]; then
    printf '%s\n' "Approver secret must be at least 32 characters." >&2
    exit 1
fi
export APPROVER_SECRET

exec uvicorn agentsentinel.approval_service:app --host 0.0.0.0 --port 8000
