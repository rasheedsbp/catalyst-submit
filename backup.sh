#!/usr/bin/env bash
# Nightly backup of captured leads. crontab: 0 3 * * * /opt/journal-formatter/backup.sh
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M)
sqlite3 data/leads.db ".backup 'backups/leads-$STAMP.db'"
gzip -f "backups/leads-$STAMP.db"
find backups -name '*.db.gz' -mtime +30 -delete
