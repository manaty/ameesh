# SPDX-License-Identifier: AGPL-3.0-only
"""#4609 : `mesh_backup_unseen`, la lecture complète quotidienne de la base du mesh.

Sous PostgreSQL 16, `pg_stat_user_tables.last_seq_scan` donne l'instant du
dernier parcours séquentiel de chaque table. Le `pg_dump` quotidien de la base
du mesh (minuterie `ameesh-sauvegarde`, 03:30 + jusqu'à 15 min d'aléa) lit
toutes les tables dans une seule transaction : les tables qu'il a lues portent
le même instant, à la microseconde. Cette alerte cherche ce groupe dans la
fenêtre de sauvegarde et alerte quand il n'existe pas.

Deux précautions, apprises du relevé du 29/10 (#4300) :

* les tables relues ensuite par l'activité normale **avancent** leur instant :
  le critère ne compte donc pas les tables encore dans la fenêtre, mais
  demande qu'un groupe de tables partage un même instant T entre le début de
  la fenêtre et `now` (preuve d'une transaction) et qu'au moins 90 % des
  tables lisibles aient un `last_seq_scan` **au moins égal** à T (elles ont pu
  être lues par le dump, puis relues plus tard). Compter les tables relues
  après le dump contre la sauvegarde produisait une fausse alerte chaque
  matin vers 04:30 (#4609 B ; #4618 : 150 tables du dump + 20 relues avant
  04:30 dans la fenêtre = 88 % sous l'ancien dénominateur) ; un plancher de
  groupe écarte la petite transaction ordinaire qui, seule, donnerait 100 %
  (faux vert) ;
* la fenêtre évaluée est la **dernière fenêtre close** : avant 04:30Z, c'est
  celle de la veille, pour qu'une sauvegarde manquante ne disparaisse pas à
  minuit et ne se relève pas chaque matin. Un groupe plus récent, jusqu'à
  `now`, est accepté : le dump du jour (vers 03:37Z) a déjà avancé toutes les
  tables hors de la fenêtre de la veille, et il prouve au moins autant.

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
#: Part minimale des tables lisibles qui doivent avoir été lues au moins à
#: l'instant de la transaction. 0 désactive l'alerte (`mesh_backup_min_share`).
DEFAULT_MIN_SHARE = 0.9
#: Part minimale des tables lisibles qu'une même transaction doit avoir lues
#: pour compter comme signature de `pg_dump` (`mesh_backup_min_group_share`).
#: Un groupe de 2-3 tables peut être une transaction ordinaire, pas une
#: sauvegarde : sans plancher, il donnerait 100 % et un faux vert.
DEFAULT_MIN_GROUP_SHARE = 0.05
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


def project(rows, now, *, window=DEFAULT_WINDOW, min_share=DEFAULT_MIN_SHARE,
            min_group_share=DEFAULT_MIN_GROUP_SHARE):
    """Évalue la signature du `pg_dump` sur les statistiques simulées ou lues.

    `rows` : `[{"relname": str, "last_seq_scan_us": int | None}]`. Rend un
    dictionnaire stable (fenêtre, tables lisibles, tables encore dans la
    fenêtre, groupe et part, verdict `ok`).

    Le `pg_dump` lit toutes les tables dans une seule transaction : après lui,
    chaque table a un `last_seq_scan` **au moins égal** à l'instant T du dump ;
    une table relue plus tard n'a fait que avancer. Le critère est donc :
    il existe T entre le début de la fenêtre jugée et `now`, porté par un
    groupe d'au moins `min_group_share` des tables lisibles, tel qu'au moins
    `min_share` des tables aient un `last_seq_scan >= T`. Les tables relues
    après T ne diluent plus la part (l'ancien dénominateur « tables encore
    dans la fenêtre » produisait une fausse alerte à 04:30, #4609 B de
    relecture) ; le plancher de groupe écarte une petite transaction
    ordinaire, qui donnerait sinon 100 % (faux vert).

    T peut dépasser la fin de la fenêtre jugée : avant 04:30Z, la fenêtre
    jugée est celle de la veille, et le dump du jour (vers 03:37Z) a déjà
    avancé toutes les tables hors de cette fenêtre. Un dump plus récent prouve
    au moins autant ; sans cela, l'alerte se lèverait chaque matin de 03:35 à
    04:30Z (B/P2 de l'auditeur, 29/10).

    Une table jamais scannée (`last_seq_scan` NULL, par exemple après une
    remise à zéro des statistiques) compte dans `tables` et jamais dans
    `covered` : elle ne peut pas provenir du dump, et un petit groupe de
    tables scannées ne doit pas verdir la base (B/P2 de relecture).
    """
    start_us, end_us = window_bounds(now, window)
    now_us = int(float(now) * MICROSECONDS)
    instants: Counter = Counter()
    values = []
    in_window = 0
    total = 0
    for row in rows or ():
        total += 1
        value = row.get("last_seq_scan_us")
        if value is None:
            continue
        value = int(value)
        values.append(value)
        if start_us <= value <= now_us:
            instants[value] += 1
        if start_us <= value < end_us:
            in_window += 1
    never_scanned = total - len(values)
    # plancher du groupe : au moins 2 tables, et `min_group_share` des tables
    # éligibles ; jamais plus que le nombre de tables (petite base).
    floor = min(total, max(2, math.ceil(float(min_group_share) * total))) if total else 0
    share_floor = float(min_share)
    best = None       # groupe au-dessus du plancher, couverture la plus haute
    fallback = None   # plus grand groupe, même sous le plancher (pour le detail)
    for instant, group in instants.items():
        covered = sum(1 for value in values if value >= instant)
        if group >= floor:
            if best is None or (covered, group) > (best[0], best[1]):
                best = (covered, group, instant)
        if fallback is None or (group, instant) > (fallback[1], fallback[2]):
            fallback = (covered, group, instant)
    if best is not None and best[0] >= share_floor * total:
        covered, group, instant = best
        ok = True
    elif best is not None:
        covered, group, instant = best
        ok = False
    elif fallback is not None:
        covered, group, instant = fallback
        ok = False
    else:
        covered, group, instant = 0, 0, None
        ok = False
    share = covered / total if total else 0.0
    return {
        "window_start": start_us / MICROSECONDS,
        "window_end": end_us / MICROSECONDS,
        "tables": total,
        "never_scanned": never_scanned,
        "in_window": in_window,
        "shared": group,
        "covered": covered,
        "min_group": floor,
        "share": share,
        "instant_us": instant,
        "ok": ok,
    }


def read(db):
    """Les statistiques de la base, lues dans le module de stockage."""
    from .storage.postgres import mesh_backup

    return mesh_backup.read(db)


def alert_detail(result, min_share, min_group_share=DEFAULT_MIN_GROUP_SHARE):
    """Le texte de l'alerte : constat, commande de vérification, limites."""
    start = _stamp(result["window_start"])
    end = _stamp(result["window_end"])
    if result["instant_us"] is None:
        seen = ("no table still shows a sequential scan inside %s..%s "
                "(nor after it up to now)" % (start, end))
    elif result["instant_us"] >= result["window_end"] * MICROSECONDS:
        seen = (
            "the largest candidate after the judged window is at %s: %d of the "
            "%d tables, covering %d of them (%.0f %% against a %.0f %% "
            "floor; group floor %d); it does not prove the judged window"
            % (_instant(result["instant_us"]), result["shared"],
               result["tables"], result["covered"], 100 * result["share"],
               100 * float(min_share), result["min_group"]))
    elif result["shared"] < result["min_group"]:
        seen = (
            "the largest group inside %s..%s is %d of the %d tables, "
            "all at %s, below the %d-table floor for a full-dump transaction"
            % (start, end, result["shared"], result["tables"],
               _instant(result["instant_us"]), result["min_group"]))
    else:
        seen = (
            "the largest transaction inside %s..%s read %d tables at %s; only "
            "%d of the %d tables have last_seq_scan at or after it "
            "(%.0f %% against a %.0f %% floor)"
            % (start, end, result["shared"], _instant(result["instant_us"]),
               result["covered"], result["tables"], 100 * result["share"],
               100 * float(min_share)))
    return (
        "mesh database backup not seen: %s (%d tables in pg_stat_user_tables, "
        "%d never scanned and counted as not read). The daily pg_dump reads every "
        "table in one transaction, so after it every table's last_seq_scan is at "
        "or after the dump instant; tables re-read later only move forward. A "
        "group of tables sharing one microsecond instant marks that transaction; "
        "the group floor rejects a small ordinary transaction. "
        "Check: psql \"$AMEESH_DSN\" -Atc \"SELECT last_seq_scan, count(*) FROM "
        "pg_stat_user_tables GROUP BY 1 ORDER BY 2 DESC LIMIT 5\" (the mesh DSN "
        "is `dsn` in the ameesh config). Statistics reset at server start: check "
        "`pg_postmaster_start_time()` too. This proves the read only, not that the "
        "dump file was written nor that it was copied to S3 (#4300)."
        % (seen, result["tables"], result["never_scanned"]))


def alerts(db, now, *, window=DEFAULT_WINDOW, min_share=DEFAULT_MIN_SHARE,
           min_group_share=DEFAULT_MIN_GROUP_SHARE):
    """L'alerte `mesh_backup_unseen` du jour, ou une liste vide."""
    floor = float(min_share)
    if floor <= 0:                  # réglage explicite : alerte désactivée
        return []
    rows = read(db)
    if rows is None:                # serveur sans `last_seq_scan` : pas de preuve
        return []
    result = project(rows, now, window=window, min_share=floor,
                     min_group_share=min_group_share)
    if result["ok"]:
        return []
    from .exploitation import _alert

    return [_alert(
        "mesh_backup_unseen", None, result["window_start"],
        round(result["share"], 3), floor,
        alert_detail(result, floor, min_group_share),
        window_start=result["window_start"], window_end=result["window_end"],
        tables=result["tables"], in_window=result["in_window"],
        never_scanned=result["never_scanned"],
        shared=result["shared"], covered=result["covered"],
        min_group=result["min_group"],
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
