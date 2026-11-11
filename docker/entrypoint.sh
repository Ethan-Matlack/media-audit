#!/bin/sh
# Container entrypoint.
#
# Started as root (the default, and how Unraid runs containers): make /config usable by
# PUID:PGID (Unraid creates appdata folders owned by root), then drop to that user.
# Started with --user / compose `user:`: just run as that user.
set -eu

if [ "$(id -u)" != "0" ]; then
  exec media-audit "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
case "${PUID}${PGID}" in
  '' | *[!0-9]*) echo "error: PUID and PGID must be numeric (got '${PUID}', '${PGID}')" >&2; exit 2 ;;
esac

if [ "$PUID" = "0" ]; then
  echo "warning: PUID=0, running as root" >&2
  exec media-audit "$@"
fi

mkdir -p /config
chown "$PUID:$PGID" /config
# Files left by an earlier run as a different user. Not recursive on purpose.
for f in /config/config.yml /config/*.db /config/*.db-wal /config/*.db-shm /config/*.lock; do
  if [ -e "$f" ]; then chown "$PUID:$PGID" "$f"; fi
done

exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- media-audit "$@"
