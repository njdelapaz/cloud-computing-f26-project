#!/bin/bash
# Start MariaDB (needed by slurmdbd), then run the given command as user "slurm".
set -e
mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld
(mysqld_safe --bind-address=127.0.0.1 >/tmp/mysqld.log 2>&1 &)
for i in $(seq 1 30); do mysqladmin ping >/dev/null 2>&1 && break; sleep 1; done
exec setpriv --reuid=1000 --regid=1000 --init-groups env HOME=/home/slurm USER=slurm "$@"
