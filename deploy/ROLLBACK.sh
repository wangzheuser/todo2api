#!/usr/bin/env bash
set -Eeuo pipefail

backup_dir="${1:?usage: ROLLBACK.sh BACKUP_DIR}"
nginx_dir="${NGINX_DIR:-/opt/docker_projects/nginx-proxy}"
project_dir="${PROJECT_DIR:-/opt/docker_projects/todo2api}"
docker_bin="${DOCKER_BIN:-docker}"

[[ -f "${backup_dir}/nginx.conf" ]] || {
  printf 'missing backup: %s/nginx.conf\n' "${backup_dir}" >&2
  exit 1
}

if [[ -f "${project_dir}/compose.yaml" ]]; then
  "${docker_bin}" compose -f "${project_dir}/compose.yaml" --project-directory "${project_dir}" down
fi

cp "${backup_dir}/nginx.conf" "${nginx_dir}/nginx.conf"
chmod 0644 "${nginx_dir}/nginx.conf"
if [[ -f "${backup_dir}/044-todo2api-codeai-de5-net.conf" ]]; then
  install -m 0644 "${backup_dir}/044-todo2api-codeai-de5-net.conf" \
    "${nginx_dir}/config-src/http.d/044-todo2api-codeai-de5-net.conf"
else
  rm -f "${nginx_dir}/config-src/http.d/044-todo2api-codeai-de5-net.conf"
fi

"${docker_bin}" compose -f "${nginx_dir}/docker-compose.yml" --project-directory "${nginx_dir}" \
  up -d --force-recreate nginx-proxy
"${docker_bin}" exec nginx-proxy nginx -t
printf 'rollback complete: application stopped and nginx configuration restored\n'
