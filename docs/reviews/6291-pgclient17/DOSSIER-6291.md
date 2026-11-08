# #6291 — postgresql-client-17 (PGDG) sur AG1 et AG2 : dossier (AUCUN geste)

Auteur : bureau (BG, claude), 08/11 08:1xZ. Demande mesh-design #200442 (cause auditeur #200436 : pg_dump 16 sur les composeurs, base de test PG17 → tests sautés).

## Relevé (lecture seule)
- AG1, AG2 : Ubuntu 24.04 noble ; `pg_dump` 16.15 (Ubuntu) ; paquets `postgresql-client-16`, `postgresql-client`, `postgresql-client-common` ; **aucune source PGDG**, pas de `postgresql-common`.
- AG3 (modèle) : source `/etc/apt/sources.list.d/pgdg.sources` (deb, `https://apt.postgresql.org/pub/repos/apt`, `noble-pgdg`, `main`, Signed-By fichier .asc de postgresql-common) ; `postgresql-client-17` 17.11-1.pgdg24.04+2 installé le 01/11 ; `pg_dump --version` = 17.11.
- Clé PGDG copiée d'AG3, empreinte vérifiée = **B97B 0AFC AA1A 47F0 44F2 44A0 7FCC 7D46 ACCC 4CF8** (« PostgreSQL Debian Repository »).

## Geste (`pose-pgclient17.sh --apply`, depuis BG ; à blanc : préalables OK sur AG1/AG2)
Par cible : empreinte revérifiée sur l'hôte ; clé `/etc/apt/keyrings/pgdg.asc` (root 0644) ; source `/etc/apt/sources.list.d/pgdg.sources` (même forme qu'AG3, Signed-By ce fichier) ; `apt-get update` ; `apt-get install --no-install-recommends postgresql-client-17` seul ; relevé des paquets avant/après (diff affiché) ; contrôles : client 16 toujours installé, **aucun serveur postgresql-17**, versions de `pg_dump` (wrapper) et de `/usr/lib/postgresql/17/bin/pg_dump`.
Effet attendu : le wrapper `pg_dump` choisit la version la plus haute → 17 par défaut, comme sur AG3 (pg_dump 17 sait vider des serveurs plus anciens).

## Retour (`retour-pgclient17.sh`)
`apt-get remove postgresql-client-17`, source et clé mises de côté, `apt-get update` ; client 16 intact.

## Suite
Report dans les fiches d'hôte (lot de fiche après le geste). coverage/pyyaml : hors bureau (épingles du runtime, #6291).
