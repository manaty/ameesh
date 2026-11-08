#!/bin/bash
# Retour #6291 : retire le client 17 et la source PGDG sur AG1/AG2 (client 16 intact).
set -euo pipefail
for h in 78.232.56.22 78.232.60.1; do ssh -F none -o BatchMode=yes "root@$h" 'set -eu; DEBIAN_FRONTEND=noninteractive apt-get remove -y -q postgresql-client-17; mv /etc/apt/sources.list.d/pgdg.sources /root/pgdg.sources.retire; mv /etc/apt/keyrings/pgdg.asc /root/pgdg.asc.retire; apt-get update -qq; pg_dump --version'; done
