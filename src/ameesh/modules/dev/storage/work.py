# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §3.2, §3.3, §4.6) : stockage des tables de développement.

Les tables de développement (37, dont la requalification 0592) quittent le noyau : ce paquet détient leur
SQL, et le noyau ne les nomme plus. Les opérations gardent la signature et les
garanties de ``storage.postgres.work`` (mêmes verrous, mêmes transactions) ;
seul l'emplacement du SQL change. Chaque instruction passe par ``DevDomain``,
qui la qualifie avec le nom physique lu dans ``module_schemas`` (voir
``storage.schema``) : la même ligne fonctionne avant P2 sur ``<noyau>.<table>``
et après P2 sur ``<noyau>__dev.<table>``.
"""
from __future__ import annotations

import json
import random
import time

from .... import packet_head, work_lifecycle as lifecycle
from ....db import (DbError, in_transaction, is_unique_violation,
                    prerequisite_query)
from ....slo import VEHICLE_EXIT_MARK
from ....storage import interface
from .schema import DevDomain, ModuleDb, relation

from .work_footprints import (cap_reached_sql as footprint_cap_sql,
                              cap_rows_sql as footprint_cap_rows_sql,
                              held_sql as footprint_held_sql,
                              in_flight_sql as footprint_in_flight_sql,
                              occupying_sql as footprint_occupying_sql,
                              open_packet_members as footprint_open_packet_members,
                              overlap_exists_sql as footprint_overlap_sql,
                              overlap_rows_sql as footprint_overlap_rows_sql,
                              start_blocking as footprint_start_blocking,
                              start_on_sql as footprint_start_on_sql,
                              unknown_policy_sql as footprint_unknown_policy_sql)

#: L160d (0057) : les colonnes d'une ligne de file de fusion. Le fragment
#: nomme `integration_train_queue_receipts`, une table du module : le noyau
#: le reçoit d'ici (P1b #7650) et la qualification le suit.
QUEUE_COLUMNS = """
    q.work_item_id, q.target, q.frozen_sha, q.state, q.rank, q.landing_sha, q.reason,
    q.queued_by, q.source, q.generation, q.frozen_id,
    EXISTS (SELECT 1 FROM integration_train_queue_receipts tr
            WHERE tr.work_item_id = q.work_item_id AND tr.generation = q.generation
              AND tr.kind = 'packet') AS packet_allocated,
    extract(epoch from q.queued_at)::float8 as queued_ts,
    extract(epoch from q.updated_at)::float8 as updated_ts,
    w.title, w.priority, w.state as item_state, w.branch, w.assignee
"""

#: L160d (0057) : les colonnes d'une résolution de conflit préparée. Le
#: fragment nomme `work_resolutions`, une table du module : le noyau le reçoit
#: d'ici (P1b #7650) et la qualification le suit.
RESOLUTION_COLUMNS = """
    work_item_id, other_item_id, commit_sha, lot_tip, other_tip, ref, files, created_by,
    extract(epoch from created_at)::float8 as created_ts
"""


def _requalify_row(row):
    """#7751: a requalification row with its JSON evidence decoded."""
    row = dict(row)
    if isinstance(row.get("reviews"), str):
        row["reviews"] = json.loads(row["reviews"])
    return row


class DevWork(DevDomain):
    """Les opérations de stockage propres au module `dev`."""

    # -- paquet de composition (#2443) ---------------------------------------
    #: les jalons du porteur (#5347) : calculés une fois par porteur distinct,
    #: pas une fois par ligne de membre. Les deux sous-requêtes corrélées
    #: s'exécutaient 2 × 653 fois (porteurs répétés) sur les 1 000 lots de
    #: `sync-merges` ; elles coûtaient à elles seules l'essentiel de la lecture.
    #: Chaque sous-requête garde l'index (work_item_id, at) et son LIMIT 1 :
    #: le plan reste des sondes d'index, indépendant de la taille du tas.
    _PACKET_HEADS = (
        "carrier AS (SELECT DISTINCT package_id FROM member),"
        " heads AS (SELECT c.package_id,"
        "       (SELECT ms.sha FROM work_item_milestones ms"
        "         WHERE ms.work_item_id = c.package_id AND ms.kind = 'merged'"
        "           AND ms.sha <> ''"
        "         ORDER BY ms.at DESC, ms.id DESC LIMIT 1) AS package_merged_sha,"
        # #3634 : la tête gelée du porteur, sans filtre sur son sha : le
        # dernier gel fait autorité, même s'il ne nomme aucun commit.
        # #5470 : avec son instant, dans la même sonde d'index.
        "       f.sha AS package_frozen_sha,"
        "       extract(epoch FROM f.at)::float8 AS package_frozen_ts"
        "   FROM carrier c"
        "   LEFT JOIN LATERAL (SELECT ms.sha, ms.at FROM work_item_milestones ms"
        "         WHERE ms.work_item_id = c.package_id AND ms.kind = 'frozen'"
        "         ORDER BY ms.at DESC, ms.id DESC LIMIT 1) f ON true)"
    )

    #: la lecture commune aux deux méthodes : l'appartenance, le porteur et
    #: ses jalons de fusion et de gel, en une requête (sur `member`/`heads`).
    _PACKET_SELECT = (
        "SELECT m.package_id, m.work_item_id, m.package_branch, m.base_sha,"
        "       m.head_sha, m.unit_sha, m.source_sha, m.frozen_sha,"
        "       m.recorded_by, extract(epoch from m.recorded_at)::float8 AS recorded_ts,"
        "       p.state AS package_state, p.branch AS carrier_branch,"
        "       p.branch_target AS package_target,"
        "       h.package_merged_sha, h.package_frozen_sha, h.package_frozen_ts"
        "  FROM member m JOIN work_items p ON p.id = m.package_id"
        "  LEFT JOIN heads h ON h.package_id = m.package_id"
    )

    _MEASURE_COLUMNS = (
        "work_item_id, extract(epoch from measured_at)::float8 AS measured_ts,"
        " extract(epoch from started_at)::float8 AS started_ts,"
        " extract(epoch from merged_at)::float8 AS merged_ts,"
        " total_minutes::float8 AS total_minutes, agent_minutes::float8 AS agent_minutes,"
        " ci_minutes::float8 AS ci_minutes, review_minutes::float8 AS review_minutes,"
        " human_minutes::float8 AS human_minutes, ci_runs, merge_sha, diff_files,"
        " diff_insertions, diff_deletions, measured_size, declared_size, agent_source")

    def _core(self):
        """Le noyau `storage.postgres.work` (import paresseux : cycle évité)."""
        from ....storage.postgres import work as core
        return core

    def _event(self, db, item_id, state, note, actor, *, evidence=None) -> None:
        """Le journal (`work_item_events`, table noyau) écrit dans `db`.

        Le mouvement et son journal sont atomiques : l'appelant passe la
        transaction du mouvement (`tx`) ou, hors transaction, `self.db`. Le
        SQL reste celui du noyau (`WorkItems._event`), pas une copie.
        """
        self._core().WorkItems(db)._event(item_id, state, note, actor, evidence=evidence)

    def _migration_row(self, tx, item_id):
        rows = tx.query(
            "SELECT %s FROM work_items WHERE id = %%s" % self._core().item_columns(tx),
            (int(item_id),))
        return rows[0] if rows else None

    def slots_available(self, db=None) -> bool:
        """La table des créneaux existe-t-elle (base d'avant 0419 comprise) ?"""
        db = self.db if db is None else db
        raw = db
        try:
            while isinstance(raw, ModuleDb):
                raw = object.__getattribute__(raw, "_db")
        except Exception:  # noqa: BLE001 - proxy sans attributs
            pass
        if getattr(raw, "_has_migration_slots", False):
            return True
        rows = db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS present",
            (relation(db, "work_migration_reservations"),))
        found = bool(rows and rows[0]["present"])
        if found:
            try:
                raw._has_migration_slots = True
            except Exception:  # noqa: BLE001 - objet sans attributs
                pass
        return found

    def migrations_column(self, db=None) -> str:
        """L'expression de la colonne `migrations` d'un lot (sous-requête bornée)."""
        db = self.db if db is None else db
        if not self.slots_available(db):
            return "'[]'::jsonb AS migrations"
        return (
            "coalesce((SELECT jsonb_agg(jsonb_build_object("
            "'number', r.number, 'repo', r.target_repo, 'slot', r.slot)"
            " ORDER BY r.number, r.slot)"
            " FROM %s r"
            " WHERE r.work_item_id = work_items.id AND r.status = 'reserved'), '[]'::jsonb)"
            " AS migrations" % relation(db, "work_migration_reservations"))

    def land_default_slot(self, item_id, number, repo) -> None:
        """#4097 : le créneau `default` atterri suit le numéro réellement posé."""
        db = self.db
        if not self.slots_available(db):
            return
        db.execute(
            "UPDATE work_migration_reservations SET number = %s, status = 'landed',"
            "       updated_at = now()"
            " WHERE work_item_id = %s AND slot = 'default'"
            "   AND status IN ('landed', 'released') AND target_repo = %s"
            "   AND (number <> %s OR status = 'released')",
            (int(number), int(item_id), "github.com/" + repo, int(number)))

    def release_landed_slot(self, item_id, number, repo) -> None:
        """#4100 : un créneau atterri introuvable est relâché, jamais deviné."""
        db = self.db
        if not self.slots_available(db):
            return
        db.execute(
            "UPDATE work_migration_reservations SET status = 'released', updated_at = now()"
            " WHERE work_item_id = %s AND slot = 'default' AND status = 'landed'"
            "   AND target_repo = %s AND number = %s",
            (int(item_id), "github.com/" + repo, int(number)))

    def train_states(self) -> list[dict]:
        """Les trains d'intégration, pour l'alerte `train_paused` (#6014)."""
        return [dict(row) for row in self.db.query(
            "SELECT target_key, revision, document FROM integration_trains"
            " ORDER BY target_key")]

    def train_projects(self) -> list[str]:
        """Les projets visés par un train (lecture bornée de la cible)."""
        return [str(row.get("target_key") or "") for row in self.db.query(
            "SELECT DISTINCT target_key FROM integration_trains")]

    def train_live_projects(self, fresh_s: float) -> list[str]:
        """#8159 : les projets dont un train tourne, vus de tout hôte.

        Une cible est vivante quand le service qui la dessert l'est : le
        propriétaire de sa réservation (`train:<runner>`, document de la
        cible) est l'exécuteur d'un bail `train-service…` vivant
        (`train-service:<runner>:<incarnation>`), et la cible a bougé depuis
        moins de `fresh_s`. Le bail d'un service qui dessert un autre projet
        ne fait pas vivre une cible qu'il ne tient pas (B #45894/#45896).

        Le runner peut lui-même contenir des `:` (`host:pid` est le défaut de
        `cfg.runner`, #8174) : on retire le préfixe puis la dernière
        incarnation du bail, jamais un découpage à l'index — un
        `split_part(..., ':', 2)` ne lirait que l'hôte et déclarerait à tort
        le train absent."""
        return [str(row.get("project") or "") for row in self.db.query(
            "SELECT DISTINCT split_part(t.target_key, ':', 1) AS project"
            " FROM integration_trains t JOIN merge_locks l"
            " ON l.target LIKE 'train-service%%' AND l.expires_at > clock_timestamp()"
            " AND l.owner LIKE 'train-service:%%'"
            " AND (t.document -> 'reservation' ->> 'owner') LIKE 'train:%%'"
            " AND regexp_replace(l.owner, '^train-service:(.*):[^:]+$', '\\1')"
            " = substring(t.document -> 'reservation' ->> 'owner' from 7)"
            " WHERE t.updated_at > clock_timestamp() - make_interval(secs => %s)",
            (float(fresh_s),))]

    def train_receipt_kinds(self, work_item_id, frozen_id, *, exact=False) -> list[str]:
        """Les reçus d'allocation/retrait d'un lot (attestation de paquet)."""
        if exact:
            rows = self.db.query(
                "SELECT kind FROM integration_train_queue_receipts"
                " WHERE work_item_id = %s AND frozen_id IS NOT DISTINCT FROM %s",
                (int(work_item_id), frozen_id))
        else:
            rows = self.db.query(
                "SELECT kind FROM integration_train_queue_receipts"
                " WHERE work_item_id = %s AND frozen_id = %s ORDER BY id",
                (int(work_item_id), frozen_id))
        return [str(row.get("kind") or "") for row in rows]

    def train_receipts(self, item_ids) -> list[dict]:
        """Les reçus d'allocation/retrait des lots donnés (#2965), dans l'ordre."""
        ids = sorted({int(item) for item in item_ids or ()})
        if not ids:
            return []
        marks = ", ".join(["%s"] * len(ids))
        return [dict(row) for row in self.db.query(
            "SELECT work_item_id, generation, frozen_id, kind"
            " FROM integration_train_queue_receipts"
            " WHERE work_item_id IN (" + marks + ") ORDER BY id", tuple(ids))]

    def alerts_entrust(self, role, host, *, lock_key, owner, epoch) -> bool:
        """#4468 : le passage d'alertes se transmet sous le bail `merge_locks`.

        Le CTE `held` est la clôture : un INSERT sans conflit doit aussi voir
        la ligne vivante de `merge_locks`, qu'un simple `ON CONFLICT ... WHERE`
        ne vérifierait pas. L'INSERT est l'instruction elle-même, pas un CTE
        modifiant les données : le pilote psql enveloppe `query()` dans un CTE
        pour sa sortie JSON, et PostgreSQL refuse une instruction modificatrice
        imbriquée dans un CTE (« must be at the top level ») — l'écriture en
        CTE n'a jamais été enregistrée sous ce pilote (#3568). Le même hôte ne
        renouvelle pas sa transmission : seul un changement d'hôte ouvre une
        nouvelle fenêtre.
        """
        rows = self.db.query(
            """
            WITH held AS (
                SELECT 1
                  FROM merge_locks
                 WHERE target = %s AND owner = %s AND epoch = %s
                   AND expires_at > clock_timestamp()
            )
            INSERT INTO alerts_delegations (role, host, entrusted_at)
            SELECT %s, %s, clock_timestamp() FROM held
            ON CONFLICT (role) DO UPDATE
               SET host = excluded.host,
                   entrusted_at = excluded.entrusted_at
             WHERE alerts_delegations.host IS DISTINCT FROM excluded.host
            RETURNING role
            """, (lock_key, owner, int(epoch), role, host))
        return bool(rows)

    def slots_for_lots(self, item_ids) -> dict:
        """Les créneaux réservés des lots donnés, pour un agrégat en une passe."""
        ids = sorted({int(item) for item in item_ids or ()})
        if not ids:
            return {}
        rows = self.db.query(
            "SELECT work_item_id, jsonb_agg(jsonb_build_object("
            "'number', number, 'repo', target_repo, 'slot', slot)"
            " ORDER BY number, slot) AS migrations"
            " FROM work_migration_reservations"
            " WHERE work_item_id IN (%s) AND status = 'reserved'"
            " GROUP BY work_item_id" % ", ".join(["%s"] * len(ids)), tuple(ids))
        return {int(row["work_item_id"]): row.get("migrations") for row in rows}

    def reserved_numbers(self, item_id) -> list[int]:
        """Les numéros de migration encore réservés d'un lot, croissants."""
        rows = self.db.query(
            "SELECT number FROM work_migration_reservations"
            " WHERE work_item_id = %s AND status = 'reserved' ORDER BY number",
            (int(item_id),))
        return [int(row["number"]) for row in rows]

    def release_slots(self, tx, item_id, *, slot=None, number=None) -> None:
        """Rend tout ou partie des créneaux réservés d'un lot."""
        tx = ModuleDb(tx)
        sql = ("UPDATE work_migration_reservations SET status = 'released',"
               "       updated_at = now()"
               " WHERE work_item_id = %s AND status = 'reserved'")
        params = [int(item_id)]
        if slot is not None:
            sql += " AND slot = %s"
            params.append(slot)
        if number is not None:
            sql += " AND number = %s"
            params.append(int(number))
        tx.execute(sql, tuple(params))

    def next_number(self, tx, repo, floor) -> int:
        """Le prochain numéro libre du dépôt, au moins `floor`."""
        tx = ModuleDb(tx)
        row = tx.query(
            "SELECT greatest(%s,"
            " (SELECT coalesce(max(migration), 0) + 1 FROM work_items"
            "   WHERE migration_repo = %s AND state NOT IN %s),"
            " (SELECT coalesce(max(number), 0) + 1"
            "  FROM work_migration_reservations"
            "  WHERE target_repo = %s AND status = 'reserved')) AS n",
            (int(floor), repo, self._core()._CLOSED, "github.com/" + repo))[0]
        return int(row["n"])

    def add_legacy_slot(self, tx, item_id, slot, number, project, repo) -> None:
        tx = ModuleDb(tx)
        tx.execute(
            "INSERT INTO work_migration_reservations"
            " (work_item_id, slot, number, project, target_repo,"
            "  target_branch, status)"
            " VALUES (%s, %s, %s, %s, %s, NULL, 'reserved')"
            " ON CONFLICT (work_item_id, slot) DO UPDATE SET"
            "   number = excluded.number, project = excluded.project,"
            "   target_repo = excluded.target_repo,"
            "   target_branch = excluded.target_branch,"
            "   status = 'reserved', updated_at = now()",
            (int(item_id), slot, int(number), project or "", "github.com/" + repo))

    def review_catch_up_bound(self):
        """L539 (#1104) : la borne d'activation (ligne unique de la migration
        0438), ou None si elle manque — le rattrapage reste alors fermé. Rend
        une chaîne ISO-8601, identique pour les deux pilotes (`psql` rend le
        texte, `psycopg` un `datetime`), utilisable telle quelle en paramètre."""
        rows = self.db.query("SELECT bound_at FROM review_catch_up WHERE id")
        if not rows:
            return None
        value = rows[0]["bound_at"]
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    def _release_migration(self, tx, item_id, row, *, why, actor) -> None:
        """#956, #2481 : libère les réservations du lot qui entre dans un état
        terminal (`merged`, `promoted`, `closed`), dans la transaction même de
        la transition : une réservation tenue par un lot fusionné bloquait le
        numéro pour toujours. Le numéro reste protégé tant qu'un fichier le
        porte sur la cible (réservation à numéro exact, `target_numbers`) ou
        sur une branche (allocation du prochain libre, `repo_numbers`). Tous
        les créneaux réservés du lot sont rendus, pas seulement le scalaire."""
        # P1b #7650 : le noyau appelle cette opération avec la transaction
        # brute de son propre mouvement ; la qualification du module se fait
        # ici, jamais par un import de l'implémentation dans le noyau (L97).
        if not isinstance(tx, ModuleDb):
            tx = ModuleDb(tx)
        numbers = sorted({int(item["number"]) for item in (row.get("migrations") or ())
                          if item.get("number") is not None})
        if row.get("migration") is not None and int(row["migration"]) not in numbers:
            numbers.append(int(row["migration"]))
            numbers.sort()
        if not numbers:
            return
        if self.slots_available(tx):
            tx.execute(
                "UPDATE work_migration_reservations SET status = 'released',"
                "       updated_at = now()"
                " WHERE work_item_id = %s AND status = 'reserved'", (int(item_id),))
        tx.execute(
            "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
            "       updated_at = now() WHERE id = %s", (int(item_id),))
        # le lot rendu à l'appelant reflète l'état validé, pas la valeur d'avant
        row["migration"] = None
        row["migration_repo"] = None
        row["migrations"] = []
        what = ("migration %s" % numbers[0] if len(numbers) == 1
                else "migrations %s" % ", ".join(str(n) for n in numbers))
        self._event(
            tx, int(item_id), row["state"],
            "%s released (%s, #956)" % (what, why), actor)

    def _release_migration_rows(self, tx, item_id, *, slots, scalar, number, note, actor,
                                state, repo=None) -> dict | None:
        """Libère le numéro `number` (None : tous) du lot, et rend sa ligne.

        Le scalaire est la réservation principale ; s'il est libéré alors que
        d'autres créneaux legacy restent, le plus petit est promu et son
        créneau libéré AVANT l'UPDATE du scalaire : le déclencheur
        `work_items_migration_slot_sync` réécrit alors le créneau `default`
        sans rencontrer le doublon du créneau promu. Les slots nommés restent
        intacts : leur identité ne devient jamais `default` (#4430)."""
        # P1b #7650 : même frontière que `_release_migration`, le noyau passe
        # la transaction brute et le module la qualifie.
        if not isinstance(tx, ModuleDb):
            tx = ModuleDb(tx)
        numbers = sorted({int(row["number"]) for row in slots})
        if scalar is not None and int(scalar) not in numbers:
            numbers.append(int(scalar))
            numbers.sort()
        if not numbers:
            return self._migration_row(tx, item_id)
        target = None if number is None else int(number)
        if target is not None and target not in numbers:
            return None
        table = self.slots_available(tx)
        if target is None:
            if table:
                tx.execute(
                    "UPDATE work_migration_reservations SET status = 'released',"
                    "       updated_at = now()"
                    " WHERE work_item_id = %s AND status = 'reserved'", (int(item_id),))
            tx.execute(
                "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
                "       updated_at = now() WHERE id = %s", (int(item_id),))
        elif scalar is not None and target == int(scalar):
            remaining = [row for row in slots if int(row["number"]) != target
                         and (row["slot"] == "default" or (
                             row["slot"] == "legacy-%d" % int(row["number"])
                             and row.get("target_sha") is None))]
            promoted = remaining[0] if remaining else None
            if table:
                for row in slots:
                    if int(row["number"]) == target or (
                            promoted is not None and row["slot"] == promoted["slot"]):
                        tx.execute(
                            "UPDATE work_migration_reservations SET status = 'released',"
                            "       updated_at = now()"
                            " WHERE work_item_id = %s AND slot = %s"
                            "   AND status = 'reserved'", (int(item_id), row["slot"]))
            if promoted is None:
                tx.execute(
                    "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
                    "       updated_at = now() WHERE id = %s", (int(item_id),))
            else:
                promoted_repo = repo or str(promoted.get("target_repo") or "")
                if promoted_repo.startswith("github.com/"):
                    promoted_repo = promoted_repo[len("github.com/"):]
                tx.query(
                    "UPDATE work_items SET migration = %%s, migration_repo = %%s,"
                    "       updated_at = now() WHERE id = %%s"
                    " RETURNING %s" % self._core().item_columns(tx),
                    (int(promoted["number"]), promoted_repo, int(item_id)))
        elif table:
            for row in slots:
                if int(row["number"]) == target:
                    tx.execute(
                        "UPDATE work_migration_reservations SET status = 'released',"
                        "       updated_at = now()"
                        " WHERE work_item_id = %s AND slot = %s"
                        "   AND status = 'reserved'", (int(item_id), row["slot"]))
        if target is not None:
            note = note % {"number": target}
        elif "%(number)" in note:
            # #2481 N2 : une libération totale n'a pas UN numéro ; le
            # gabarit nomme l'ensemble au lieu du sentinelle 0.
            note = note.replace("%(number)04d", "%s").replace("%(number)s", "%s")
            note = note % ", ".join(str(value) for value in numbers)
        self._event(tx, int(item_id), state, note, actor)
        return self._migration_row(tx, item_id)
    def reserve_migration(self, item_id, repo, number, floor, *, note, actor,
                          issue_ref, release=False, release_number=None) -> dict | None:
        """Réserve (ou libère, `release`) un numéro de migration d'un lot
        ouvert dans le dépôt `repo`, avec une ligne de journal. Sans
        `number` : le prochain libre de ce dépôt, au moins `floor` ; l'index
        unique `work_items_migration_repo_idx` et le garde de
        `work_migration_reservations` tranchent entre deux réservations
        simultanées : la perdante attend un délai plein jitter croissant,
        borné au budget restant, et recalcule (nouvelle transaction) ;
        épuisé, `ReservationContention` — jamais la DbError de l'index
        (#478). Une autre erreur qui nomme l'index remonte telle quelle
        (codex #478 r1). Un `number` explicite déjà pris lève la DbError de
        l'index, sans réessai. #1830 : `issue_ref` (lue par l'appelant) est une
        condition du CAS, NULL et chaîne vide confondus — une correction
        concurrente de la référence pendant le scan du dépôt ne peut plus
        laisser `issue_ref` d'un dépôt avec `migration_repo` d'un autre ;
        `release` l'ignore. En transaction appelante (`import_reservations`,
        #879 B2), aucune reprise locale : la transaction y est perdue, la
        DbError remonte et l'appelant décide.

        #2481 : quand le lot a déjà une réservation, `number` (ou le prochain
        libre) est AJOUTÉ comme créneau `legacy-<numéro>` de
        `work_migration_reservations`, et le scalaire reste la réservation
        principale. `release` libère tout, ou seulement `release_number`."""
        if release:
            repo = repo or None
        deadline = time.monotonic() + self._core()._RESERVE_BUDGET_S
        wait = self._core()._RESERVE_WAIT_BASE_S
        while True:
            try:
                with self.db.transaction() as tx:
                    if not self._core()._lock_migration_writer(tx, item_id, None if release else repo):
                        return None
                    current = tx.query(
                        "SELECT id, migration, migration_repo, app, branch_target, state,"
                        "       coalesce(issue_ref, '') AS issue_ref"
                        "  FROM work_items WHERE id = %s", (int(item_id),))
                    if not current:
                        return None
                    current = current[0]
                    scalar = (None if current["migration"] is None
                              else int(current["migration"]))
                    slots = []
                    if self.slots_available(tx):
                        slots = tx.query(
                            "SELECT slot, number, target_repo, target_sha"
                            "  FROM work_migration_reservations"
                            " WHERE work_item_id = %s AND status = 'reserved'"
                            " ORDER BY number, slot", (int(item_id),))
                    if release:
                        return self._release_migration_rows(
                            tx, item_id, slots=slots, scalar=scalar,
                            number=release_number, note=note, actor=actor,
                            state=current["state"], repo=repo)
                    if current["state"] in lifecycle.COMPLETED:
                        return None
                    if (current["issue_ref"] or "") != (issue_ref or ""):
                        return None
                    # #956 : une réservation héritée d'un lot terminal
                    # (prise avant ce correctif) ne tient plus le numéro :
                    # elle est libérée dans la transaction de la
                    # réservation, avec sa ligne de journal. Les lignes
                    # sont lues AVANT l'UPDATE : `RETURNING` rendrait la
                    # valeur déjà effacée. `ORDER BY id` : ordre de
                    # verrouillage canonique (#967) — deux transactions qui
                    # reprennent les réservations héritées du même dépôt
                    # verrouillent ainsi les mêmes lignes dans le même
                    # ordre, jamais en sens inverse (interblocage).
                    for old in tx.query(
                            "SELECT id, migration, state FROM work_items"
                            " WHERE migration_repo = %s"
                            f"   AND state IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                            " ORDER BY id FOR UPDATE", (repo,)):
                        if self.slots_available(tx):
                            tx.execute(
                                "UPDATE work_migration_reservations SET status = 'released',"
                                "       updated_at = now()"
                                " WHERE work_item_id = %s AND status = 'reserved'",
                                (int(old["id"]),))
                        tx.execute(
                            "UPDATE work_items SET migration = NULL,"
                            "       migration_repo = NULL, updated_at = now()"
                            " WHERE id = %s", (int(old["id"]),))
                        self._event(
                            tx, int(old["id"]), old["state"],
                            "migration %s released (lot %s %s, #956)" % (
                                old["migration"], old["id"], old["state"]), actor)
                    if scalar is None and slots:
                        if any(row["slot"] != "default" and not (
                                row["slot"] == "legacy-%d" % int(row["number"])
                                and row.get("target_sha") is None) for row in slots):
                            raise ValueError("named migration slots require --slot; legacy allocation refused")
                        # état incohérent (scalaire effacé hors API) : le plus
                        # petit créneau redevient la réservation principale
                        first = slots[0]
                        tx.execute(
                            "UPDATE work_migration_reservations SET status = 'released',"
                            "       updated_at = now()"
                            " WHERE work_item_id = %s AND slot = %s"
                            "   AND status = 'reserved'", (int(item_id), first["slot"]))
                        tx.query(
                            "UPDATE work_items SET migration = %%s, migration_repo = %%s,"
                            "       updated_at = now() WHERE id = %%s"
                            " RETURNING %s" % self._core().item_columns(tx),
                            (int(first["number"]), repo, int(item_id)))
                        scalar = int(first["number"])
                    if scalar is not None and (current["migration_repo"] or "") != (repo or ""):
                        # déplacement de la réservation principale vers un
                        # autre dépôt (appel storage direct, #711) : les
                        # créneaux de l'ancien dépôt sont rendus, puis le
                        # scalaire est réécrit ; le déclencheur 0419 recrée
                        # le créneau `default` dans le nouveau dépôt.
                        if self.slots_available(tx):
                            tx.execute(
                                "UPDATE work_migration_reservations SET status = 'released',"
                                "       updated_at = now()"
                                " WHERE work_item_id = %s AND status = 'reserved'",
                                (int(item_id),))
                        if number is None:
                            value, params = (
                                "greatest(%s, (SELECT coalesce(max(migration), 0) + 1"
                                " FROM work_items WHERE migration_repo = %s"
                                f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')),"
                                " (SELECT coalesce(max(number), 0) + 1"
                                "  FROM work_migration_reservations"
                                "  WHERE target_repo = %s AND status = 'reserved'))",
                                (int(floor), repo, "github.com/" + repo))
                        else:
                            value, params = "%s", (int(number),)
                        rows = tx.query(
                            "UPDATE work_items SET migration = %s, migration_repo = %%s,"
                            "       updated_at = now() WHERE id = %%s"
                            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                            "   AND coalesce(issue_ref, '') = coalesce(%%s, '')"
                            " RETURNING %s" % (value, self._core().item_columns(tx)),
                            params + (repo, int(item_id), issue_ref))
                        if not rows:
                            return None
                        self._event(
                            tx, int(item_id), rows[0]["state"],
                            note % {"number": int(rows[0]["migration"] or 0)}, actor)
                        return rows[0]
                    if scalar is not None:
                        # la réservation principale est conservée : le
                        # nouveau numéro devient un créneau supplémentaire
                        if number is not None:
                            value = int(number)
                            if value == scalar or any(
                                    int(row["number"]) == value for row in slots):
                                return self._migration_row(tx, item_id)
                        else:
                            row = tx.query(
                                "SELECT greatest(%s,"
                                " (SELECT coalesce(max(migration), 0) + 1 FROM work_items"
                                "  WHERE migration_repo = %s"
                                f"    AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')),"
                                " (SELECT coalesce(max(number), 0) + 1"
                                "  FROM work_migration_reservations"
                                "  WHERE target_repo = %s AND status = 'reserved')) AS n",
                                (int(floor), repo, "github.com/" + repo))[0]
                            value = int(row["n"])
                        slot = "legacy-%d" % value
                        tx.execute(
                            "INSERT INTO work_migration_reservations"
                            " (work_item_id, slot, number, project, target_repo,"
                            "  target_branch, status)"
                            " VALUES (%s, %s, %s, %s, %s, NULL, 'reserved')"
                            " ON CONFLICT (work_item_id, slot) DO UPDATE SET"
                            "   number = excluded.number, project = excluded.project,"
                            "   target_repo = excluded.target_repo,"
                            "   target_branch = excluded.target_branch,"
                            "   status = 'reserved', updated_at = now()",
                            (int(item_id), slot, value, current["app"] or "",
                             "github.com/" + repo))
                        self._event(
                            tx, int(item_id), current["state"],
                            note % {"number": value}, actor)
                        return self._migration_row(tx, item_id)
                    # aucune réservation : le scalaire reste la principale,
                    # le déclencheur 0419 crée son créneau `default`
                    if number is None:
                        value, params = (
                            "greatest(%s, (SELECT coalesce(max(migration), 0) + 1"
                            " FROM work_items WHERE migration_repo = %s"
                            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')),"
                            " (SELECT coalesce(max(number), 0) + 1"
                            "  FROM work_migration_reservations"
                            "  WHERE target_repo = %s AND status = 'reserved'))",
                            (int(floor), repo, "github.com/" + repo))
                    else:
                        value, params = "%s", (int(number),)
                    rows = tx.query(
                        "UPDATE work_items SET migration = %s, migration_repo = %%s,"
                        "       updated_at = now()"
                        " WHERE id = %%s"
                        f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                        "   AND coalesce(issue_ref, '') = coalesce(%%s, '')"
                        " RETURNING %s" % (value, self._core().item_columns(tx)),
                        params + (repo, int(item_id), issue_ref))
                    if not rows:
                        return None
                    self._event(
                        tx, int(item_id), rows[0]["state"],
                        note % {"number": int(rows[0]["migration"] or 0)}, actor)
                    return rows[0]
            except DbError as exc:
                if (number is not None or release or in_transaction(self.db)
                        or not (is_unique_violation(exc, self._core().MIGRATION_INDEX)
                                or is_unique_violation(
                                    exc, "work_migration_reservations_active_idx"))):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise interface.ReservationContention(
                        "reservation contention, retry") from exc
                # plein jitter (codex #478 r1) : uniform(0, min(plafond,
                # base·2^n)) — les perdantes se répartissent au lieu de
                # retenter ensemble — borné au budget restant pour n'entamer
                # aucune transaction après l'échéance
                time.sleep(random.uniform(0.0, min(wait, remaining)))
                wait = min(wait * 2, self._core()._RESERVE_WAIT_MAX_S)
                if time.monotonic() >= deadline:
                    raise interface.ReservationContention(
                        "reservation contention, retry") from exc
    def migration_holder(self, number, repo) -> dict | None:
        """Le lot OUVERT qui tient ce numéro (#956 : un lot terminal ne le
        tient plus). #2481 : les créneaux supplémentaires comptent aussi (un
        lot peut réserver plusieurs numéros)."""
        if self.slots_available(self.db):
            rows = self.db.query(
                "SELECT w.id, w.title, w.state FROM work_items w"
                f" WHERE w.state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                "   AND ((w.migration = %s AND w.migration_repo = %s)"
                "        OR EXISTS (SELECT 1 FROM work_migration_reservations r"
                "                    WHERE r.work_item_id = w.id"
                "                      AND r.status = 'reserved' AND r.number = %s"
                "                      AND (r.target_repo = %s OR r.target_repo = %s)))"
                " ORDER BY w.id LIMIT 1",
                (int(number), repo, int(number), repo, "github.com/" + repo))
        else:
            rows = self.db.query(
                "SELECT id, title, state FROM work_items WHERE migration = %s"
                " AND migration_repo = %s"
                f" AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')", (int(number), repo))
        return rows[0] if rows else None
    def landed_slot(self, item_id) -> dict | None:
        """#4100 : le créneau `default` atterri du lot (`number`, `repo` sous la
        forme `owner/repo`), ou None."""
        if not self.slots_available(self.db):
            return None
        rows = self.db.query(
            "SELECT number, target_repo FROM work_migration_reservations"
            " WHERE work_item_id = %s AND slot = 'default' AND status = 'landed'",
            (int(item_id),))
        if not rows:
            return None
        repo = str(rows[0]["target_repo"] or "")
        return {"number": int(rows[0]["number"]),
                "repo": repo[len("github.com/"):] if repo.startswith("github.com/") else repo}
    def record_resolution(self, item_id, other_id, *, commit, lot_tip, other_tip, ref,
                          files, note, actor) -> dict | None:
        """Upsert of the pair's resolution and its journal line, in one
        transaction. `files` goes through JSON: the psql driver binds no
        arrays."""
        with self.db.transaction() as tx:
            lots = tx.query("SELECT id, state FROM work_items WHERE id IN (%s, %s)",
                            (int(item_id), int(other_id)))
            states = {int(r["id"]): r["state"] for r in lots}
            if len(states) != 2:
                return None
            rows = tx.query(
                "INSERT INTO work_resolutions (work_item_id, other_item_id, commit_sha,"
                "                              lot_tip, other_tip, ref, files, created_by)"
                " VALUES (%%s, %%s, %%s, %%s, %%s, %%s,"
                "         ARRAY(SELECT jsonb_array_elements_text(%%s::jsonb)), %%s)"
                " ON CONFLICT (work_item_id, other_item_id) DO UPDATE SET"
                "   commit_sha = excluded.commit_sha, lot_tip = excluded.lot_tip,"
                "   other_tip = excluded.other_tip, ref = excluded.ref,"
                "   files = excluded.files, created_by = excluded.created_by,"
                "   created_at = now()"
                " RETURNING %s" % RESOLUTION_COLUMNS,
                (int(item_id), int(other_id), commit, lot_tip, other_tip, ref,
                 json.dumps(list(files or ())), actor or ""))
            self._event(tx, int(item_id), states[int(item_id)], note, actor)
            return rows[0]
    def resolutions(self, item_ids) -> list[dict]:
        ids = json.dumps(sorted({int(i) for i in item_ids or ()}))
        return self.db.query(
            "SELECT %s FROM work_resolutions"
            " WHERE work_item_id IN (SELECT jsonb_array_elements_text(%%s::jsonb)::bigint)"
            "    OR other_item_id IN (SELECT jsonb_array_elements_text(%%s::jsonb)::bigint)"
            " ORDER BY created_at DESC, work_item_id, other_item_id"
            % RESOLUTION_COLUMNS, (ids, ids))
    def _queue_row(self, db, item_id) -> dict | None:
        rows = db.query("SELECT %s FROM merge_queue q JOIN work_items w ON w.id = q.work_item_id"
                        " WHERE q.work_item_id = %%s" % QUEUE_COLUMNS, (int(item_id),))
        return rows[0] if rows else None
    def queue_add(self, item_id, *, target, frozen_sha, note, actor,
                  source="", frozen_id=None, packet=None, packet_initial=False, lease=None,
                  withdrawn_after=None) -> dict | None:
        with self.db.transaction() as tx:
            lot = tx.query("SELECT state, app, branch, branch_target, type FROM work_items WHERE id = %s"
                           f" AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                           " FOR NO KEY UPDATE", (int(item_id),))
            if not lot:
                return None
            # #3532: read under the lot row lock that `queue_remove` takes
            # first, so a take-out cannot slip between this check and insertion.
            # #3550: the take-out only binds the target and freeze it names.
            if withdrawn_after is not None and DevWork(tx).queue_withdrawn_since(
                    item_id, withdrawn_after, target=target,
                    frozen_id=frozen_id) is not None:
                return None
            if lease is not None:
                key, owner, epoch = lease
                held = tx.query("SELECT owner, epoch FROM merge_locks WHERE target = %s FOR UPDATE",
                                (key,))
                if not held or (held[0]["owner"], held[0]["epoch"]) != (owner, epoch):
                    return None
                # Check after acquiring the row lock: waiting cannot extend
                # the lease through a transaction-start timestamp.
                if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                                " AND expires_at > clock_timestamp()", (key,)):
                    return None
            latest = tx.query("SELECT id FROM work_item_milestones WHERE work_item_id = %s"
                              " AND kind = 'frozen' ORDER BY at DESC, id DESC LIMIT 1", (int(item_id),))
            latest_id = latest[0]["id"] if latest else None
            if frozen_id is not None and frozen_id != latest_id:
                return None
            if packet is not None:
                fields = ("app", "branch", "branch_target", "type")
                if frozen_id is None or lot[0]["state"] not in lifecycle.TO_VALIDATE \
                        or set(packet) != set(fields) | {"resolved_target"} \
                        or packet["resolved_target"] != target \
                        or any(lot[0][key] != packet[key] for key in fields):
                    return None
                current = self._queue_row(tx, item_id)
                markers = tx.query("SELECT kind, frozen_id, generation FROM integration_train_queue_receipts"
                                   " WHERE work_item_id = %s ORDER BY id", (int(item_id),))
                allocated = None
                for receipt in markers:
                    if receipt.get("frozen_id") != frozen_id:
                        continue
                    if receipt.get("kind") == "removed":
                        return None
                    if receipt.get("kind") == "packet":
                        allocated = receipt.get("generation")
                if current is not None:
                    same = (current["generation"], current["target"], current["frozen_id"],
                            current["frozen_sha"], current["source"])
                    # #8096 : une ligne de file laissée par un AUTRE gel (la
                    # branche a avancé depuis l'éjection) ne vise que ce gel
                    # périmé. Un nouveau gel validé, d'une tête différente,
                    # remplace la ligne sous une nouvelle génération et son
                    # reçu de paquet, sans geste manuel. La ligne n'est pas
                    # verrouillée par la lecture : l'UPDATE compare la ligne
                    # entière lue (génération, cible, source, gel, état,
                    # motif) et une écriture concurrente le fait échouer.
                    stale_freeze = (
                        allocated is None
                        and current["state"] in ("queued", "ejected")
                        and current["target"] == target
                        and current["source"] == source
                        and current["frozen_sha"] != frozen_sha)
                    if stale_freeze:
                        replaced = tx.query(
                            "UPDATE merge_queue SET frozen_sha = %s, frozen_id = %s,"
                            "   state = 'queued', rank = NULL, landing_sha = NULL,"
                            "   reason = '', generation = nextval('merge_queue_generation_seq'),"
                            "   queued_by = %s, queued_at = clock_timestamp(), updated_at = now()"
                            " WHERE work_item_id = %s AND generation = %s AND target = %s"
                            "   AND source = %s AND frozen_sha = %s"
                            "   AND frozen_id IS NOT DISTINCT FROM %s AND state = %s"
                            "   AND reason IS NOT DISTINCT FROM %s"
                            " RETURNING work_item_id",
                            (frozen_sha, frozen_id, actor or "", int(item_id),
                             current["generation"], target, source, current["frozen_sha"],
                             current["frozen_id"], current["state"], current["reason"]))
                        if replaced:
                            self._event(
                                tx, int(item_id), lot[0]["state"],
                                "%s (#8096: stale queue row of freeze %s replaced by "
                                "freeze %s, generation %s)"
                                % (note, current["frozen_sha"][:12], frozen_sha[:12],
                                   current["generation"]), actor)
                            row = self._queue_row(tx, item_id)
                            tx.execute("INSERT INTO integration_train_queue_receipts"
                                       " (work_item_id, generation, frozen_id, kind)"
                                       " VALUES (%s, %s, %s, 'packet')",
                                       (row["work_item_id"], row["generation"],
                                        row["frozen_id"]))
                            return dict(row, packet_allocated=True)
                    if same != (allocated, target, frozen_id, frozen_sha, source):
                        return None
                    if current["state"] == "queued":
                        return current
                    if not interface.ci_required_ejection(current):
                        return None
                    # #3560 : la file radar a éjecté cette allocation faute de CI
                    # de lot (§11 bis : il n'y en a pas, la CI du paquet en tient
                    # lieu). Même gel, même SHA, aucun retrait : l'allocation est
                    # remise en file sous une nouvelle génération et son reçu ;
                    # un écrivain resté sur l'ancienne génération est refusé.
                    # La ligne lue n'est pas verrouillée (une éjection n'écrit
                    # que merge_queue) : l'UPDATE compare la ligne entière lue,
                    # cause comprise, et une éjection concurrente pour une autre
                    # cause le fait échouer (revue cx3, B/P2).
                    requeued = tx.query(
                        "UPDATE merge_queue SET state = 'queued', rank = NULL, landing_sha = NULL,"
                        "   reason = '', generation = nextval('merge_queue_generation_seq'),"
                        "   queued_by = %s, queued_at = clock_timestamp(), updated_at = now()"
                        " WHERE work_item_id = %s AND state = 'ejected' AND generation = %s"
                        "   AND reason = %s AND target = %s AND frozen_id IS NOT DISTINCT FROM %s"
                        "   AND frozen_sha = %s AND source = %s"
                        " RETURNING work_item_id",
                        (actor or "", int(item_id), allocated, current["reason"], target,
                         frozen_id, frozen_sha, source))
                    if not requeued:
                        return None
                    self._event(
                        tx, int(item_id), lot[0]["state"],
                        "%s (#3560: allocation ejected for want of a lot CI, "
                        "requeued: generation %s)" % (note, allocated), actor)
                    row = self._queue_row(tx, item_id)
                    tx.execute("INSERT INTO integration_train_queue_receipts"
                               " (work_item_id, generation, frozen_id, kind) VALUES (%s, %s, %s, 'packet')",
                               (row["work_item_id"], row["generation"], row["frozen_id"]))
                    return dict(row, packet_allocated=True)
                if allocated is not None:
                    return None  # the allocated entry was withdrawn; do not resurrect it
                if packet_initial is not True:
                    return None  # absence is ambiguous for legacy DELETE-only writers
            conflict = (" DO NOTHING" if packet is not None else
                " DO UPDATE SET target = excluded.target,"
                "   frozen_sha = excluded.frozen_sha, state = 'queued', rank = NULL,"
                "   source = excluded.source, frozen_id = excluded.frozen_id,"
                "   generation = nextval('merge_queue_generation_seq'), landing_sha = NULL,"
                "   reason = '', queued_by = excluded.queued_by, queued_at = now(),"
                "   updated_at = now()")
            # Packet cutoff eligibility starts at insertion, not at an
            # enclosing transaction's BEGIN before it waited for these locks.
            inserted = tx.query(
                "INSERT INTO merge_queue (work_item_id, target, frozen_sha, queued_by, source, frozen_id, queued_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, "
                + ("clock_timestamp()" if packet is not None else "now()") + ")"
                " ON CONFLICT (work_item_id)" + conflict + " RETURNING work_item_id",
                (int(item_id), target, frozen_sha, actor or "", source or "", latest_id))
            if not inserted:
                return None
            self._event(tx, int(item_id), lot[0]["state"], note, actor)
            row = self._queue_row(tx, item_id)
            if packet is not None:
                tx.execute("INSERT INTO integration_train_queue_receipts"
                           " (work_item_id, generation, frozen_id, kind) VALUES (%s, %s, %s, 'packet')",
                           (row["work_item_id"], row["generation"], row["frozen_id"]))
                # #3560 : la ligne rendue est lue avant son reçu ; elle le porte
                # déjà, comme toute relecture (`packet_allocated`).
                row = dict(row, packet_allocated=True)
            return row
    def queue_update(self, item_id, *, state=None, rank=None, reason=None, landing_sha=None,
                     note="", actor="", clear_rank=False, lease=None,
                     freeze=None, entry=None) -> dict | None:
        key, owner, epoch = lease if lease is not None else (None, None, None)
        frozen, landing = freeze if freeze is not None else (None, None)
        generation, target, frozen_id, entry_sha = entry if entry is not None else (None,) * 4
        with self.db.transaction() as tx:
            if entry is not None:
                # Same lock as add_milestone/queue_add: read the latest freeze
                # after waiting for any concurrent refreeze to finish.
                tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE", (int(item_id),))
            # #194 r6: the lease row is locked by the same statement (an
            # acquisition waits for this write, or this write sees its new
            # epoch), its expiry read on the wall clock, not the transaction's
            rows = tx.query(
                "WITH lease AS (SELECT 1 FROM merge_locks l"
                "    WHERE l.target = %s AND l.owner = %s AND l.epoch = %s"
                "      AND l.expires_at > clock_timestamp() FOR UPDATE)"
                " UPDATE merge_queue SET state = coalesce(%s, state),"
                "   rank = CASE WHEN %s THEN NULL ELSE coalesce(%s, rank) END,"
                "   landing_sha = CASE WHEN %s IN ('queued', 'ejected') THEN NULL"
                "                      ELSE coalesce(%s, landing_sha) END,"
                "   reason = coalesce(%s, reason), updated_at = now()"
                " WHERE work_item_id = %s"
                "   AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM lease))"
                # #194 r8: or under the freeze — still `landing`, same freeze
                # and same landing commit (a new freeze is never overwritten)
                "   AND (%s::text IS NULL OR (state = 'landing' AND frozen_sha = %s"
                "        AND landing_sha IS NOT DISTINCT FROM %s))"
                "   AND (NOT %s OR (generation = %s AND target = %s"
                "        AND frozen_id IS NOT DISTINCT FROM %s AND frozen_sha = %s"
                "        AND frozen_id IS NOT DISTINCT FROM (SELECT id FROM work_item_milestones"
                "          WHERE work_item_id = %s AND kind = 'frozen'"
                "          ORDER BY at DESC, id DESC LIMIT 1)))"
                " RETURNING work_item_id",
                (key, owner, epoch, state, state == "ejected" or bool(clear_rank), rank,
                 state or "", landing_sha, reason, int(item_id), key, frozen, frozen,
                 landing, entry is not None, generation, target, frozen_id, entry_sha, int(item_id)))
            if not rows:
                return None
            if note:
                lot = tx.query("SELECT state FROM work_items WHERE id = %s", (int(item_id),))
                self._event(tx, int(item_id), lot[0]["state"], note, actor)
            return self._queue_row(tx, item_id)
    def queue_remove(self, item_id, *, note, actor) -> bool:
        with self.db.transaction() as tx:
            # Same order as allocation/refreeze: a withdrawal cannot vanish
            # between a packet's check for absence and its insertion.
            lot = tx.query("SELECT state FROM work_items WHERE id = %s FOR NO KEY UPDATE", (int(item_id),))
            rows = tx.query("DELETE FROM merge_queue WHERE work_item_id = %s"
                            " RETURNING work_item_id, generation, frozen_id, target",
                            (int(item_id),))
            if not rows:
                return False
            self._event(tx, int(item_id), lot[0]["state"], note, actor)
            tx.execute("INSERT INTO integration_train_queue_receipts"
                       " (work_item_id, generation, frozen_id, target, kind)"
                       " VALUES (%s, %s, %s, %s, 'removed')",
                       (rows[0]["work_item_id"], rows[0]["generation"],
                        rows[0]["frozen_id"], rows[0].get("target")))
            return True
    def queue_take_out_on_build(self, item_id, *, note, actor) -> bool:
        """#8096 : un lot qui repasse en build sort de la file, atomiquement.

        Appelé par le mouvement du noyau dans SA transaction : la ligne de
        file ne peut pas survivre à la transition (un lot en build n'est plus
        prêt à être fusionné), et le reçu `removed` la borne, cible et gel
        compris, pour un feu ou une inscription ultérieure. Une base d'avant
        0058 (ou 0442 pour les reçus) n'a pas la file : rien à retirer.
        """
        present = self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL AS present",
            (relation(self.db, "merge_queue"),
             relation(self.db, "integration_train_queue_receipts")))
        if not present or not present[0].get("present"):
            return False
        rows = self.db.query("DELETE FROM merge_queue WHERE work_item_id = %s"
                             " RETURNING work_item_id, generation, frozen_id, target",
                             (int(item_id),))
        if not rows:
            return False
        self._event(self.db, int(item_id), lifecycle.IN_PROGRESS, note, actor)
        self.db.execute("INSERT INTO integration_train_queue_receipts"
                        " (work_item_id, generation, frozen_id, target, kind)"
                        " VALUES (%s, %s, %s, %s, 'removed')",
                        (rows[0]["work_item_id"], rows[0]["generation"],
                         rows[0]["frozen_id"], rows[0].get("target")))
        return True
    def queue_withdrawn_since(self, item_id, since_ts, *, target=None,
                              frozen_id=None) -> float | None:
        # #3550: a take-out is final only for the queue entry it removed — its
        # target and freeze. A removal of another target, or of an older
        # freeze, must not stop a new fire's fallback. Legacy receipts without
        # a target keep matching (they cannot be told apart); a receipt
        # without a freeze never matches a named freeze.
        rows = self.db.query(
            "SELECT extract(epoch FROM max(created_at))::float8 AS at"
            " FROM integration_train_queue_receipts WHERE work_item_id = %s"
            " AND kind = 'removed' AND created_at >= to_timestamp(%s)"
            " AND (%s::text IS NULL OR target IS NULL OR target = %s)"
            " AND (%s::bigint IS NULL OR (frozen_id IS NOT NULL AND frozen_id = %s))",
            (int(item_id), float(since_ts), target, target, frozen_id, frozen_id))
        return rows[0]["at"] if rows and rows[0]["at"] is not None else None
    def land_lock(self, key, owner, ttl_s, *, epoch=None) -> int | None:
        if epoch is not None:  # renewal: only the holder, at its epoch
            rows = self.db.query(
                "UPDATE merge_locks SET expires_at = clock_timestamp()"
                " + %s * interval '1 second'"
                " WHERE target = %s AND owner = %s AND epoch = %s"
                " AND expires_at > clock_timestamp() RETURNING epoch",
                (float(ttl_s), key, owner, int(epoch)))
            return int(rows[0]["epoch"]) if rows else None
        rows = self.db.query(
            "INSERT INTO merge_locks (target, owner, epoch, expires_at)"
            " VALUES (%s, %s, 1, clock_timestamp() + %s * interval '1 second')"
            " ON CONFLICT (target) DO UPDATE SET owner = excluded.owner,"
            "   epoch = merge_locks.epoch + 1, expires_at = excluded.expires_at"
            " WHERE merge_locks.owner IS NULL OR merge_locks.expires_at < clock_timestamp()"
            "    OR merge_locks.owner = excluded.owner"
            " RETURNING epoch", (key, owner, float(ttl_s)))
        return int(rows[0]["epoch"]) if rows else None
    def land_unlock(self, key, owner, epoch) -> bool:
        rows = self.db.query(
            "UPDATE merge_locks SET owner = NULL, expires_at = NULL"
            " WHERE target = %s AND owner = %s AND epoch = %s RETURNING target",
            (key, owner, int(epoch)))
        return bool(rows)
    def land_holder(self, key) -> dict | None:
        """#3598 : qui tient le bail, son époque, et jusqu'à quand, pour le
        refus de `give`/`withdraw`. #6563 : l'époque sert aussi de preuve
        fraîche au détenteur (`ameesh work packet check`). Horloge de la base,
        lecture seule, jamais une garde. L'échéance est rendue en UTC ISO
        (`…Z`), identique sous les deux pilotes plutôt que datetime (psycopg)
        ou texte (psql)."""
        rows = self.db.query(
            "SELECT owner, epoch,"
            " to_char(expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
            "   AS expires_at,"
            " extract(epoch FROM (expires_at - clock_timestamp()))::float8 AS left_s"
            " FROM merge_locks WHERE target = %s AND owner IS NOT NULL"
            "   AND expires_at > clock_timestamp()", (key,))
        return dict(rows[0]) if rows else None
    def train_read(self, key) -> dict | None:
        rows = self.db.query(
            "SELECT revision, document FROM integration_trains WHERE target_key = %s", (key,))
        if not rows:
            return None
        document = rows[0]["document"]
        if isinstance(document, str):
            document = json.loads(document)
        return dict(document, revision=int(rows[0]["revision"]))
    def train_save(self, key, document, *, revision, owner, epoch) -> dict | None:
        with self.db.transaction() as tx:
            held = tx.query("SELECT owner, epoch"
                            " FROM merge_locks WHERE target = %s FOR UPDATE", (key,))
            if not held or held[0]["owner"] != owner or int(held[0]["epoch"]) != epoch:
                return None
            # Recheck after the row lock, never using a pre-lock clock.
            live = tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                            " AND expires_at > clock_timestamp()", (key,))
            if not live:
                return None
            rows = tx.query(
                "INSERT INTO integration_trains (target_key, revision, document)"
                " SELECT %s, 1, %s::jsonb WHERE %s = 0"
                " ON CONFLICT (target_key) DO NOTHING RETURNING revision",
                (key, json.dumps(document), int(revision))) if revision == 0 else tx.query(
                # #6978 : une demande d'abandon posée sans bail survit à la
                # réécriture, pour le même plan, d'un détenteur qui l'a
                # chargée avant elle (son document n'y porte pas d'objet).
                "UPDATE integration_trains SET revision = revision + 1, document = %s::jsonb"
                " || CASE WHEN jsonb_typeof(document->'abandon_request') = 'object'"
                " AND jsonb_typeof((%s::jsonb)->'abandon_request') IS DISTINCT FROM 'object'"
                " AND document->'abandon_request'->>'plan' = (%s::jsonb)->'plan'->>'id'"
                " THEN jsonb_build_object('abandon_request', document->'abandon_request')"
                " ELSE '{}'::jsonb END,"
                " updated_at = clock_timestamp() WHERE target_key = %s AND revision = %s"
                " RETURNING revision", (json.dumps(document), json.dumps(document),
                                        json.dumps(document), key, int(revision)))
            return dict(document, revision=int(rows[0]["revision"])) if rows else None
    def train_abandon_request(self, key, plan_id, request) -> dict | None:
        with self.db.transaction() as tx:
            # Même verrou que train_save et train_fire_give : la demande est
            # vue par la prochaine écriture du détenteur, ou la précède.
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s FOR UPDATE", (key,)):
                return None
            rows = tx.query("SELECT revision, document FROM integration_trains"
                            " WHERE target_key = %s FOR UPDATE", (key,))
            if not rows:
                return None
            current = rows[0]["document"]
            if isinstance(current, str):
                current = json.loads(current)
            if current.get("phase") in ("landed", "empty", "invalidated", "promoting") \
                    or (current.get("plan") or {}).get("id") != plan_id \
                    or not isinstance(request, dict) or request.get("plan") != plan_id:
                return None
            fire = DevWork(tx).train_fire_read(key, plan_id)
            if fire and fire.get("state") in ("active", "promoting", "landed"):
                return None
            tx.execute("UPDATE integration_trains SET document = jsonb_set(document,"
                       " '{abandon_request}', %s::jsonb) WHERE target_key = %s",
                       (json.dumps(request), key))
            return dict(current, abandon_request=request, revision=int(rows[0]["revision"]))
    _REQUALIFY_COLUMNS = (
        " id, project, pin, gate, reviews, lot, requester,"
        " to_char(requested_at AT TIME ZONE 'UTC',"
        " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS requested_at, state,"
        " to_char(resolved_at AT TIME ZONE 'UTC',"
        " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS resolved_at, resolved_by, error")

    def train_requalify_request(self, project, pin, gate, reviews, lot,
                                requester) -> dict | None:
        """#7751: persist a reviewed requalification request without a lease.

        The partial unique index makes a retry of the same pending reading
        return the existing row instead of stacking duplicates. The lot is a
        real trace target (foreign key); the caller checks it before asking.

        #7806: a *new* deposit also wakes the live train service holder in this
        transaction. The hint is project-scoped and carries no authority: the
        holder still applies the request with the reviewed guards at its next
        passage. A retry that hits the unique index is not a new deposit and
        emits nothing; a rollback discards the request and the hint together.
        """
        if not isinstance(reviews, (list, tuple)) or not reviews:
            return None
        # The same evidence set in another order is the same request: the
        # partial unique index must not let a retry stack a second row.
        evidence = json.dumps(sorted({str(reference) for reference in reviews}))
        with self.db.transaction() as tx:
            rows = tx.query(
                "INSERT INTO integration_train_requalifications"
                " (project, pin, gate, reviews, lot, requester)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, %s)"
                " ON CONFLICT (project, pin, gate, reviews)"
                "   WHERE state = 'requested' DO NOTHING"
                " RETURNING" + self._REQUALIFY_COLUMNS,
                (project, pin, gate, evidence, int(lot), requester))
            if rows:
                from .... import train_events
                try:
                    train_events.emit(tx, kind="requalification-request",
                                      project=project, target="")
                except ValueError:
                    # A project name outside the wake envelope's closed set is
                    # still a valid durable request; the hourly pass applies it.
                    pass
                return _requalify_row(rows[0])
            rows = tx.query(
                "SELECT" + self._REQUALIFY_COLUMNS
                + " FROM integration_train_requalifications"
                " WHERE project = %s AND pin = %s AND gate = %s"
                " AND reviews = %s::jsonb AND state = 'requested'",
                (project, pin, gate, evidence))
            return _requalify_row(rows[0]) if rows else None

    def train_requalify_requests(self, project) -> list[dict]:
        """#7751: pending requests of ``project``, oldest first."""
        rows = self.db.query(
            "SELECT" + self._REQUALIFY_COLUMNS
            + " FROM integration_train_requalifications"
            " WHERE project = %s AND state = 'requested' ORDER BY id",
            (project,))
        return [_requalify_row(row) for row in rows]

    def train_requalify_resolve(self, request_id, state, *, actor,
                                error="") -> dict | None:
        """#7751: compare-and-set one pending request; None if already resolved."""
        if state not in ("applied", "refused"):
            return None
        rows = self.db.query(
            "UPDATE integration_train_requalifications"
            " SET state = %s, resolved_at = clock_timestamp(), resolved_by = %s,"
            " error = NULLIF(%s, '')"
            " WHERE id = %s AND state = 'requested'"
            " RETURNING" + self._REQUALIFY_COLUMNS,
            (state, actor, error, int(request_id)))
        return _requalify_row(rows[0]) if rows else None

    def train_abandon(self, key, plan_id, document, *, revision, owner, epoch) -> dict | None:
        with self.db.transaction() as tx:
            held = tx.query("SELECT owner, epoch"
                            " FROM merge_locks WHERE target = %s FOR UPDATE", (key,))
            if not held or held[0]["owner"] != owner or int(held[0]["epoch"]) != epoch:
                return None
            live = tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                            " AND expires_at > clock_timestamp()", (key,))
            if not live:
                return None
            # The target row lock is also taken by train_fire_give: a grant is
            # either committed before this read (seen just below and refused),
            # or waits and then finds the terminal phase this write leaves.
            rows = tx.query("SELECT document FROM integration_trains"
                            " WHERE target_key = %s FOR UPDATE", (key,))
            if not rows:
                return None
            current = rows[0]["document"]
            if isinstance(current, str):
                current = json.loads(current)
            phase = current.get("phase")
            # #6853 : un train `paused` s'abandonne (sa seule sortie) ; les
            # autres phases terminales restent refusées.
            if phase in ("landed", "empty", "invalidated") or phase == "promoting" \
                    or (current.get("plan") or {}).get("id") != plan_id:
                return None
            fire = DevWork(tx).train_fire_read(key, plan_id)
            if fire and fire.get("state") in ("active", "promoting", "landed"):
                return None
            if (document.get("plan") or {}).get("id") != plan_id:
                return None
            updated = tx.query(
                "UPDATE integration_trains SET revision = revision + 1, document = %s::jsonb,"
                " updated_at = clock_timestamp() WHERE target_key = %s AND revision = %s"
                " RETURNING revision", (json.dumps(document), key, int(revision)))
            return dict(document, revision=int(updated[0]["revision"])) if updated else None
    def train_adopt(self, key, owner, *, revision, previous, service, ttl_s) -> dict | None:
        if not owner.startswith("train:") or key == service[0]:
            return None
        # L165 (EP4) × #3142, B cx2 : une clé de service liée à un rôle porte
        # l'époque du rôle (`train-service:<rôle>:<époque>`). Elle survit à sa
        # tenure par le TTL de service (900 s) : l'adoption doit aussi prouver
        # que cette époque est le bail vivant courant du rôle, sinon l'ancien
        # élu reprendrait la réservation au nouvel élu.
        parts = str(service[0] or "").split(":")
        role = (parts[1], int(parts[2])) if (len(parts) == 3 and parts[1]
                and parts[0] == "train-service" and parts[2].isdigit()) else None
        with self.db.transaction() as tx:
            # Service lease first, then the target: no writer locks them in
            # the other order (train_save only locks the target row).
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s AND owner = %s"
                            " AND epoch = %s AND expires_at > clock_timestamp() FOR SHARE",
                            (service[0], service[1], int(service[2]))):
                return None
            if role is not None:
                current = tx.query(
                    "SELECT lease_epoch, status,"
                    " lease_expires_at > clock_timestamp() AS live"
                    " FROM agent_registry WHERE name = %s FOR SHARE", (role[0],))
                if not current or current[0]["lease_epoch"] is None \
                        or int(current[0]["lease_epoch"]) != role[1] \
                        or not current[0]["live"] \
                        or str(current[0]["status"] or "") == "stopped":
                    return None
            held = tx.query("SELECT owner, epoch FROM merge_locks WHERE target = %s FOR UPDATE",
                            (key,))
            if not held or not (held[0]["owner"] or "").startswith("train:") \
                    or held[0]["owner"] == owner:
                return None
            before = (held[0]["owner"], int(held[0]["epoch"]))
            if previous is not None and before != (previous[0], int(previous[1])):
                return None
            rows = tx.query("SELECT revision, document FROM integration_trains"
                            " WHERE target_key = %s FOR UPDATE", (key,))
            if (int(rows[0]["revision"]) if rows else 0) != int(revision):
                return None
            document = rows[0]["document"] if rows else None
            if isinstance(document, str):
                document = json.loads(document)
            ticket = (document or {}).get("reservation")
            # A ticket naming the held epoch moves with it, whoever asked:
            # left behind, it would bind the plan to the fenced holder.
            if not isinstance(ticket, dict) \
                    or (ticket.get("owner"), ticket.get("epoch")) != before:
                if previous is not None:
                    return None
                document = None
            # The target or document lock may have waited: recheck the
            # service lease against wall time after every lock, like train_save.
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s AND owner = %s"
                            " AND epoch = %s AND expires_at > clock_timestamp()",
                            (service[0], service[1], int(service[2]))):
                return None
            # The role lease can also expire while those locks wait: FOR SHARE
            # blocks a claim, not time. Recheck the exact epoch, liveness and
            # status after every lock, next to the final service check
            # (B/P1 cx2, #3142×#3014).
            if role is not None:
                current = tx.query(
                    "SELECT 1 FROM agent_registry WHERE name = %s AND lease_epoch = %s"
                    " AND lease_expires_at > clock_timestamp()"
                    " AND coalesce(status, '') <> 'stopped' FOR SHARE",
                    (role[0], role[1]))
                if not current:
                    return None
            epoch = int(tx.query(
                "UPDATE merge_locks SET owner = %s, epoch = epoch + 1,"
                " expires_at = clock_timestamp() + %s * interval '1 second'"
                " WHERE target = %s RETURNING epoch", (owner, float(ttl_s), key))[0]["epoch"])
            if document is not None:
                document["reservation"] = dict(document["reservation"], owner=owner, epoch=epoch)
                tx.execute("UPDATE integration_trains SET revision = revision + 1,"
                           " document = %s::jsonb, updated_at = clock_timestamp()"
                           " WHERE target_key = %s", (json.dumps(document), key))
            return {"epoch": epoch, "previous_owner": before[0], "previous_epoch": before[1]}
    @staticmethod
    def _train_fire(rows):
        if not rows:
            return None
        row = dict(rows[0])
        document = row.pop("document")
        if isinstance(document, str):
            document = json.loads(document)
        return dict(document, **row)
    def train_fire_read(self, key, plan_id) -> dict | None:
        return self._train_fire(self.db.query(
            "SELECT id, document, state, changed_by, owner, epoch,"
            " extract(epoch from created_at)::float8 AS created_ts"
            " FROM integration_train_fires WHERE target_key = %s AND plan_id = %s"
            " ORDER BY id DESC LIMIT 1", (key, plan_id)))
    def train_fire_give(self, key, document) -> dict | None:
        with self.db.transaction() as tx:
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s FOR UPDATE", (key,)):
                return None
            # Serialize both a regrant and a plan revision with the claim.
            rows = tx.query("SELECT document FROM integration_trains WHERE target_key = %s",
                            (key,))
            current = rows[0]["document"] if rows else {}
            if isinstance(current, str):
                current = json.loads(current)
            if current.get("plan") != document["plan"] or current.get("phase") not in (
                    "publishing", "ci-intent", "ci", "ready"):
                return None
            ops = type(self)(tx)
            old = ops.train_fire_read(key, document["plan"]["id"])
            if old and old["state"] != "revoked":
                return None
            tx.execute("INSERT INTO integration_train_fires"
                       " (target_key, plan_id, document, state, changed_by)"
                       " VALUES (%s, %s, %s::jsonb, 'active', %s)",
                       (key, document["plan"]["id"], json.dumps(document), document["actor"]))
            return ops.train_fire_read(key, document["plan"]["id"])
    def train_fire_change(self, key, fire_id, state, *, actor, owner=None, epoch=None) -> dict | None:
        expected = {"revoked": "active", "promoting": "active", "landed": "promoting"}
        if state not in expected:
            raise ValueError("train fire transition: revoked | promoting | landed expected")
        with self.db.transaction() as tx:
            locks = tx.query("SELECT owner, epoch FROM merge_locks WHERE target = %s FOR UPDATE",
                             (key,))
            if not locks:
                return None
            if state != "revoked":
                if owner is None or epoch is None or locks[0]["owner"] != owner \
                        or int(locks[0]["epoch"]) != epoch:
                    return None
                if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                                " AND expires_at > clock_timestamp()", (key,)):
                    return None
            rows = tx.query("SELECT document, owner, epoch FROM integration_train_fires"
                            " WHERE target_key = %s AND id = %s AND state = %s FOR UPDATE",
                            (key, int(fire_id), expected[state]))
            if not rows:
                return None
            document = rows[0]["document"]
            if isinstance(document, str):
                document = json.loads(document)
            if state != "revoked":
                # The engine must have durably entered promoting on this
                # exact plan, under the same target reservation.
                if not tx.query("SELECT 1 FROM integration_trains WHERE target_key = %s"
                                " AND document->>'phase' = 'promoting'"
                                " AND document->'plan' = %s::jsonb",
                                (key, json.dumps(document["plan"]))):
                    return None
                # A new live holder may finish a previous holder's claim
                # after observing the exact remote head. It cannot reactivate
                # the fire or retry the effect; business code supplies that
                # fresh observation before requesting this transition.
            tx.execute("UPDATE integration_train_fires SET state = %s, changed_by = %s,"
                       " owner = %s, epoch = %s, updated_at = clock_timestamp() WHERE id = %s",
                       (state, actor, owner, epoch, int(fire_id)))
            return DevWork(tx).train_fire_read(key, document["plan"]["id"])
    def queue(self, target=None) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM merge_queue q JOIN work_items w ON w.id = q.work_item_id"
            " WHERE (%%s::text IS NULL OR q.target = %%s)"
            " ORDER BY q.target, q.rank NULLS LAST, q.queued_at, q.work_item_id"
            % QUEUE_COLUMNS, (target, target))
    def set_branch_head(self, item_id, branch, head) -> bool:
        """Retient le dernier commit de la branche vu en avance sur sa cible
        (sans journal ni `updated_at` : un relevé n'est pas une activité du
        lot), et quand (L118d, 0372, horloge de la base). Une pointe inchangée
        est relevée de nouveau si son instant précède la dernière fusion
        partielle vue (#346 r2) : elle est encore là après cette fusion.
        Conditionné à la branche lue : rien si elle a changé. #6626 : la
        tête entre aussi, une fois, à l'historique des têtes du lot (un
        enfant parti d'une tête intermédiaire reste vu empilé)."""
        rows = self.db.query(
            "UPDATE work_items SET branch_head = %s, branch_head_at = clock_timestamp()"
            " WHERE id = %s AND branch = %s AND (branch_head IS DISTINCT FROM %s"
            "       OR branch_head_at IS NULL OR branch_head_at <= partial_seen_at)"
            " RETURNING id", (head, int(item_id), branch, head))
        if head:
            self.db.execute(
                "INSERT INTO work_item_branch_heads (work_item_id, branch, sha)"
                " SELECT id, branch, %s FROM work_items WHERE id = %s AND branch = %s"
                " ON CONFLICT DO NOTHING", (head, int(item_id), branch))
        return bool(rows)
    def branch_heads(self, item_ids) -> list[dict]:
        ids = sorted({int(value) for value in item_ids or ()})
        if not ids:
            return []
        return self.db.query(
            "SELECT work_item_id, branch, sha FROM work_item_branch_heads"
            " WHERE work_item_id = ANY(%s) ORDER BY work_item_id, observed_at",
            (ids,))
    def record_packet(self, package_id, *, branch, base_sha, head_sha, units, note,
                      actor, expect_pushed=None) -> list[dict]:
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id, state, branch_head,"
                "       extract(epoch FROM branch_head_at)::float8 AS branch_head_ts"
                " FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                (int(package_id),))
            if not rows:
                raise DbError("packet #%s: no such work item" % package_id)
            # #4501 : sous le verrou du porteur, relire la tête poussée avant
            # toute écriture. `FOR NO KEY UPDATE` est exclusif avec lui-même
            # (matrice PostgreSQL : deux verrous de ce mode s'attendent) et
            # avec les verrous pris par un gel (`add_milestone`) ou un relevé
            # de `branch_head` (`UPDATE`) : soit ils ont commité avant et
            # cette lecture les voit, soit ils attendent et l'enregistrement
            # porte sur la tête courante. Sans cette relecture, une lecture
            # initiale périmée de `packet_link.record` écrasait le manifeste
            # d'une tête poussée entre-temps; sans le verrou exclusif, deux
            # enregistrements concurrents mélangeaient leurs membres.
            freeze = tx.query(
                "SELECT sha, extract(epoch FROM at)::float8 AS at_ts"
                " FROM work_item_milestones"
                " WHERE work_item_id = %s AND kind = 'frozen'"
                " ORDER BY at DESC, id DESC LIMIT 1", (int(package_id),))
            protected = packet_head.protected_head(
                freeze[0]["sha"] if freeze else "",
                freeze[0]["at_ts"] if freeze else 0,
                rows[0].get("branch_head") or "",
                rows[0].get("branch_head_ts") or 0)
            if protected and not packet_head.same_commit(protected, head_sha) \
                    and not (expect_pushed
                             and packet_head.same_commit(expect_pushed, protected)):
                raise DbError(packet_head.refusal(
                    int(package_id), protected, head_sha, expect_pushed))
            # #4369 : une unité que la recomposition retire sort du véhicule ;
            # la note du porteur la nomme, l'horloge « verdict → fusion »
            # repart de là (`slo.vehicle_exits`). Une seule ligne par
            # recomposition, même quand le composeur rejoue le paquet pas à pas.
            kept = {int(unit["work_item_id"]) for unit in units}
            removed = [int(row["work_item_id"]) for row in tx.query(
                "SELECT work_item_id FROM work_packet_members WHERE package_id = %s"
                " ORDER BY work_item_id", (int(package_id),))
                if int(row["work_item_id"]) not in kept]
            if removed:
                note = "%s%s%s" % (note or "", VEHICLE_EXIT_MARK,
                                   ", ".join("#%d" % lot for lot in removed))
            tx.execute("DELETE FROM work_packet_members WHERE package_id = %s",
                       (int(package_id),))
            for unit in units:
                tx.execute(
                    "INSERT INTO work_packet_members"
                    " (package_id, work_item_id, package_branch, base_sha, head_sha,"
                    "  unit_sha, source_sha, frozen_sha, recorded_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (int(package_id), int(unit["work_item_id"]), branch, base_sha,
                     head_sha, unit["unit_sha"], unit.get("source_sha") or "",
                     unit.get("frozen_sha") or "", actor or ""))
            self._event(tx, int(package_id), rows[0]["state"], note, actor)
            return DevWork(tx).packet_members(package_ids=[int(package_id)])
    def record_packet_snapshot(self, *, repository, target, package_id, branch,
                               head_sha, base_sha, payload, actor, lease_key,
                               lease_owner, lease_epoch) -> dict:
        payload = json.loads(json.dumps(payload))
        # GitHub names are case-insensitive: store the canonical key so a
        # later checkout cannot miss its own receipt by spelling.
        repository = str(repository or "").strip().lower()
        with self.db.transaction() as tx:
            lease = tx.query(
                "SELECT owner, epoch FROM merge_locks WHERE target = %s FOR SHARE",
                (lease_key,))
            if (not lease or lease[0]["owner"] != lease_owner
                    or int(lease[0]["epoch"]) != int(lease_epoch)):
                raise DbError("packet snapshot: composer lease changed")
            live = tx.query(
                "SELECT 1 FROM merge_locks WHERE target = %s"
                " AND expires_at > clock_timestamp()", (lease_key,))
            if not live:
                raise DbError("packet snapshot: composer lease expired")
            tx.execute(
                "INSERT INTO work_packet_snapshots"
                " (repository, target, package_id, branch, head_sha, base_sha,"
                " payload, recorded_by, lease_owner, lease_epoch)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)"
                " ON CONFLICT (repository, target, package_id, head_sha) DO NOTHING",
                (repository, target, int(package_id), branch, head_sha, base_sha,
                 json.dumps(payload), actor or "", lease_owner, int(lease_epoch)))
            receipt = DevWork(tx).packet_snapshots(
                repository=repository, target=target, package_id=package_id,
                head_sha=head_sha)[0]
            if (receipt["branch"] != branch or receipt["base_sha"] != base_sha
                    or receipt["payload"] != payload):
                raise DbError("packet snapshot: immutable head already has different content")
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                            " AND expires_at > clock_timestamp()", (lease_key,)):
                raise DbError("packet snapshot: composer lease expired during write")
            return receipt
    def packet_snapshots(self, *, repository, target=None, package_id=None,
                         branch=None, head_sha=None) -> list[dict]:
        # During additive rollout an older database has no new receipts.
        # Query errors after the prerequisite are not treated as absence.
        ready = prerequisite_query(self.db,
            "SELECT to_regclass(%s) IS NOT NULL AS ready",
            (relation(self.db, "work_packet_snapshots"),))
        if not ready or not ready[0]["ready"]:
            return []
        repository = str(repository or "").strip().lower()
        where, params = ["repository = %s"], [repository]
        for column, value in (("target", target), ("package_id", package_id),
                              ("branch", branch), ("head_sha", head_sha)):
            if value is not None:
                where.append(column + " = %s")
                params.append(value)
        return self.db.query(
            "SELECT id, repository, target, package_id, branch, head_sha, base_sha,"
            " payload, recorded_by, lease_owner, lease_epoch,"
            " extract(epoch FROM recorded_at)::float8 AS recorded_ts"
            " FROM work_packet_snapshots WHERE " + " AND ".join(where)
            + " ORDER BY id", tuple(params))
    def packet_members(self, *, item_ids=None, package_ids=None) -> list[dict]:
        where, params = [], []
        if item_ids is not None:
            ids = sorted({int(value) for value in item_ids})
            if not ids:
                return []
            where.append("m.work_item_id IN (%s)" % ", ".join(["%s"] * len(ids)))
            params.extend(ids)
        if package_ids is not None:
            ids = sorted({int(value) for value in package_ids})
            if not ids:
                return []
            where.append("m.package_id IN (%s)" % ", ".join(["%s"] * len(ids)))
            params.extend(ids)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return self.db.query(
            "WITH member AS (SELECT m.* FROM work_packet_members m" + clause + "),"
            + self._PACKET_HEADS + " " + self._PACKET_SELECT
            + " ORDER BY m.recorded_at DESC, m.package_id DESC, m.work_item_id",
            tuple(params))
    def packet_member_heads(self, package_id) -> list[dict]:
        """#3416 : les têtes déclarées par les membres d'un véhicule (#7650)."""
        return self.db.query(
            "SELECT m.base_sha, m.head_sha, p.branch_target AS package_target"
            " FROM work_packet_members m JOIN work_items p ON p.id = m.package_id"
            " WHERE m.package_id = %s"
            " ORDER BY m.recorded_at DESC, m.work_item_id", (int(package_id),))

    def packet_members_since(self, since_ts) -> list[dict]:
        """#3256 C : les membres des paquets composés depuis cet instant."""
        return self.db.query(
            "SELECT m.package_id, m.work_item_id, m.frozen_sha,"
            "       p.state AS package_state,"
            "       extract(epoch FROM p.created_at)::float8 AS composed_ts"
            "  FROM work_packet_members m"
            "  JOIN work_items p ON p.id = m.package_id"
            " WHERE p.created_at > to_timestamp(%s)"
            " ORDER BY p.created_at, m.work_item_id", (float(since_ts),))

    def project_plan_rows(self, project, *, assignment_id=None) -> list[dict]:
        """#4174: complete open project snapshot, including every waiting lot."""
        # #6347: scan historical lots once, then reuse the open set for the
        # correlated footprint/capacity guards. Keep every app and improvement:
        # they can occupy a footprint even when absent from the output snapshot.
        # #7940 : la garde du plan lit la même attente humaine canonique que la
        # prise (`planned_waits.human_waits`), pas un motif SQL parallèle.
        from .... import planned_waits
        human_wait_ids = tuple(planned_waits.human_waits(self.db))
        return self.db.query(
            "WITH plan_open_items AS MATERIALIZED ("
            " SELECT * FROM work_items WHERE state NOT IN " + self._core()._CLOSED + "),"
            " pending_decisions AS MATERIALIZED ("
            " SELECT DISTINCT btrim(work_item_id) AS lot FROM agent_mailbox"
            " WHERE delivered_at IS NULL AND kind = 'request'"
            " AND payload->'decision' IS NOT NULL) "
            "SELECT w.*, extract(epoch FROM w.created_at)::float8 AS created_ts,"
            "       extract(epoch FROM w.wake_at)::float8 AS wake_ts,"
            "       to_char(w.planned_start, 'YYYY-MM-DD') AS planned_start,"
            "       to_char(w.planned_end, 'YYYY-MM-DD') AS planned_end,"
            "       to_char(w.planned_delivery, 'YYYY-MM-DD') AS planned_delivery,"
            "       CASE WHEN w.state = 'build' THEN " + self._core().requeued_sql("w") +
            " ELSE false END AS returned_to_queue,"
            "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
            "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
            "       ARRAY(SELECT d.blocker_id FROM work_item_dependencies d"
            "             JOIN work_items b ON b.id = d.blocker_id"
            "             WHERE d.work_item_id = w.id AND NOT " + self._core()._not_blocking("b") +
            "             ORDER BY d.blocker_id) AS blocking_dependencies,"
            "       ARRAY(SELECT b.id FROM work_item_links l"
            "             JOIN work_items b ON b.id = CASE WHEN l.work_item_id = w.id"
            "                  THEN l.other_id ELSE l.work_item_id END"
            "             WHERE (l.work_item_id = w.id OR l.other_id = w.id)"
            "               AND l.kind = 'conflicts_with' AND b.state NOT IN " + self._core()._CLOSED +
            "             ORDER BY b.id) AS open_conflicts,"
            "       (pd.lot IS NOT NULL) AS pending_decision,"
            # Match the intake footprint guard used by dispatch (#4173).
            # #7940 : une attente humaine de plus de 24 h ne marque plus le
            # lot comme retenu par l'empreinte (même garde que la prise).
            "       (w.state = 'intake' AND " + footprint_overlap_sql(
                "w", items="plan_open_items", start_on=True, blocking_only=True,
                human_wait_ids=human_wait_ids) + ")"
            "         AS footprint_overlap,"
            # #4818 : plafond de lots en vol du dépôt, et ce qu'il faut au plan
            # pour choisir un ensemble aux empreintes disjointes deux à deux
            "       (w.state = 'intake' AND w.assignee IS NULL AND "
            + footprint_cap_sql("w", items="plan_open_items") + ") AS repository_cap,"
            "       ARRAY(SELECT DISTINCT f.path FROM work_item_footprints f"
            "             WHERE f.work_item_id = w.id ORDER BY f.path) AS footprint_paths,"
            "       CASE WHEN w.state = 'intake' THEN " + footprint_in_flight_sql("w", items="plan_open_items") +
            " END AS repository_in_flight,"
            "       EXISTS (SELECT 1 FROM commitments c WHERE c.work_item_id = w.id"
            "               AND c.kind = 'decision' AND c.status IN ('proposed', 'open'))"
            "         AS decision_milestone"
            " FROM plan_open_items w LEFT JOIN work_packages k ON k.id = w.package_id"
            " LEFT JOIN work_packages kp ON kp.id = w.package_parent"
            " LEFT JOIN pending_decisions pd ON pd.lot = w.id::text"
            " WHERE coalesce(nullif(btrim(w.app), ''), nullif(btrim(w.team), ''),"
            "                nullif(btrim(k.team), ''), nullif(btrim(kp.team), ''), '') = %s"
            " AND w.state NOT IN " + self._core()._CLOSED + " AND w.type <> 'improvement'"
            + (" AND (w.id = %s OR (w.type = 'bug' AND w.assignee IS NULL"
               " AND w.state IN ('intake', 'build')))" if assignment_id is not None else "") +
            " ORDER BY w.id", self._core().REQUEUED_PARAMS + (str(project),) +
            ((int(assignment_id),) if assignment_id is not None else ()))
    def issue_feed(self, limit) -> list[dict]:
        """Lots ouverts, porteurs d'une `issue_ref`, ou pinnés sur une révision
        Process (#2130/D : un cœur `merged` reste projeté ouvert, il doit donc
        être dans le flux), les plus récents d'abord pour la borne, rendus par
        id croissant avec l'équipe de leur fiche et l'équipe, le chantier et
        l'hôte de leur assigné.

        #2130/D : `workflow_ref`, `workflow_stage` et `workflow_version` sont
        portés par la lecture comme B5 les porte dans `work.get` (`to_jsonb`,
        sans sonde de colonne) ; base pré-0452 : `''`, NULL, 0.

        #5583 : `merged_ts` se calcule en ensemble. La fenêtre (`ids`) est
        matérialisée une fois ; `merged` agrège les jalons de fusion par lot et
        n'évalue `agent_mesh_merged_annulled` que là où une correction `build`
        postérieure au dernier jalon existe (2 lots sur 5 000 au central) ; la
        liste des créneaux de migration est un agrégat borné aux lots de la
        fenêtre. L'ordre par id croissant est rendu en mémoire : trier 5 000
        identifiants coûte moins que trier 5 000 lignes larges (5 Mo, tri
        externe, diagnostic #5579).
        """
        slots = self.slots_available(self.db)
        reserved = (
            " reserved AS MATERIALIZED ("
            "  SELECT r.work_item_id,"
            "         jsonb_agg(jsonb_build_object('number', r.number,"
            "                                    'repo', r.target_repo, 'slot', r.slot)"
            "                    ORDER BY r.number, r.slot) AS migrations"
            "    FROM work_migration_reservations r"
            "    JOIN ids ON ids.id = r.work_item_id"
            "   WHERE r.status = 'reserved'"
            "   GROUP BY r.work_item_id),") if slots else ""
        reserved_join = (
            " LEFT JOIN reserved res ON res.work_item_id = work_items.id"
            if slots else "")
        migrations_column = ("coalesce(res.migrations, '[]'::jsonb) AS migrations"
                             if slots else "'[]'::jsonb AS migrations")
        rows = self.db.query(
            ("WITH ids AS MATERIALIZED ("
             "  SELECT id FROM work_items"
             "   WHERE state NOT IN %s OR coalesce(btrim(issue_ref), '') <> ''"
             "      OR coalesce(workflow_ref, '') <> ''"
             "   ORDER BY id DESC LIMIT %%s),"
             + reserved +
             " last_merge AS MATERIALIZED ("
             "  SELECT i.id, lm.at"
             "    FROM ids i"
             "   CROSS JOIN LATERAL (SELECT max(m.at) AS at"
             "                         FROM work_item_milestones m"
             "                        WHERE m.work_item_id = i.id"
             "                          AND m.kind = 'merged') lm"
             "   WHERE lm.at IS NOT NULL),"
             " candidates AS MATERIALIZED ("
             "  SELECT lm.id, lm.at"
             "    FROM last_merge lm"
             "   WHERE EXISTS (SELECT 1 FROM work_item_events c"
             "                  WHERE c.work_item_id = lm.id"
             "                    AND c.state = 'build'"
             "                    AND c.created_at >= lm.at)),"
             " annulled AS MATERIALIZED ("
             "  SELECT k.id"
             "    FROM candidates k"
             "   WHERE agent_mesh_merged_annulled(k.id, k.at)),"
             " merged AS MATERIALIZED ("
             "  SELECT lm.id, extract(epoch FROM lm.at)::float8 AS merged_ts"
             "    FROM last_merge lm"
             "   WHERE NOT EXISTS (SELECT 1 FROM annulled a WHERE a.id = lm.id))"
             " SELECT w.*, k.team AS package_team, r.team AS assignee_team,"
             "        r.chantier AS assignee_chantier, r.host AS assignee_host,"
             # L157 : la date de fusion effective, pour l'en-tête de l'issue
             "        mm.merged_ts"
             "   FROM (SELECT %s,"
             "                coalesce(workflow_ref, '') AS workflow_ref,"
             "                workflow_stage,"
             "                coalesce(workflow_version, 0) AS workflow_version,"
             "                to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
             "                to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
             "                to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
             "           FROM work_items" + reserved_join +
             "          WHERE work_items.id IN (SELECT id FROM ids)) w"
             "   LEFT JOIN work_packages k ON k.id = w.package_id"
             "   LEFT JOIN agent_registry r ON r.name = w.assignee"
             "   LEFT JOIN merged mm ON mm.id = w.id")
            % (self._core()._CLOSED, self._core().item_columns(self.db, migrations=migrations_column)),
            (int(limit),))
        # #5583 : l'ordre de sortie de la fenêtre est rendu ici, pas par un
        # tri SQL des lignes larges (5 000 × ~1,6 ko, tri externe).
        rows.sort(key=lambda row: int(row["id"]))
        return rows
    def _intake_footprint(self, item_id, paths, actor) -> list[str]:
        """#4186 : écrit les chemins d'empreinte `intake` absents (union), avec
        leur ligne de journal — le geste commun de `add` et de la file
        d'amélioration. Rend les chemins ajoutés (vide : rien à changer).
        Le verrou du graphe est pris par les appelants qui gardent une création
        attribuée ; ces lots naissent libres."""
        wanted = sorted({str(path) for path in (paths or ()) if str(path).strip()})
        if not wanted:
            return []
        have = {row["path"] for row in self.db.query(
            "SELECT path FROM work_item_footprints"
            " WHERE work_item_id = %s AND source = 'intake'", (int(item_id),))}
        missing = [path for path in wanted if path not in have]
        for path in missing:
            self.db.execute(
                "INSERT INTO work_item_footprints"
                "(work_item_id, source, path, created_by)"
                " VALUES (%s, 'intake', %s, %s) ON CONFLICT DO NOTHING",
                (int(item_id), path, actor or ""))
        if missing:
            self._event(self.db, int(item_id), "intake", "Footprint (intake): %s"
                        % ", ".join(sorted(have | set(missing))), actor)
        return missing
    def measure_candidates(self, *, ids, retry_diff_s, since_s, limit) -> list[dict]:
        """Les lots livrés à mesurer (ou à remesurer), le plus récent d'abord.

        #5411 : sans `ids`, la lecture part des seuls lots qui MANQUENT de
        mesure (ou dont le diff/CI est incomplet), matérialisés une fois
        (145 lots sur la base du 01/11) ; le LATERAL du jalon `merged` ne
        tourne plus pour les 4 289 lots ouverts démarrés (111 931 tampons,
        2,2 s mesurés). Les deux conditions restent celles de #3056/#2317 :
        un lot mesuré sans diff, ou sans CI alors qu'il a une branche, est
        remesuré tant que sa fusion est récente."""
        columns = (
            "w.id, w.state, w.assignee, w.branch, w.branch_target, w.change_size,"
            "       w.issue_ref, w.app,"
            "       extract(epoch from w.started_at)::float8 AS started_ts,"
            "       extract(epoch from fus.at)::float8 AS merged_ts, fus.sha AS merge_sha,"
            "       (m.work_item_id IS NOT NULL) AS measured,"
            "       (m.diff_files IS NOT NULL) AS has_diff,"
            "       (m.ci_minutes IS NOT NULL) AS has_ci,"
            "       (SELECT f.note FROM work_item_milestones f"
            "         WHERE f.work_item_id = w.id AND f.kind = 'frozen'"
            "         ORDER BY f.at DESC, f.id DESC LIMIT 1) AS freeze_note"
        )
        lateral = (
            "  JOIN LATERAL (SELECT x.at, x.sha FROM work_item_milestones x"
            "                 WHERE x.work_item_id = w.id AND x.kind = 'merged'"
            "                   AND " + self._core()._effective_merged("x") +
            "                 ORDER BY x.at DESC, x.id DESC LIMIT 1) fus ON true"
            "  LEFT JOIN work_item_measures m ON m.work_item_id = w.id"
        )
        params: list = []
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return []
            sql = ("SELECT " + columns + " FROM work_items w" + lateral +
                   f" WHERE w.started_at IS NOT NULL AND w.state <> '{lifecycle.CLOSED}'"
                   " AND w.id IN (%s)" % ", ".join(["%s"] * len(ids)))
            params += ids
        else:
            sql = (
                "WITH need AS MATERIALIZED ("
                "  SELECT w.id, w.state, w.assignee, w.branch, w.branch_target,"
                "         w.change_size, w.issue_ref, w.app, w.started_at,"
                "         (m.work_item_id IS NOT NULL) AS measured,"
                "         (m.diff_files IS NOT NULL) AS has_diff,"
                "         (m.ci_minutes IS NOT NULL) AS has_ci"
                "    FROM work_items w"
                "    LEFT JOIN work_item_measures m ON m.work_item_id = w.id"
                f"   WHERE w.started_at IS NOT NULL AND w.state <> '{lifecycle.CLOSED}'"
                "     AND (m.work_item_id IS NULL OR m.diff_files IS NULL"
                "          OR (m.ci_minutes IS NULL AND w.branch IS NOT NULL))"
                ") SELECT n.id, n.state, n.assignee, n.branch, n.branch_target,"
                "         n.change_size, n.issue_ref, n.app,"
                "         extract(epoch from n.started_at)::float8 AS started_ts,"
                "         extract(epoch from fus.at)::float8 AS merged_ts,"
                "         fus.sha AS merge_sha, n.measured, n.has_diff, n.has_ci,"
                "         (SELECT f.note FROM work_item_milestones f"
                "           WHERE f.work_item_id = n.id AND f.kind = 'frozen'"
                "           ORDER BY f.at DESC, f.id DESC LIMIT 1) AS freeze_note"
                "    FROM need n"
                "    JOIN LATERAL (SELECT x.at, x.sha FROM work_item_milestones x"
                "                   WHERE x.work_item_id = n.id AND x.kind = 'merged'"
                "                     AND " + self._core()._effective_merged("x") +
                "                   ORDER BY x.at DESC, x.id DESC LIMIT 1) fus ON true"
                "   WHERE (NOT n.measured"
                "          OR fus.at > now() - make_interval(secs => %s))")
            params.append(float(retry_diff_s))
            if since_s is not None:
                sql += " AND fus.at > now() - make_interval(secs => %s)"
                params.append(float(since_s))
        if ids is not None:
            sql += " ORDER BY fus.at DESC, w.id DESC LIMIT %s"
        else:
            sql += " ORDER BY fus.at DESC, n.id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))
    def save_measure(self, item_id, measure) -> dict:
        m = measure
        rows = self.db.query(
            "INSERT INTO work_item_measures (work_item_id, started_at, merged_at,"
            "  total_minutes, agent_minutes, ci_minutes, review_minutes, human_minutes,"
            "  ci_runs, merge_sha, diff_files, diff_insertions, diff_deletions,"
            "  measured_size, declared_size, agent_source)"
            " VALUES (%s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s,"
            "         %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (work_item_id) DO UPDATE SET measured_at = now(),"
            "  started_at = excluded.started_at, merged_at = excluded.merged_at,"
            + ", ".join(
                # a measure whose CI could not be read never replaces phases
                # measured with a known CI
                "  %s = CASE WHEN excluded.ci_minutes IS NULL"
                " AND work_item_measures.ci_minutes IS NOT NULL"
                " THEN work_item_measures.%s ELSE excluded.%s END" % (c, c, c)
                for c in ("total_minutes", "agent_minutes", "ci_minutes", "review_minutes",
                          "human_minutes", "ci_runs")) + ","
            # même garde pour la provenance des minutes d'agent : garder les
            # phases d'une mesure connue (ci_minutes non nul) garde leur
            # source — 'states' ne devient jamais 'turns' (ni l'inverse) sans
            # que agent_minutes soit recalculé par la même règle
            "  agent_source = CASE WHEN excluded.ci_minutes IS NULL"
            " AND work_item_measures.ci_minutes IS NOT NULL"
            " THEN work_item_measures.agent_source ELSE excluded.agent_source END,"
            "  merge_sha = coalesce(excluded.merge_sha, work_item_measures.merge_sha),"
            # a measure that could not read a size keeps the stored one, unless
            # it doubts for good (`size_doubt`, #3056): the old size is then
            # cleared, not shown as measured
            + ", ".join(
                "  %s = %s" % (c, "NULL" if m.get("size_doubt") else
                               "coalesce(excluded.%s, work_item_measures.%s)" % (c, c))
                for c in ("diff_files", "diff_insertions", "diff_deletions",
                          "measured_size")) + ","
            "  declared_size = excluded.declared_size"
            " RETURNING " + self._MEASURE_COLUMNS,
            (int(item_id), float(m["started_ts"]), float(m["merged_ts"]),
             m["total_minutes"], m["agent_minutes"], m.get("ci_minutes"),
             m["review_minutes"], m["human_minutes"], m.get("ci_runs"), m.get("merge_sha"),
             m.get("diff_files"), m.get("diff_insertions"), m.get("diff_deletions"),
             m.get("measured_size"), m.get("declared_size"),
             m.get("agent_source")))
        return rows[0]
    def measure(self, item_id) -> dict | None:
        rows = self.db.query(
            "SELECT " + self._MEASURE_COLUMNS + " FROM work_item_measures"
            " WHERE work_item_id = %s", (int(item_id),))
        return rows[0] if rows else None
    def _footprint_overlaps(self, item_id, *, free_only=False,
                            start_on=False, blocking_only=True) -> list[dict]:
        """#4173, sous le verrou du graphe : les chevauchements de l'empreinte
        du lot avec celle d'un lot occupant de son dépôt (requête POSTÉRIEURE
        au verrou, comme le contrôle des dépendances). `free_only` : vide si
        le lot occupe déjà son empreinte (réattribution d'un lot démarré).

        #7940 : l'attente humaine est lue par `planned_waits.human_waits` sur
        les occupants rendus (lecture canonique : `active_holds`, routes
        humaines du runtime), jamais par un motif SQL parallèle.
        `blocking_only=True` écarte les occupants qu'elle ne retient plus ;
        `blocking_only=False` les rend avec leur hold lisible
        (`human_hold_note`, `human_hold_ts`), pour l'avertissement des deux
        lots."""
        from .... import planned_waits
        guard = (" AND NOT " + footprint_occupying_sql("c")) if free_only else ""
        rows = self.db.query(
            "SELECT DISTINCT x.* FROM work_items c, LATERAL ("
            + footprint_overlap_rows_sql("c", start_on=start_on)
            + ") x WHERE c.id = %s" + guard +
            " ORDER BY x.id, x.path", (int(item_id),))
        waits = planned_waits.human_waits(
            self.db, [int(row["id"]) for row in rows])
        packets = footprint_open_packet_members(self.db, waits)
        out = []
        for row in rows:
            ident = int(row["id"])
            hold = waits.get(ident) if ident not in packets else None
            row = dict(row)
            row["human_wait"] = hold is not None
            if hold is not None:
                row["human_hold_note"] = hold["note"]
                row["human_hold_ts"] = hold["since"]
            if blocking_only and hold is not None:
                continue
            out.append(row)
        return out
    def _footprint_cap(self, item_id, *, free_only=False) -> dict | None:
        """#4818, sous le verrou du graphe : `{cap, in_flight}` si le dépôt du
        lot a atteint son plafond de lots en vol, sinon None ; `free_only` :
        None si le lot occupe déjà son empreinte (il est déjà compté)."""
        guard = (" AND NOT " + footprint_occupying_sql("c")) if free_only else ""
        rows = self.db.query(
            "SELECT x.* FROM work_items c, LATERAL (" + footprint_cap_rows_sql("c")
            + ") x WHERE c.id = %s" + guard, (int(item_id),))
        return dict(rows[0]) if rows else None

    def start_blocked(self, item_id) -> bool:
        """#7760 : une dépendance retient-elle le démarrage du lot ?

        Même prédicat que `unassigned_lots` (`start_on` écarté), exécuté sur
        la connexion du module : le SQL des empreintes y est qualifié (revue
        B1 de #7650). Les gardes de prise l'appellent sous le verrou du graphe.
        """
        return bool(self.db.query(
            "SELECT 1 FROM work_item_dependencies d"
            " JOIN work_items b ON b.id = d.blocker_id"
            " JOIN work_items i ON i.id = d.work_item_id"
            " WHERE d.work_item_id = %s AND " + footprint_start_blocking("i", "b")
            + " LIMIT 1", (int(item_id),)))

    def unknown_policy(self, item_id, db=None) -> str:
        """#4818 : la politique d'empreinte inconnue du lot.

        `db` : la transaction de l'appelant quand le noyau garde la
        transaction (création, intake), sinon la connexion du domaine. Le SQL
        nomme `repository_footprint_policies` et `work_item_footprints` —
        tables du module, donc qualifiées ici (revue B de #7650).
        """
        return footprint_unknown_policy_sql(
            self.db if db is None else ModuleDb(db), item_id)

    def start_on_pairs(self, item_ids) -> set[tuple[int, int]]:
        """#7760 : les couples (dépendant, prérequis) en « démarrage sur ».

        Lecture qualifiée pour `work show` (`WorkDependencies.for_items`) :
        hors du module, le SQL des empreintes ne serait pas résolu après P2
        (revue B1 de #7650).
        """
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return set()
        rows = self.db.query(
            "SELECT d.work_item_id, d.blocker_id"
            "  FROM work_item_dependencies d"
            "  JOIN work_items w ON w.id = d.blocker_id"
            "  JOIN work_items i ON i.id = d.work_item_id"
            " WHERE d.work_item_id IN (" + ", ".join(["%s"] * len(ids)) + ")"
            "   AND " + footprint_start_on_sql("i", "w"),
            tuple(ids))
        return {(int(row["work_item_id"]), int(row["blocker_id"]))
                for row in rows}

    def unassigned_lots(self, limit) -> list[dict]:
        # #2252 : seuls les lots dont TOUTES les dépendances sont fusionnées
        # (`merged`/`promoted`/`closed`) sont prenables. La règle est celle de
        # `work show` (`depends … blocking`) et de « planned: waiting on lots » :
        # une dépendance en `qa`, même jugée, n'offre pas de base stable.
        # #7940 : même attente humaine canonique que la prise, lue une fois.
        from .... import planned_waits
        human_wait_ids = tuple(planned_waits.human_waits(self.db))
        # #8051 : les occupants sont matérialisés UNE fois pour toute la
        # sélection. Sans cette CTE, chaque candidat refaisait le parcours des
        # ~7 500 lots ouverts (plan mesuré : 16 ms par candidat) ; les colonnes
        # sont celles que lisent `occupying`/`exempt`/le plafond.
        sql = ("WITH open_items AS MATERIALIZED ("
               " SELECT id, type, app, state, assignee, source, branch FROM work_items"
               "  WHERE state NOT IN " + self._core()._CLOSED + ") "
               "SELECT w.*,"
               f"       (w.state = '{lifecycle.IN_PROGRESS}' AND " + self._core().requeued_sql("w") + ") AS returned_to_queue,"
               "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
               "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
               "       EXISTS (SELECT 1 FROM agent_mailbox b"
               "                WHERE b.delivered_at IS NULL AND b.kind = 'request'"
               "                  AND (b.payload -> 'decision') IS NOT NULL"
               "                  AND btrim(b.work_item_id) = w.id::text) AS pending_decision,"
               "       EXISTS (SELECT 1 FROM commitments c"
               "                WHERE c.work_item_id = w.id AND c.kind = 'decision'"
               "                  AND c.status IN ('proposed', 'open')) AS decision_milestone"
               "  FROM (SELECT " + self._core().item_columns(self.db) + ","
               "               to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
               "               to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
               "               to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
               "          FROM work_items"
               f"         WHERE (state = '{lifecycle.REQUESTED}' OR (state = '{lifecycle.IN_PROGRESS}' AND "
               + self._core().requeued_sql("work_items") + "))"
               "           AND assignee IS NULL"
               # #3447 : un lot réservé à un exécutant externe n'est pas
               # prenable (le filtre précède le LIMIT, comme les dépendances).
               "           AND to_jsonb(work_items)->>'external_executor' IS NULL"
               "           AND type <> 'improvement'"
               "           AND NOT EXISTS (SELECT 1 FROM work_item_dependencies d"
               "                             JOIN work_items b ON b.id = d.blocker_id"
               "                            WHERE d.work_item_id = work_items.id"
               "                              AND " + footprint_start_blocking("work_items", "b") + ")"
               # #4173 : un lot `intake` dont l'empreinte chevauche celle d'un
               # lot occupant du même dépôt attend sa fusion (un lot `build`
               # rendu à la file occupe déjà la sienne) ; même prédicat que la
               # garde de `_assign_locked`, avant le LIMIT.
               # #4818 : et le plafond de lots en vol de son dépôt (`held_sql`).
               f"           AND (state <> '{lifecycle.REQUESTED}' OR NOT "
               + footprint_held_sql("work_items", items="open_items",
                                    start_on=True,
                                    human_wait_ids=human_wait_ids) + ")) w"
               "  LEFT JOIN work_packages k ON k.id = w.package_id"
               "  LEFT JOIN work_packages kp ON kp.id = w.package_parent"
               f" ORDER BY coalesce(w.priority, 2), (w.state = '{lifecycle.IN_PROGRESS}') DESC,"
               "          w.planned_start NULLS LAST,"
               "          w.planned_end NULLS LAST, w.planned_delivery NULLS LAST,"
               "          w.created_ts, w.id"
               " LIMIT %s")
        # les deux fragments attendent les mêmes paramètres, dans l'ordre du texte
        return self.db.query(sql, self._core().REQUEUED_PARAMS + self._core().REQUEUED_PARAMS +
                             (None if limit is None else int(limit),))
