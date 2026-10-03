#!/bin/bash
# Nightly SQLite dumps for ravixfs (prod + staging). Keeps 7 days.
# Safe to run any time; exits 0 even if a DB doesn't exist yet.
set -u

BACKUP_DIR="/opt/ravixfs/backups"
mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"

dump_one() {
  local name="$1" db="$2"
  if [ -f "$db" ]; then
    if sqlite3 "$db" .dump 2>/dev/null | gzip > "$BACKUP_DIR/$name-$STAMP.sql.gz"; then
      echo "backed up $db -> $BACKUP_DIR/$name-$STAMP.sql.gz"
    else
      echo "backup FAILED for $db" >&2
      rm -f "$BACKUP_DIR/$name-$STAMP.sql.gz"
    fi
  else
    echo "skip $name: $db not present yet"
  fi
}

dump_one "ravixfs" "/opt/ravixfs/ravixfs.db"
dump_one "ravixfs-staging" "/opt/ravixfs-staging/ravixfs.db"

# prune dumps older than 7 days
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +7 -delete

exit 0
