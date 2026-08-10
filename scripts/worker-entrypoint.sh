#!/bin/sh
set -eu

cache_dir=/var/cache/personaforge/huggingface
lam_cache_dir=/var/cache/personaforge/lam
state_dir="${AVATAR_WORKER_STATE_DIR:-/var/lib/personaforge/avatar-worker}"
runtime_dir=/run/personaforge/worker-master
cache_uid="$(id -u personaforge)"
cache_gid="$(id -g personaforge)"
permission_marker="$cache_dir/.personaforge-permissions-${cache_uid}-${cache_gid}"
lam_permission_marker="$lam_cache_dir/.personaforge-permissions-${cache_uid}-${cache_gid}"

mkdir -p "$cache_dir" "$lam_cache_dir" "$state_dir" "$runtime_dir"
if [ ! -f "$permission_marker" ]; then
    chown -R personaforge:personaforge "$cache_dir"
    touch "$permission_marker"
    chown personaforge:personaforge "$permission_marker"
fi
if [ ! -f "$lam_permission_marker" ]; then
    chown -R personaforge:personaforge "$lam_cache_dir"
    touch "$lam_permission_marker"
    chown personaforge:personaforge "$lam_permission_marker"
fi

# Named volumes preserve numeric ownership across image rebuilds. Reconcile the
# small identity/runtime trees before dropping privileges so a WORKER_UID/GID
# change never makes a previously enrolled worker unreadable.
chown -R personaforge:personaforge "$state_dir" "$runtime_dir"

exec gosu personaforge "$@"
