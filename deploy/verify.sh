#!/usr/bin/env bash
set -Eeuo pipefail

base_url="${1:-https://todo2api.codeai.de5.net}"
token="${TODO2API_CLIENT_TOKEN:?TODO2API_CLIENT_TOKEN is required}"

health_status="$(curl -sS -o /tmp/todo2api-health.out -w '%{http_code}' "${base_url}/healthz")"
[[ "${health_status}" == "200" ]] || {
  printf 'health check failed: status=%s body=%s\n' "${health_status}" "$(cat /tmp/todo2api-health.out)" >&2
  exit 1
}

root_status="$(curl -sS -o /tmp/todo2api-root.out -w '%{http_code}' "${base_url}/")"
[[ "${root_status}" == "200" ]] || {
  printf 'WebUI check failed: status=%s\n' "${root_status}" >&2
  exit 1
}

unauthorized_status="$(curl -sS -o /tmp/todo2api-unauthorized.out -w '%{http_code}' "${base_url}/v1/models")"
[[ "${unauthorized_status}" == "401" ]] || {
  printf 'unauthorized check failed: status=%s\n' "${unauthorized_status}" >&2
  exit 1
}

models_status="$(curl -sS -o /tmp/todo2api-models.out -w '%{http_code}' \
  -H "Authorization: Bearer ${token}" "${base_url}/v1/models")"
[[ "${models_status}" == "200" ]] || {
  printf 'authenticated models check failed: status=%s body=%s\n' \
    "${models_status}" "$(cat /tmp/todo2api-models.out)" >&2
  exit 1
}

printf 'health=200 webui=200 unauthorized=401 models=200\n'
