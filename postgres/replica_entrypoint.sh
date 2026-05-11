#!/bin/sh
set -e

if [ ! -f "$PGDATA/PG_VERSION" ]; then
    echo "Replica not initialized. Running pg_basebackup from primary..."
    until pg_isready -h postgres -U replicator; do
        echo "Waiting for primary to be ready..."
        sleep 2
    done
    pg_basebackup -h postgres -U replicator -D "$PGDATA" -Xs -R --checkpoint=fast -P
    chmod 700 "$PGDATA"
    echo "pg_basebackup complete. Replica initialized."
fi

exec postgres "$@"
