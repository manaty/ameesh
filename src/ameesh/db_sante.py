# SPDX-License-Identifier: AGPL-3.0-only
"""#6862 (EP8.5) — les alertes du collecteur de santé de la grappe, sans la base.

`deploy/serveur/patroni/sante-base.py` (#6870), lancé chaque minute par
`ameesh-db-sante.timer` sur chaque nœud de la grappe, écrit
`/run/ameesh-db/alertes.json` : `db_no_primary`, `db_failover`,
`db_replication_lag`, `db_etcd_no_quorum`, `db_disk_low`, `db_archive_stale`.
Ces alertes doivent partir quand la base n'a plus de primaire, et PC éteint :
la passe ordinaire du notifieur, qui lit la base, ne le peut pas. Ce module
est donc une source du **passage local** (`Notifier.run_local_pass`), comme la
console du pool (#532) : il lit le fichier de l'hôte, sans connexion.

- `db_replication_lag` n'est pas reprise : `db_status` (#6871) la calcule
  déjà sur le primaire, dans la passe ordinaire, et un même type ne peut pas
  avoir deux propriétaires (local et ordinaire).
- Pas de répertoire `/run/ameesh-db` : l'hôte n'est pas un nœud de la grappe,
  aucune alerte (observation vide, les alertes locales de ce type se résolvent).
- Répertoire présent mais fichier absent (démarrage, avant la première sonde),
  fichier illisible ou document invalide : non concluant, les alertes actives
  sont gardées.
- Document plus vieux que `STALE_S` : non concluant aussi, et
  `db_health_stale` le dit — un collecteur arrêté ne doit pas faire taire la
  grappe.
"""
from __future__ import annotations

import json
import os
import time

#: chemin écrit par `ameesh-db-sante.service` (RuntimeDirectory=ameesh-db)
DEFAULT_PATH = "/run/ameesh-db/alertes.json"
SCHEMA = "ameesh-db-sante/1"
#: la minuterie tourne chaque minute ; cinq minutes sans document = arrêtée
STALE_S = 300.0
#: types repris du collecteur (pas `db_replication_lag`, voir plus haut)
FORWARDED = ("db_no_primary", "db_failover", "db_etcd_no_quorum",
             "db_disk_low", "db_archive_stale")
STALE_TYPE = "db_health_stale"
TYPES = FORWARDED + (STALE_TYPE,)
#: champs du collecteur recopiés tels quels (aucun ne porte de secret)
_KEPT = ("value", "threshold", "detail", "severity", "member", "previous",
         "percent", "failed_at")
_DETAIL_MAX = 300


def path_of(cfg) -> str:
    return str(getattr(cfg, "db_health_file", "") or DEFAULT_PATH)


def _short(value) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _DETAIL_MAX else text[:_DETAIL_MAX - 1] + "…"


def _make(kind: str, host, cluster: str, since, **fields) -> dict:
    from .exploitation import _alert
    detail = fields.pop("detail", "")
    return _alert(kind, None, since, fields.pop("value", None),
                  fields.pop("threshold", None), _short(detail),
                  host=host, observation_host=host, cluster=cluster, **fields)


def local(cfg, now: float | None = None) -> tuple[list[dict], list[str]]:
    """(alertes, erreurs) du collecteur de cet hôte.

    Une erreur rend la lecture non concluante : le passage local garde les
    alertes `FORWARDED` actives. `db_health_stale` fait partie des alertes
    rendues quand le document est périmé ; elle se résout à la première
    lecture fraîche."""
    now = float(time.time() if now is None else now)
    host = getattr(cfg, "host", None)
    path = path_of(cfg)
    if not os.path.isdir(os.path.dirname(path) or "."):
        return [], []
    try:
        with open(path, encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError:
        return [], ["%s absent (collecteur pas encore passé)" % path]
    except (OSError, ValueError) as exc:
        return [], ["%s illisible (%s)" % (path, type(exc).__name__)]
    if not isinstance(document, dict) or document.get("schema") != SCHEMA \
            or not isinstance(document.get("alertes"), list):
        return [], ["%s : document %s attendu" % (path, SCHEMA)]
    cluster = str(document.get("grappe") or "ameesh")
    generated = document.get("genere")
    if isinstance(generated, bool) or not isinstance(generated, (int, float)):
        return [], ["%s : horodatage illisible" % path]
    age = now - float(generated)
    if age > STALE_S:
        stale = _make(STALE_TYPE, host, cluster, generated, value=round(age),
                      threshold=STALE_S, severity="warning",
                      detail="collecteur de santé de la grappe muet depuis %d s "
                             "(%s) : vérifier ameesh-db-sante.timer sur %s"
                             % (age, path, host or "cet hôte"))
        return [stale], ["%s périmé (%d s)" % (path, age)]
    alerts = []
    for item in document["alertes"]:
        if not isinstance(item, dict) or item.get("type") not in FORWARDED:
            continue
        fields = {k: item[k] for k in _KEPT if item.get(k) is not None}
        alerts.append(_make(item["type"], host, cluster, item.get("since"),
                            health_key=str(item.get("key") or item["type"]),
                            **fields))
    return alerts, []
