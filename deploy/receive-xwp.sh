#!/usr/bin/env bash
# Intended as a forced SSH command on the deployment host.
set -Eeuo pipefail
umask 077

base=/opt/ainew-xwp
sha=${SSH_ORIGINAL_COMMAND:-}
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid release SHA" >&2; exit 2; }

exec 9>"$base/deploy.lock"
flock 9
release=$(mktemp -d "$base/releases/$sha.XXXXXX")
previous=$(readlink -f "$base/current" || true)

compose() {
    local directory=$1
    shift
    DEPLOY_SHA=$(<"$directory/revision") docker compose --project-name ainew-xwp \
        --project-directory "$directory" --env-file "$base/.env" \
        -f "$directory/docker-compose.yml" -f "$directory/deploy/compose.xwp.yml" "$@"
}

rollback() {
    local status=$?
    if [[ -n $previous && -f $previous/revision ]]; then
        compose "$previous" up -d --no-build --wait --wait-timeout 150 || true
    else
        compose "$release" stop || true
    fi
    exit "$status"
}

tar -xf - -C "$release"
printf '%s\n' "$sha" > "$release/revision"
ln -s "$base/backend" "$release/backend"
for variable in AI_NEW_JWT_SECRET AGENT_INTERNAL_TOKEN SEED_ADMIN_PASSWORD SEED_CUSTOMER_PASSWORD; do
    grep -Eq "^${variable}=[a-f0-9]{32,}$" "$base/.env" || {
        echo "Missing deployment setting: $variable" >&2
        exit 2
    }
done
compose "$release" config --quiet
docker load -i "$release/images.tar.gz"
rm "$release/images.tar.gz"

trap rollback ERR
compose "$release" up -d --no-build --wait --wait-timeout 180
health=$(curl -fsS --max-time 8 http://127.0.0.1:2080/health)
printf '%s' "$health" | python3 -c '
import json, sys
health = json.load(sys.stdin)
assert health.get("status") == "ok", health
assert all(health.get("services", {}).get(s) == "ok" for s in ("business", "retrieval")), health
'
ln -sfn "$release" "$base/.current-next"
mv -Tf "$base/.current-next" "$base/current"
trap - ERR
printf 'Active release %s\n' "$sha"

# Keep the active revision and its immediate predecessor for rollback.
previous_sha=
if [[ -n $previous && -f $previous/revision ]]; then
    previous_sha=$(<"$previous/revision")
fi
for component in backend business retrieval gateway; do
    repository="ainew-xwp-$component"
    while IFS= read -r tag; do
        if [[ $tag =~ ^[0-9a-f]{40}$ && $tag != "$sha" && $tag != "$previous_sha" ]]; then
            docker image rm "$repository:$tag" >/dev/null || true
        fi
    done < <(docker image ls "$repository" --format '{{.Tag}}')
done
find "$base/releases" -mindepth 1 -maxdepth 1 -type d -name '????????????????????????????????????????.*' \
    ! -path "$release" ! -path "${previous:-$release}" -exec rm -rf -- {} +
