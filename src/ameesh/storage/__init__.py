# SPDX-License-Identifier: AGPL-3.0-only
"""Interface de stockage d'ameesh (spec §10, C1 ; décision 0016).

Les modules métier n'écrivent plus de SQL : ils appellent des opérations
NOMMÉES, regroupées par domaine, sur l'objet rendu par `of(db)` :

    from . import storage
    storage.of(db).leases.claim(name, owner, ttl, require_responsible=False)

`db` est une connexion de `ameesh.db` (pilote psql ou psycopg) ou une
transaction ouverte (`db.transaction()`) : les opérations s'exécutent alors
dans cette transaction.

Règles du découpage (lot L1, refonte sans changement de comportement) :

* une opération porte son SQL TEL QUEL (déplacé depuis le module métier,
  pas réécrit) ;
* une requête multi-instructions, ou une suite d'instructions qui forme une
  transaction, est UNE opération ;
* verrous (`FOR UPDATE`, `FOR SHARE`, `pg_advisory_xact_lock`), ordre des
  verrous, recontrôles d'échéance après le dernier verrou et hors des WHERE
  verrouillants (motif verrou → recontrôle `clock_timestamp()` → écriture),
  fonctions PL/pgSQL appelées : ceux du SQL déplacé, inchangés ;
* les décisions métier (validation, messages, choix d'une branche) restent
  dans les modules métier ; une opération rend des lignes ou une valeur
  simple.

v1 : un seul pilote, Postgres (`storage.postgres`, qui délègue à `ameesh.db`).
Un pilote SQLite (profil local, décision 0016) implémentera les mêmes classes
abstraites (`storage.interface`) ; `of()` choisira alors selon la connexion.
Les migrations restent des fichiers SQL versionnés propres au pilote. Ce qui
reste hors de l'interface, et la liste de contrôle d'un pilote SQLite
(domaines et opérations, verrous, heure, réveil, schéma, tests), sont dans
la documentation de `storage.interface`.
"""
from __future__ import annotations

from .interface import Storage
from .postgres import PostgresStorage

__all__ = ["Storage", "PostgresStorage", "of"]


def of(db) -> Storage:
    """Le stockage lié à cette connexion (ou à cette transaction ouverte)."""
    return PostgresStorage(db)
