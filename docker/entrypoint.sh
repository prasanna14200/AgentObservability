#!/bin/sh
set -eu

secret_file=/run/secrets/approver_secret
if [ -r "$secret_file" ]; then
    APPROVER_SECRET="$(cat "$secret_file")"
else
    APPROVER_SECRET="${APPROVER_SECRET:-}"
fi
if [ "${#APPROVER_SECRET}" -lt 32 ]; then
    printf '%s\n' "Approver secret must be at least 32 characters." >&2
    exit 1
fi
export APPROVER_SECRET

case "${PORT:-8000}" in
    ''|*[!0-9]*)
        printf '%s\n' "PORT must be a positive integer." >&2
        exit 1
        ;;
esac
if [ "${PORT:-8000}" -lt 1 ]; then
    printf '%s\n' "PORT must be a positive integer." >&2
    exit 1
fi

exec uvicorn agentsentinel.approval_service:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}"
