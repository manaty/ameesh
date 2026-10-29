# SPDX-License-Identifier: AGPL-3.0-only
"""#4609 : `mesh_backup_unseen`, la lecture complète quotidienne de la base du mesh.

Sous PostgreSQL 17, `pg_stat_user_tables.last_seq_scan` donne l'instant du
dernier parcours séquentiel de chaque table. Le `pg_dump` quotidien de la base
du mesh (minuterie `ameesh-sauvegarde`, 03:30 + jusqu'à 15 min d'aléa) lit
toutes les tables dans une seule transaction : les tables qu'il a lues portent
le même instant, à la microseconde. Cette alerte cherche ce groupe dans la
fenêtre de sauvegarde et alerte quand il n'existe pas.

Deux précautions, apprises du relevé du 29/10 (#4300) :

* les tables relues ensuite par l'activité normale **perdent** cet instant et
  sortent de la fenêtre : le seuil porte donc sur les tables dont le
  `last_seq_scan` est encore dans la fenêtre, jamais sur toutes les tables
  (au 29/10 à 16:2xZ, 150 tables portaient encore l'instant du `pg_dump` et
  121 avaient été relues ; compter ces dernières contre la sauvegarde
  produirait une fausse alerte chaque après-midi). Le nombre total de tables
  est reporté dans la ligne, pour que le lecteur voie la couverture ;
* la fenêtre évaluée est la **dernière fenêtre close** : avant 04:30Z, c'est
  celle de la veille, pour qu'une sauvegarde manquante ne disparaisse pas à
  minuit et ne se relève pas chaque matin.

Ce que l'alerte ne prouve pas : l'écriture du fichier de `pg_dump`, ni son
envoi vers S3 (ligne de statut de `sauvegarder-base.sh`, #4300). Le détail de
l'alerte le dit, avec la commande de vérification.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

#: Fenêtre UTC par défaut, en secondes depuis minuit : le minuteur
#: `ameesh-sauvegarde.timer` part à 03:30 avec jusqu'à 15 min d'aléa, et la
#: fenêtre se referme une heure plus tard. Réglable (`mesh_backup_window`).
DEFAULT_WINDOW = (3 * 3600 + 30 * 60, 4 * 3600 + 30 * 60)
#: Part minimale des tables de la fenêtre qui doivent partager un même
#: instant. 0 désactive l'alerte (`mesh_backup_min_share`).
DEFAULT_MIN_SHARE = 0.9
SECONDS_PER_DAY = 86400
MICROSECONDS = 1000000


def window_bounds(now, window=DEFAULT_WINDOW):
    """La dernière fenêtre de sauvegarde close avant `now`, en microsecondes.

    La fenêtre est quotidienne en UTC (`HH:MM` de `mesh_backup_window`) ; avant
    sa fin, celle de la veille est la dernière complète, et c'est elle qu'on
    juge : une sauvegarde manquante la veille ne se tait pas à minuit.
    """
    try:
        start_s, end_s = int(window[0]), int(window[1])
    except (TypeError, ValueError, IndexError):
        start_s, end_s = DEFAULT_WINDOW
    if end_s <= start_s:
        start_s, end_s = DEFAULT_WINDOW
    day = math.floor(float(now) / SECONDS_PER_DAY) * SECONDS_PER_DAY
    start = day + start_s
    end = day + end_s
    if float(now) < end:
        start -= SECONDS_PER_DAY
        end -= SECONDS_PER_DAY
    return start * MICROSECONDS, end * MICROSECONDS


def project(rows, now, *, window=DEFAULT_WINDOW, min_share=DEFAULT_MIN_SHARE):
    """Évalue la signature du `pg_dump` sur les statistiques simulées ou lues.

    `rows` : `[{"relname": str, "last_seq_scan_us": int | None}]`. Rend un
    dictionnaire stable (fenêtre, nombre total de tables, tables encore dans
    la fenêtre, plus grand groupe au même instant, part, verdict `ok`).
    """
    start_us, end_us = window_bounds(now, window)
    instants: Counter = Counter()
    total = 0
    for row in rows or ():
        total += 1
        value = row.get("last_seq_scan_us")
        if value is None:
            continue
        value = int(value)
        if start_us <= value < end_us:
            instants[value] += 1
    in_window = sum(instants.values())
    shared_us, shared = instants.most_common(1)[0] if instants else (None, 0)
    share = shared / in_window if in_window else 0.0
    return {
        "window_start": start_us / MICROSECONDS,
        "window_end": end_us / MICROSECONDS,
        "tables": total,
        "in_window": in_window,
        "shared": shared,
        "share": share,
        "instant_us": shared_us,
        "ok": in_window > 0 and share >= float(min_share),
    }


def read(db):
    """Les statistiques de la base, lues dans le module de stockage."""
    from .storage.postgres import mesh_backup

    return mesh_backup.read(db)


def alert_detail(result, min_share):
    """Le texte de l'alerte : constat, commande de vérification, limites."""
    start = _stamp(result["window_start"])
    end = _stamp(result["window_end"])
    if result["in_window"]:
        seen = (
            "the largest group is %d of the %d tables still showing a sequential "
            "scan inside %s..%s, all at %s (%.0f %% against a %.0f %% floor)"
            % (result["shared"], result["in_window"], start, end,
               _instant(result["instant_us"]), 100 * result["share"],
               100 * float(min_share)))
    else:
        seen = "no table still shows a sequential scan inside %s..%s" % (start, end)
    return (
        "mesh database backup not seen: %s (%d tables in pg_stat_user_tables). "
        "The daily pg_dump reads every table in one transaction, so the tables it "
        "read share last_seq_scan to the microsecond; tables re-read later by "
        "normal activity lose that instant and are not counted as failures. "
        "Check: psql \"$AMEESH_DSN\" -Atc \"SELECT last_seq_scan, count(*) FROM "
        "pg_stat_user_tables GROUP BY 1 ORDER BY 2 DESC LIMIT 5\" (the mesh DSN "
        "is `dsn` in the ameesh config). Statistics reset at server start: check "
        "`pg_postmaster_start_time()` too. This proves the read only, not that the "
        "dump file was written nor that it was copied to S3 (#4300)."
        % (seen, result["tables"]))


def alerts(db, now, *, window=DEFAULT_WINDOW, min_share=DEFAULT_MIN_SHARE):
    """L'alerte `mesh_backup_unseen` du jour, ou une liste vide."""
    floor = float(min_share)
    if floor <= 0:                  # réglage explicite : alerte désactivée
        return []
    rows = read(db)
    if rows is None:                # serveur sans `last_seq_scan` : pas de preuve
        return []
    result = project(rows, now, window=window, min_share=floor)
    if result["ok"]:
        return []
    from .exploitation import _alert

    return [_alert(
        "mesh_backup_unseen", None, result["window_start"],
        round(result["share"], 3), floor, alert_detail(result, floor),
        window_start=result["window_start"], window_end=result["window_end"],
        tables=result["tables"], in_window=result["in_window"],
        shared=result["shared"],
        instant=(_instant(result["instant_us"])
                 if result["instant_us"] is not None else None))]


def _stamp(seconds):
    return datetime.fromtimestamp(float(seconds), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _instant(microseconds):
    """Un instant en microsecondes, sans perdre le dernier chiffre."""
    seconds, micros = divmod(int(microseconds), MICROSECONDS)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S") + ".%06dZ" % micros
