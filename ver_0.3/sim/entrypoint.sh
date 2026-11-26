#!/bin/bash
# Start MariaDB (needed by slurmdbd), then run the command as the slurm user.
set -e

mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld
(mysqld_safe --user=mysql >/var/log/mysqld_safe.log 2>&1 &)
for i in $(seq 1 60); do
    mysqladmin ping --silent 2>/dev/null && break
    sleep 1
done
mysqladmin ping --silent || { echo "MariaDB did not start" >&2; exit 1; }

exec sudo -E -u slurm \
    PATH="$PATH" PYTHONPATH="$PYTHONPATH" HOME=/home/slurm \
    "$@"
