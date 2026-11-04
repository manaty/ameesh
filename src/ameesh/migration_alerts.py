# SPDX-License-Identifier: AGPL-3.0-only
"""#5692 — une migration non additive en attente, signalée par base configurée.

`migrate --additive-only` refuse une migration que le code d'avant ne
survivrait pas : l'appliquer exige les exécuteurs arrêtés (fenêtre, décision
owner). Rien ne le disait avant le staging : `doctor` n'affichait qu'un
compteur, et la base secondaire de l'hôte (`config-ancienne-base.json`) n'était
sondée par aucune alerte. Ce module produit une alerte par base configurée :

* `migration_pending_non_additive` : la base à laquelle ce notifieur est
  connecté (base du mesh), poussée par la passe ordinaire, une fois pour le
  maillage ;
* `host_migration_pending_non_additive` : les autres configurations de l'hôte,
  lues par le passage local du notifieur. Le type est local (comme la console
  du pool, #532) : un hôte qui n'a pas ce fichier ne peut pas résoudre
  l'alerte d'un autre, et `_local_entry_owned` la garde sur `host`.

Lecture seule partout : aucune migration n'est appliquée, aucune table n'est
créée (`migrations.status`), la détection ne change donc rien à la base.
"""
from __future__ import annotations

import dataclasses
import os
import time

from . import config as config_mod
from . import db as db_mod
from . import migrations

#: une base secondaire injoignable ne retient pas le passage local du
#: notifieur plus longtemps que cela (le passage ordinaire reste à 30 s)
PROBE_TIMEOUT_S = 5.0

#: la base du mesh, vue par la passe ordinaire (globale, une par maillage)
TYPE = "migration_pending_non_additive"
#: une autre base configurée de l'hôte (locale, gardée par `host`)
HOST_TYPE = "host_migration_pending_non_additive"


def _since(items) -> float:
    """Début d'épisode stable entre deux passages : la plus ancienne migration
    en attente. Le fichier d'un `src-<sha>` déployé (git archive) garde une
    date stable ; sans elle, l'instant courant (l'alerte reste alors nouvelle
    à chaque passage, jamais silencieuse)."""
    stamps = []
    for migration, _reasons in items:
        try:
            stamps.append(os.path.getmtime(migration.path))
        except OSError:
            pass
    return min(stamps) if stamps else time.time()


def _base_label(cfg) -> str:
    """Identité affichable et stable d'une base : DSN masqué + schéma."""
    return "%s (schema %s)" % (config_mod.mask_dsn(cfg.dsn), cfg.schema or "public")


def _rows(items) -> list[dict]:
    return [{"version": migration.version, "label": migration.label,
             "reasons": list(reasons)}
            for migration, reasons in items]


def _make(kind: str, cfg, base: str, items, *, label: str | None = None) -> dict:
    from .exploitation import _alert
    where = ("%s — %s" % (label, base)) if label else base
    detail = (
        "%d pending non-additive migration(s) on %s: %s. `migrate "
        "--additive-only` refuses them: the previous code would break on the "
        "migrated base. Plan the migration window (executors stopped), or "
        "defer this host at the next staging; applying them is a separate "
        "reviewed lot (#4792)."
        % (len(items), where, migrations.describe_pending_non_additive(items)))
    return _alert(kind, None, _since(items), len(items), 0, detail,
                  base=base, host=getattr(cfg, "host", None),
                  observation_host=getattr(cfg, "host", None),
                  config=label, migrations=_rows(items))


def primary(cfg, db) -> list[dict]:
    """L'alerte de la base du mesh, si des migrations non additives manquent."""
    if getattr(getattr(db, "cfg", None), "backend", "") == "mediated":
        return []  # pas de base SQL : le serveur tient son propre inventaire
    items = migrations.pending_non_additive(db)
    if not items:
        return []
    return [_make(TYPE, cfg, _base_label(cfg), items)]


def local(cfg) -> tuple[list[dict], list[str]]:
    """(alertes, erreurs) des autres bases configurées de l'hôte.

    Une base illisible n'est jamais une résolution : son nom part dans
    `erreurs`, que le passage local journalise et traite comme non concluant
    (les alertes locales encore actives sont gardées)."""
    alerts: list[dict] = []
    errors: list[str] = []
    for label, path in config_mod.secondary_configs(cfg):
        try:
            other = config_mod.load_file(path)
            if other.connect_timeout > PROBE_TIMEOUT_S:
                other = dataclasses.replace(other, connect_timeout=PROBE_TIMEOUT_S)
            other_db = db_mod.connect(other, writer=False)
        except (Exception, SystemExit) as exc:  # base absente/refusée : dite, gardée
            errors.append("%s: %s" % (label, " ".join(str(exc).split())[:120]))
            continue
        try:
            items = migrations.pending_non_additive(other_db)
        except (Exception, SystemExit) as exc:
            errors.append("%s: %s" % (label, " ".join(str(exc).split())[:120]))
            continue
        finally:
            other_db.close()
        if items:
            alerts.append(_make(HOST_TYPE, cfg, _base_label(other), items, label=label))
    return alerts, errors
