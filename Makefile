# SPDX-License-Identifier: AGPL-3.0-only
# agent-mesh — raccourcis du dépôt (rien n'est installé dans ~/.local/bin).
PYTHON ?= python3
export PYTHONPATH := src$(if $(PYTHONPATH),:$(PYTHONPATH))
#: DSN du banc local, sans mot de passe : PGPASSWORD ou ~/.pgpass fournit le secret.
export AMEESH_DSN ?= postgresql://agent_mesh@127.0.0.1:55432/agent_mesh

.PHONY: help pg-up migrate doctor test list mesh-list keys work clean-schemas

help:
	@sed -n '1,20p' Makefile

pg-up:            ## démarre le conteneur Postgres local (agent-mesh-pg)
	./scripts/pg-up.sh

migrate: pg-up     ## applique les migrations versionnées
	$(PYTHON) -m ameesh migrate

doctor:            ## diagnostic complet (pilote, schéma, LISTEN/NOTIFY)
	$(PYTHON) -m ameesh doctor --notify-test

test:              ## suite complète, deux pilotes (psql puis psycopg)
	./scripts/test.sh

list:              ## agents connus, hôte, bail, non lus (vue v0-compatible)
	$(PYTHON) -m ameesh list

mesh-list:         ## mesh list : statut, bail, budget, clé
	$(PYTHON) -m ameesh list

keys:              ## clés publiques enregistrées
	$(PYTHON) -m ameesh key list

work:              ## lots en cours
	$(PYTHON) -m ameesh work list

clean-schemas:     ## supprime les schémas de test restants (t_*)
	@psql "$(AMEESH_DSN)" -X -A -t -c \
	  "select 'drop schema if exists \"' || nspname || '\" cascade;' from pg_namespace \
	   where nspname like 't\_%' or nspname like 'bench%' or nspname like 'smoke%' \
	   or nspname like 'iso\_%' or nspname = 'psycopg_dbg'" | psql "$(AMEESH_DSN)" -X -q
	@echo "schémas de test supprimés"
