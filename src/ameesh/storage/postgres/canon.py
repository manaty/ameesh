# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : écritures de `canon sync` et agents éphémères (§4.4, R14).

SQL déplacé tel quel depuis `ameesh.canon_sync` (lot L1) : état du canon par
hôte (`canon_state`, 0008, 0024), colonnes déclaratives et verdicts de
placement (0022 : profil évalué calculé par `ameesh_placement_profile` dans
la même instruction), arrêt et réintégration, création atomique d'un
éphémère depuis la ligne de son créateur.
"""
from __future__ import annotations

import json

from .. import interface
from .packages import WorkPackages
from .placement import PROFILE_PARAMS_SQL, profile_params, profile_sql
from .registry import NAME_LOCK_CTE, name_lock_key

_CAPS_SQL = ("CASE WHEN %s::text IS NULL THEN NULL "
             "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END")


def _caps_param(capabilities) -> str | None:
    return None if capabilities is None else json.dumps(list(capabilities))


#: L42 (0031) : la ligne appartient-elle au canon (id, défaut) ? NULL = canon
#: par défaut. Garde des écritures destructrices de `canon sync`.
_OWNER_SQL = " AND (canon = %s OR (canon IS NULL AND %s))"


def _owner(owner) -> tuple[str, tuple]:
    """(clause, paramètres) de la garde de propriété ; owner None : aucune garde."""
    if owner is None:
        return "", ()
    ident, default = owner
    return _OWNER_SQL, (ident, bool(default))


class Canon(interface.Canon):

    def record_state(self, host, status, *, root, source, commit, good,
                     diagnostic, canon="", canon_id=None,
                     expected=None) -> str | None:
        # #3955 : `expected` rend l'avance du commit audité conditionnelle.
        # `None` : sans condition (comportement historique, écritures non-`ok`) ;
        # une chaîne ('' comprise) : la valeur enregistrée doit encore être
        # celle-là, sinon le commit ET sa provenance restent ceux du gagnant.
        # #3964 : la provenance (statut, racine, source, diagnostic, horodatage)
        # suit le même CAS — une écriture fondée sur une lecture périmée ne
        # laisse pas une provenance périmée derrière un commit plus récent.
        # Le CAS, dans la même instruction que l'upsert, ferme la course entre
        # deux synchronisations (l'une lit l'ancien commit avant le fetch et
        # écrit après que l'autre a enregistré le nouveau).
        rows = self.db.query(
            """
            INSERT INTO canon_state AS s
                (host, canon, canon_id, status, root, source, last_good_commit, last_good_at,
                 diagnostic, checked_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, now())
            ON CONFLICT (host, canon) DO UPDATE SET
                canon_id         = coalesce(excluded.canon_id, s.canon_id),
                status           = CASE WHEN excluded.status <> 'ok'
                                              OR %s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s
                                        THEN excluded.status ELSE s.status END,
                root             = CASE WHEN excluded.status <> 'ok'
                                              OR %s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s
                                        THEN excluded.root ELSE s.root END,
                source           = CASE WHEN excluded.status <> 'ok'
                                              OR %s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s
                                        THEN excluded.source ELSE s.source END,
                last_good_commit = CASE WHEN excluded.status = 'ok'
                                         AND (%s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s)
                                        THEN excluded.last_good_commit
                                        ELSE s.last_good_commit END,
                last_good_at     = CASE WHEN excluded.status = 'ok'
                                         AND (%s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s)
                                        THEN excluded.last_good_at
                                        ELSE s.last_good_at END,
                diagnostic       = CASE WHEN excluded.status <> 'ok'
                                              OR %s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s
                                        THEN excluded.diagnostic ELSE s.diagnostic END,
                checked_at       = CASE WHEN excluded.status <> 'ok'
                                              OR %s::boolean
                                              OR coalesce(s.last_good_commit, '') = %s
                                        THEN excluded.checked_at ELSE s.checked_at END
            RETURNING last_good_commit
            """,
            (host, canon or "", canon_id, status, root, source, commit, good, diagnostic,
             expected is None, expected or "",      # status
             expected is None, expected or "",      # root
             expected is None, expected or "",      # source
             expected is None, expected or "",      # last_good_commit
             expected is None, expected or "",      # last_good_at
             expected is None, expected or "",      # diagnostic
             expected is None, expected or ""),     # checked_at
        )
        return rows[0]["last_good_commit"] if rows else None

    _STATE_COLUMNS = """
        host, canon, canon_id, status, root, source, last_good_commit, process_commit, diagnostic,
        extract(epoch from last_good_at)::float8 AS last_good_ts,
        extract(epoch from checked_at)::float8   AS checked_ts,
        auth_status, auth_diagnostic,
        extract(epoch from auth_checked_at)::float8 AS auth_checked_ts
    """

    def state(self, host, canon="") -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM canon_state WHERE host = %%s AND canon = %%s" % self._STATE_COLUMNS,
            (host, canon or ""),
        )
        return rows[0] if rows else None

    def states(self, host) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM canon_state WHERE host = %%s ORDER BY canon" % self._STATE_COLUMNS,
            (host,))

    def claim_commit(self, host, old, new, canon="") -> bool:
        rows = self.db.query(
            "UPDATE canon_state SET process_commit = %s "
            "WHERE host = %s AND canon = %s"
            " AND coalesce(process_commit, last_good_commit, '') = %s RETURNING host",
            (new, host, canon or "", old))
        return bool(rows)

    def key_for_root(self, host, root) -> str | None:
        rows = self.db.query(
            "SELECT canon FROM canon_state WHERE host = %s AND root = %s AND canon <> '' "
            "ORDER BY checked_at DESC LIMIT 1", (host, root))
        return rows[0]["canon"] if rows else None

    def record_auth_state(self, host, status, diagnostic, canon="", *,
                          quiet_prefix="") -> dict | None:
        # The previous state and the transient-error promotion belong to the
        # same row lock. A second write after returning would reopen the race.
        rows = self.db.query(
            """WITH previous AS MATERIALIZED (
                SELECT host, canon, auth_status, auth_diagnostic
                  FROM canon_state WHERE host = %s AND canon = %s FOR UPDATE
            )
                UPDATE canon_state s SET auth_status = %s,
                    auth_diagnostic = CASE WHEN %s <> ''
                        AND previous.auth_status IS DISTINCT FROM 'error'
                        THEN left(%s || %s, 4000) ELSE %s END,
                    auth_checked_at = now()
                  FROM previous
                 WHERE s.host = previous.host AND s.canon = previous.canon
                RETURNING previous.auth_status, previous.auth_diagnostic""",
            (host, canon or "", status, quiet_prefix, quiet_prefix, diagnostic, diagnostic))
        return rows[0] if rows else None

    def close_others(self, host, keep, diagnostic) -> list[str]:
        rows = self.db.query(
            """
            UPDATE canon_state SET status = 'invalid', diagnostic = %s, checked_at = now()
             WHERE host = %s AND status = 'ok'
               AND NOT (canon = ANY(ARRAY(SELECT jsonb_array_elements_text(%s::jsonb))))
            RETURNING canon
            """, (diagnostic, host, json.dumps(list(keep))))
        return sorted(r["canon"] for r in rows)

    def claim_sync(self, host, canon, token, interval_s) -> bool:
        rows = self.db.query(
            """INSERT INTO canon_state AS s
                (host, canon, status, sync_claimed_at, sync_token)
                VALUES (%s, %s, 'unreadable', clock_timestamp(), %s)
                ON CONFLICT (host, canon) DO UPDATE SET
                    sync_claimed_at = clock_timestamp(), sync_token = excluded.sync_token
                WHERE s.sync_claimed_at IS NULL OR
                    s.sync_claimed_at <= clock_timestamp() - make_interval(secs => %s)
                RETURNING host""", (host, canon, token, float(interval_s)))
        return bool(rows)

    def complete_sync(self, host, canon, token, signature) -> None:
        self.db.execute(
            "UPDATE canon_state SET sync_completed_token = %s, sync_signature = %s "
            "WHERE host = %s AND canon = %s AND sync_token = %s",
            (token, signature, host, canon, token))

    def shared_sync(self, host, canon) -> dict | None:
        rows = self.db.query(
            "SELECT status, diagnostic, sync_signature FROM canon_state "
            "WHERE host = %s AND canon = %s AND sync_token = sync_completed_token",
            (host, canon))
        return rows[0] if rows else None

    def lock_default_state(self, host) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM canon_state WHERE host = %%s AND canon = '' FOR UPDATE"
            % self._STATE_COLUMNS, (host,))
        return rows[0] if rows else None

    def set_default_id(self, host, canon_id) -> None:
        self.db.execute(
            "UPDATE canon_state SET canon_id = %s WHERE host = %s AND canon = ''",
            (canon_id, host))

    def flag_default(self, host, status, diagnostic) -> None:
        self.db.execute(
            "UPDATE canon_state SET status = %s, diagnostic = %s, checked_at = now() "
            "WHERE host = %s AND canon = ''", (status, diagnostic, host))

    def rebase_default(self, host, previous, new_default="") -> dict:
        """Le canon par défaut de l'hôte a changé (L42) : les lignes sans canon
        (NULL) de l'ancien canon par défaut `previous` reçoivent son
        identifiant, et son état est recopié sous sa propre clé s'il n'y en a
        pas. Rien n'est arrêté ni effacé.

        L44 (0031) : le canon des actions (`actions.canon`, '' = canon par
        défaut) suit de même — '' passe à `previous`, puis `new_default` (id
        du nouveau canon par défaut) passe à '' : un reçu d'une action garde
        les authentificateurs de son canon.

        #1464 (N2 #793) : la copie emporte `process_commit`, le repère d'envoi
        des règles Process. Sans lui, l'état recopié retombait sur
        `last_good_commit`, plus ancien, et les règles déjà poussées
        repartaient une fois."""
        # L46 (0036) : `actions.canon` est immuable sauf pendant CE rebase ; le
        # réglage ne vaut que pour la transaction de l'appelant (SET LOCAL),
        # que `adopt_default` ouvre.
        # #831 : la bascule écrit `work_packages.canon` ; sans le verrou de
        # synchronisation, un sync du canon par défaut parti avant elle pouvait
        # réécrire NULL après. Même verrou que `canon sync` (par schéma).
        WorkPackages(self.db).lock_sync()
        self.db.execute("SET LOCAL ameesh.rebase_default = 'on'")
        moved_actions = self.db.query(
            "UPDATE actions SET canon = %s WHERE canon = '' RETURNING action_id", (previous,))
        if new_default and new_default != previous:
            self.db.query("UPDATE actions SET canon = '' WHERE canon = %s RETURNING action_id",
                          (new_default,))
        agents = self.db.query(
            "UPDATE agent_registry SET canon = %s, updated_at = now() "
            "WHERE canon IS NULL AND host = %s AND (canon_ref IS NOT NULL OR ephemeral) "
            "RETURNING name", (previous, host))
        packages = self.db.query(
            "UPDATE work_packages SET canon = %s WHERE canon IS NULL RETURNING id",
            (previous,))
        adopted: list = []
        if new_default and new_default != previous:
            # L46 : les lignes du NOUVEAU canon par défaut repassent à NULL —
            # ses éphémères surtout, que sa synchronisation ne réécrit pas
            # (sinon elles resteraient sous une clé fermée par
            # `close_unconfigured`, non réclamables).
            adopted = self.db.query(
                "UPDATE agent_registry SET canon = NULL, updated_at = now() "
                "WHERE canon = %s AND host = %s RETURNING name", (new_default, host))
            self.db.query("UPDATE work_packages SET canon = NULL WHERE canon = %s "
                          "RETURNING id", (new_default,))
        self.db.query(
            """
            INSERT INTO canon_state
                (host, canon, canon_id, status, root, source, last_good_commit,
                 process_commit, last_good_at, diagnostic, checked_at, auth_status,
                 auth_diagnostic, auth_checked_at)
            SELECT host, %s::text, %s::text, status, root, source, last_good_commit,
                   process_commit, last_good_at, diagnostic, checked_at, auth_status,
                   auth_diagnostic, auth_checked_at
              FROM canon_state WHERE host = %s AND canon = ''
            ON CONFLICT (host, canon) DO NOTHING
            RETURNING host
            """, (previous, previous, host))
        return {"agents": sorted(r["name"] for r in agents),
                "packages": sorted(r["id"] for r in packages),
                "actions": len(moved_actions),
                "adopted": sorted(r["name"] for r in adopted)}

    def host_rows(self, host, names) -> list[dict]:
        sql = """
            SELECT name, harness, host, cwd, model, budget_usd, responsible, team, projects,
                   aliases, roles, releve, candidats, relit,
                   provider, credential_mode, capabilities, canon_ref, canon, ephemeral, priority,
                   admitted_hosts, admitted_tags, memory_repository,
                   visibility_ok, visibility_diagnostic,
                   status, status_text, lease_epoch,
                   placement_ok, placement_diagnostic, placement_ref,
                   placement_profile IS NOT DISTINCT FROM __PROFILE__ AS profile_fresh,
                   (status = 'running' AND lease_expires_at > now()) AS in_turn
              FROM agent_registry
             WHERE host = %s
        """.replace("__PROFILE__", profile_sql())
        params: list = [host]
        if names:
            sql += " OR name = ANY(ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)))"
            params.append(json.dumps(names))
        return self.db.query(sql, tuple(params))

    def lot_projects(self) -> list[str]:
        return [row["app"] for row in self.db.query(
            "SELECT DISTINCT app FROM work_items WHERE coalesce(app, '') <> '' ORDER BY app")]

    def write_declared(self, name, values) -> bool:
        """Le profil évalué (`placement_profile`) est celui des valeurs écrites
        (celles que `placement.evaluate` a jugées), calculé par
        `ameesh_placement_profile` dans la même instruction. False signifie
        que le CAS a refusé l'écriture : aucune politique n'a été appliquée."""
        caps = _caps_param(values["capabilities"])
        ahosts = _caps_param(values.get("admitted_hosts"))
        atags = _caps_param(values.get("admitted_tags"))
        projects = _caps_param(values.get("projects"))
        aliases = _caps_param(values.get("aliases"))
        roles = _caps_param(values.get("roles"))
        relit = _caps_param(values.get("relit"))
        # L165 (EP1) : relève et hôtes candidats, déclaratifs comme le placement
        candidats = _caps_param(values.get("candidats"))
        rows = self.db.query(
            """
            __LOCK__
            INSERT INTO agent_registry
                (name, harness, host, cwd, model, budget_usd, responsible, team, provider,
                 credential_mode, capabilities, canon_ref, ephemeral, ephemeral_expires_at,
                 priority, admitted_hosts, admitted_tags, memory_repository,
                 visibility_ok, visibility_diagnostic,
                 placement_ok, placement_diagnostic, placement_ref, placement_profile, canon,
                 releve, candidats, projects, aliases, roles, relit)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, __CAPS__, %s, false, NULL,
                   %s, __AHOSTS__, __ATAGS__, %s, %s, %s, %s, %s, %s, __PROFILE__, %s,
                   %s, __CANDIDATS__, __PROJECTS__, __ALIASES__, __ROLES__, __RELIT__
              FROM l
            ON CONFLICT (name) DO UPDATE SET
                harness         = excluded.harness,
                host            = excluded.host,
                cwd             = excluded.cwd,
                model           = excluded.model,
                budget_usd      = excluded.budget_usd,
                responsible     = excluded.responsible,
                team            = excluded.team,
                projects        = excluded.projects,
                aliases         = excluded.aliases,
                roles           = excluded.roles,
                relit           = excluded.relit,
                provider        = excluded.provider,
                credential_mode = excluded.credential_mode,
                capabilities    = excluded.capabilities,
                canon_ref       = excluded.canon_ref,
                ephemeral       = false,
                ephemeral_expires_at = NULL,
                priority        = excluded.priority,
                admitted_hosts  = excluded.admitted_hosts,
                admitted_tags   = excluded.admitted_tags,
                memory_repository = excluded.memory_repository,
                visibility_ok   = excluded.visibility_ok,
                visibility_diagnostic = excluded.visibility_diagnostic,
                placement_ok    = excluded.placement_ok,
                placement_diagnostic = excluded.placement_diagnostic,
                placement_ref   = excluded.placement_ref,
                placement_profile = excluded.placement_profile,
                canon           = excluded.canon,
                releve          = excluded.releve,
                candidats       = excluded.candidats,
                updated_at      = now()
            WHERE agent_registry.lease_epoch = %s
              AND (excluded.host = agent_registry.host
                   OR NOT EXISTS (SELECT 1 FROM host_handoffs h WHERE h.agent=agent_registry.name
                                  AND h.state NOT IN ('verified','cancelled')))
            RETURNING name
            """.replace("__LOCK__", NAME_LOCK_CTE)
               .replace("__CAPS__", _CAPS_SQL)
               .replace("__AHOSTS__", _CAPS_SQL)
               .replace("__ATAGS__", _CAPS_SQL)
               .replace("__CANDIDATS__", _CAPS_SQL)
               .replace("__PROJECTS__", _CAPS_SQL)
               .replace("__ALIASES__", _CAPS_SQL)
               .replace("__ROLES__", _CAPS_SQL)
               .replace("__RELIT__", _CAPS_SQL)
               .replace("__PROFILE__", PROFILE_PARAMS_SQL),
            (name_lock_key(name),
             name, values["harness"], values["host"], values["cwd"], values["model"],
             values["budget_usd"], values["responsible"], values["team"], values["provider"],
             values["credential_mode"], caps, caps, values["canon_ref"],
             int(values.get("priority") or 0), ahosts, ahosts, atags, atags,
             values.get("memory_repository"),
             values.get("visibility_ok"), values.get("visibility_diagnostic") or "",
             values["placement_ok"], values["placement_diagnostic"], values["placement_ref"])
            + profile_params(values) + (values.get("canon"), values.get("releve") or "hote",
                                       candidats, candidats, projects, projects, aliases, aliases,
                                       roles, roles,
                                       relit, relit,
                                       values.get("_expected_lease_epoch", values.get("lease_epoch", -1))),
        )
        return bool(rows)

    def set_election(self, name, releve, candidats, *, canon) -> None:
        """L165 (EP1) : `releve` / `candidats` et ménage des verdicts candidats.

        Une ligne qui redevient `hote`, ou dont la liste de candidats se
        resserre, ne garde pas de verdict pour un hôte qui n'est plus candidat
        (le prédicat exige aussi l'appartenance, mais un verdict périmé ne doit
        pas pouvoir resservir si l'hôte redevient candidat plus tard)."""
        candidates = _caps_param(candidats)
        #: liste vide pour le DELETE (ANY(NULL) ne supprimerait rien) ; NULL
        #: pour la colonne (un agent `hote` n'a pas de liste, pas `[]`).
        empty = ("CASE WHEN %s::text IS NULL THEN ARRAY[]::text[] "
                 "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END")
        nullable = ("CASE WHEN %s::text IS NULL THEN NULL "
                    "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END")
        self.db.execute(
            "DELETE FROM agent_candidates WHERE name = %s AND canon = %s"
            " AND (%s <> 'election' OR NOT (host = ANY(" + empty + ")))",
            (name, canon, releve, candidates, candidates))
        self.db.execute(
            """
            UPDATE agent_registry
               SET releve = %s, candidats = """ + nullable + """, updated_at = now()
             WHERE name = %s AND coalesce(canon, '') = %s
               AND (releve IS DISTINCT FROM %s OR candidats IS DISTINCT FROM """ + nullable + """)
            """,
            (releve, candidates, candidates, name, canon, releve, candidates, candidates))

    def write_candidate(self, name, host, *, canon, cwd, ok, diagnostic, ref,
                        harness, provider, model, credential_mode) -> None:
        """L165 (EP1) : verdict de placement de `name` sur l'hôte candidat.

        Le profil évalué est calculé ici, pour CET hôte : la réclamation depuis
        un autre candidat compare le sien, donc un profil divergé ferme la
        réclamation comme pour un agent épinglé."""
        self.db.execute(
            """
            INSERT INTO agent_candidates
                (name, host, canon, cwd, placement_ok, placement_diagnostic,
                 placement_ref, placement_profile, updated_at)
            SELECT %s, %s, %s, %s, %s, %s, %s,
                   ameesh_placement_profile(%s, %s, %s, %s, %s), now()
            ON CONFLICT (name, host) DO UPDATE SET
                canon = excluded.canon,
                cwd = excluded.cwd,
                placement_ok = excluded.placement_ok,
                placement_diagnostic = excluded.placement_diagnostic,
                placement_ref = excluded.placement_ref,
                placement_profile = excluded.placement_profile,
                updated_at = now()
            """,
            (name, host, canon, cwd, ok, diagnostic or "", ref,
             host, harness, provider, model, credential_mode))

    def candidate_names(self, host, canon="") -> list[str]:
        rows = self.db.query(
            "SELECT name FROM agent_candidates WHERE host = %s AND canon = %s"
            " ORDER BY name", (host, canon))
        return [row["name"] for row in rows]

    def close_candidates(self, name, *, canon, diagnostic) -> None:
        self.db.execute(
            """
            UPDATE agent_candidates
               SET placement_ok = false, placement_diagnostic = %s,
                   updated_at = now()
             WHERE name = %s AND canon = %s
               AND (placement_ok IS DISTINCT FROM false
                    OR placement_diagnostic IS DISTINCT FROM %s)
            """, (diagnostic, name, canon, diagnostic))

    def clear_candidate(self, name, host, *, canon) -> None:
        self.db.execute(
            "DELETE FROM agent_candidates WHERE name = %s AND host = %s AND canon = %s",
            (name, host, canon))

    def clear_responsible(self, name, host, owner=None) -> None:
        guard, params = _owner(owner)
        self.db.execute(
            "UPDATE agent_registry SET responsible = NULL, updated_at = now() "
            "WHERE name = %s AND host = %s AND NOT ephemeral AND responsible IS NOT NULL"
            + guard, (name, host) + params)

    def set_placement(self, name, host, *, ok, diagnostic, ref, profile) -> None:
        self.db.execute(
            """
            UPDATE agent_registry
               SET placement_ok = %s, placement_diagnostic = %s, placement_ref = %s,
                   placement_profile = coalesce(%s::text, __PROFILE__),
                   updated_at = now()
             WHERE name = %s AND host = %s
               AND (placement_ok, placement_diagnostic, placement_ref, placement_profile)
                   IS DISTINCT FROM (%s::boolean, %s::text, %s::text,
                                     coalesce(%s::text, __PROFILE__))
            """.replace("__PROFILE__", profile_sql()),
            (ok, diagnostic, ref, profile, name, host,
             ok, diagnostic, ref, profile))

    def lineage_rows(self) -> list[dict]:
        return self.db.query(
            """
            SELECT name, created_by, ephemeral, canon_ref, canon, host,
                   placement_ok, placement_diagnostic, placement_ref, placement_profile,
                   __PROFILE__ AS profile_now
              FROM agent_registry
             WHERE ephemeral
                OR name IN (SELECT created_by FROM agent_registry
                             WHERE ephemeral AND created_by IS NOT NULL)
            """.replace("__PROFILE__", profile_sql())
        )

    def revive(self, name, text, stop_mark) -> bool:
        revived = self.db.query(
            """
            UPDATE agent_registry
               SET status = 'idle', status_text = %s, updated_at = now()
             WHERE name = %s AND status = 'stopped' AND starts_with(status_text, %s)
            RETURNING name
            """,
            (text, name, stop_mark),
        )
        return bool(revived)

    def clear_pending_stop(self, name, pending_mark) -> None:
        self.db.execute(
            "UPDATE agent_registry SET status_text = '' "
            "WHERE name = %s AND starts_with(status_text, %s)",
            (name, pending_mark))

    def move_host(self, name, host, *, target, canon_ref, diagnostic, ref) -> None:
        self.db.execute(
            """
            UPDATE agent_registry
               SET host = %s, responsible = NULL, canon_ref = %s,
                   placement_ok = NULL, placement_diagnostic = %s, placement_ref = %s,
                   placement_profile = NULL,
                   updated_at = now()
             WHERE name = %s AND host = %s
               AND NOT EXISTS (SELECT 1 FROM host_handoffs h WHERE h.agent=agent_registry.name
                               AND h.state NOT IN ('verified','cancelled'))
            """,
            (target, canon_ref, diagnostic, ref, name, host))

    def relocate(self, name, host, *, target, canon_ref, diagnostic, ref,
                 keep_session, owner, epoch, summary="") -> dict | None:
        """Déplacement d'exécution entre deux hôtes admis (L31, 0028).

        Fencé par le bail : verrou de ligne d'abord, puis, DANS l'écriture, le
        bail doit être vivant et détenu par `owner`/`epoch`, l'agent ne doit
        pas être en tour, et `target` doit encore figurer dans ses hôtes admis."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, host, status, admitted_hosts, lease_owner, lease_epoch,
                       lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET host = %s, responsible = NULL, canon_ref = %s,
                   placement_ok = NULL, placement_diagnostic = %s::text,
                   placement_ref = %s::text,
                   placement_profile = NULL,
                   session_id = CASE WHEN %s THEN r.session_id ELSE NULL END,
                   session_work_item = CASE WHEN %s THEN r.session_work_item ELSE NULL END,
                   session_reset_at = CASE WHEN %s THEN r.session_reset_at
                                           ELSE clock_timestamp() END,
                   pending_prompt = CASE
                       WHEN %s OR coalesce(%s::text, '') = '' THEN r.pending_prompt
                       ELSE concat_ws(E'\\n\\n', %s::text, r.pending_prompt) END,
                   status = CASE WHEN %s THEN r.status ELSE 'queued' END,
                   status_text = %s::text,
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.host = %s
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND verrou.status NOT IN ('running', 'stopped')
               AND NOT ameesh_handoff_fenced(r.name)
               AND NOT EXISTS (SELECT 1 FROM host_handoffs h WHERE h.agent=r.name
                               AND h.state NOT IN ('verified','cancelled'))
               AND %s = ANY(verrou.admitted_hosts)
               -- #254 : rien ne part vers un hôte en pause
               AND NOT EXISTS (SELECT 1 FROM host_holds hh
                                WHERE hh.host = %s AND hh.until > clock_timestamp())
             RETURNING r.name, r.host
            """,
            (name, target, canon_ref, diagnostic, ref, bool(keep_session),
             bool(keep_session), bool(keep_session), bool(keep_session), summary, summary,
             bool(keep_session), "moved to %s" % target,
             host, owner, int(epoch), target, target),
        )
        return rows[0] if rows else None

    def relocate_operator(self, name, host, *, target, canon_ref, diagnostic, ref,
                          keep_session, runner_stale_s, summary="",
                          source_stopped=False) -> dict | None:
        """Déplacement manuel (#3299), sans bail vivant exigé.

        Le verrou de ligne est pris d'abord, puis l'écriture recontrôle : hôte
        de départ inchangé, agent `releve: hote` et hors tour, aucun handoff
        ouvert, destination encore admise et sans `host_hold`. Le bail n'est
        pas exigé quand l'exécuteur de départ est arrêté : `runner_seen_at`
        périmé (horloge de la base), relevé sur un autre hôte, ou
        `source_stopped` (unité de l'exécuteur inactive, prouvée par
        l'appelant). Un bail vivant et un exécuteur frais refusent : on ne
        dépossède pas un exécuteur qui sert encore l'agent.

        `source_stopped` n'est retenu que si le dernier relevé vient de
        l'unité DE CET agent (`runner_seen_owner` = `runner-<nom>@…`) : le
        gabarit systemd `ameesh-runner-agent@<nom>.service` répond
        `loaded`/`inactive` pour n'importe quel nom jamais instancié, et un
        agent mené par une autre unité (coordinateur partagé) garderait sinon
        un bail vivant (B de la relecture #3299).

        Le bail de la source est révoqué dans cette même écriture (owner et
        échéance vidés, époque +1), `keep_session` compris : `renew` et
        `begin_turn` ne contrôlent que owner/epoch, jamais l'hôte, donc un
        exécuteur de départ laissé vivant garderait l'agent (B1 #3299).
        """
        stale = float(runner_stale_s)
        if not stale > 0:
            raise ValueError("relocate_operator: runner_stale_s must be positive")
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, host, status, releve, admitted_hosts, lease_owner,
                       lease_epoch, lease_expires_at, runner_seen_at, runner_seen_host,
                       runner_seen_owner
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET host = %s,
                   -- un agent du canon verra son responsable re-résolu par le
                   -- sync d'arrivée ; un agent inscrit à la main le garde
                   responsible = CASE WHEN coalesce(r.canon_ref, '') = ''
                                      THEN r.responsible ELSE NULL END,
                   canon_ref = %s,
                   placement_ok = NULL, placement_diagnostic = %s::text,
                   placement_ref = %s::text,
                   placement_profile = NULL,
                   session_id = CASE WHEN %s THEN r.session_id ELSE NULL END,
                   session_work_item = CASE WHEN %s THEN r.session_work_item ELSE NULL END,
                   session_reset_at = CASE WHEN %s THEN r.session_reset_at
                                           ELSE clock_timestamp() END,
                   pending_prompt = CASE
                       WHEN %s OR coalesce(%s::text, '') = '' THEN r.pending_prompt
                       ELSE concat_ws(E'\\n\\n', %s::text, r.pending_prompt) END,
                   -- un arrêté reste arrêté (son motif d'arrêt est conservé) ;
                   -- sinon l'agent repart réclamable sur l'hôte d'arrivée
                   status = CASE WHEN %s THEN r.status
                                 WHEN r.status = 'stopped' THEN r.status
                                 ELSE 'queued' END,
                   status_text = CASE WHEN r.status = 'stopped' THEN r.status_text
                                      ELSE %s::text END,
                   -- B1 des deux relectures (#3299) : l'ancien exécuteur ne
                   -- doit plus pouvoir renouveler ni ouvrir un tour (renew et
                   -- begin_turn ne contrôlent que owner/epoch, pas l'hôte).
                   -- Même avec --keep-session, le bail de la source est révoqué
                   -- dans CETTE écriture ; l'époque avance et l'arrivée peut
                   -- réclamer après son sync.
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   lease_epoch = r.lease_epoch + 1,
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.host = %s
               AND coalesce(verrou.releve, 'hote') <> 'election'
               AND verrou.status <> 'running'
               AND NOT ameesh_handoff_fenced(r.name)
               AND NOT EXISTS (SELECT 1 FROM host_handoffs h WHERE h.agent=r.name
                               AND h.state NOT IN ('verified','cancelled'))
               AND %s = ANY(verrou.admitted_hosts)
               -- #254 : rien ne part vers un hôte en pause
               AND NOT EXISTS (SELECT 1 FROM host_holds hh
                                WHERE hh.host = %s AND hh.until > clock_timestamp())
               -- #3299 : bail vivant refusé, sauf exécuteur de départ arrêté.
               -- La preuve locale (unité inactive) ne vaut que si le dernier
               -- relevé vient de l'unité DE CET agent (`runner-<nom>@…`) : le
               -- gabarit systemd répond loaded/inactive pour tout nom, même
               -- jamais instancié, et un agent servi par une autre unité
               -- (coordinateur partagé) garderait sinon un bail vivant.
               AND ((%s::boolean
                     AND starts_with(coalesce(verrou.runner_seen_owner, ''),
                                     'runner-' || verrou.name || '@'))
                    OR verrou.lease_owner IS NULL
                    OR verrou.lease_expires_at IS NULL
                    OR verrou.lease_expires_at <= clock_timestamp()
                    OR NOT (coalesce(verrou.runner_seen_host = verrou.host, false)
                            AND verrou.runner_seen_at IS NOT NULL
                            AND verrou.runner_seen_at >
                                clock_timestamp() - (%s::float8 * interval '1 second')))
              RETURNING r.name, r.host,
                        verrou.lease_owner AS previous_lease_owner,
                        verrou.lease_epoch AS previous_lease_epoch,
                        (%s::boolean
                         AND starts_with(coalesce(verrou.runner_seen_owner, ''),
                                         'runner-' || verrou.name || '@'))
                            AS source_stopped
            """,
            (name, target, canon_ref, diagnostic, ref, bool(keep_session),
             bool(keep_session), bool(keep_session), bool(keep_session), summary, summary,
             bool(keep_session), "moved to %s (manual)" % target,
             host, target, target, bool(source_stopped), stale, bool(source_stopped)),
        )
        return rows[0] if rows else None

    def stop_removed(self, name, host, *, pending_text, stop_text,
                     owner=None, expected_lease_epoch=None) -> dict | None:
        guard, params = _owner(owner)
        epoch, epoch_params = "", ()
        if expected_lease_epoch is not None:
            epoch, epoch_params = " AND lease_epoch = %s", (expected_lease_epoch,)
        stopped = self.db.query(
            """
            UPDATE agent_registry
               SET status = CASE WHEN status = 'running' AND lease_expires_at > now()
                                 THEN status ELSE 'stopped' END,
                   status_text = CASE WHEN status = 'running' AND lease_expires_at > now()
                                      THEN %s ELSE %s END,
                   -- L37 (0030) : raison structurée (gardée par le
                   -- déclencheur seulement si l'arrêt a lieu maintenant)
                   stop_reason = 'retire_du_canon',
                   updated_at = now()
             WHERE name = %s AND host = %s AND status <> 'stopped'
               AND NOT ephemeral AND canon_ref IS NOT NULL __EPOCH__ __OWNER__
            RETURNING name, status
            """.replace("__EPOCH__", epoch).replace("__OWNER__", guard),
            (pending_text, stop_text, name, host) + epoch_params + params,
        )
        return stopped[0] if stopped else None


class Ephemerals(interface.Ephemerals):

    def creator(self, name) -> dict | None:
        rows = self.db.query(
            """
            SELECT name, responsible, ephemeral, status, capabilities,
                   (NOT ephemeral OR ephemeral_expires_at > now()) AS alive
              FROM agent_registry WHERE name = %s
            """,
            (name,),
        )
        return rows[0] if rows else None

    def create(self, name, creator, *, cwd, ttl_seconds) -> dict | None:
        """Insertion atomique (INSERT … SELECT sur la ligne du créateur) : le
        responsable copié est celui du créateur au moment de la création ; un
        verdict de placement admis n'est hérité que si le profil du créateur
        est encore le profil évalué."""
        created = self.db.query(
            """
            __LOCK__
            INSERT INTO agent_registry
                (name, chantier, harness, host, cwd, model, responsible, team, projects, provider,
                 credential_mode, capabilities, ephemeral, ephemeral_expires_at, created_by,
                 placement_ok, placement_diagnostic, placement_ref, placement_profile, canon)
            SELECT %s, c.chantier, c.harness, c.host, coalesce(%s, c.cwd), c.model,
                   c.responsible, c.team, c.projects, c.provider, c.credential_mode,
                   ARRAY(SELECT x FROM unnest(ARRAY['read', 'propose']::text[]) AS x
                          WHERE c.capabilities IS NULL OR x = ANY(c.capabilities)),
                   true,
                   CASE WHEN c.ephemeral
                        THEN least(now() + make_interval(secs => %s), c.ephemeral_expires_at)
                        ELSE now() + make_interval(secs => %s) END,
                   c.name,
                   -- C4 : le profil de l'éphémère est celui du créateur (colonnes
                   -- recopiées ci-dessus) ; un verdict admis n'est hérité que s'il
                   -- a jugé ce profil
                   CASE WHEN c.placement_ok AND c.placement_profile IS DISTINCT FROM __NOW__
                        THEN false ELSE c.placement_ok END,
                   CASE WHEN c.placement_ok AND c.placement_profile IS DISTINCT FROM __NOW__
                        THEN 'profile of ' || c.name || ' diverged since the placement was '
                             || 'evaluated (evaluated: ' || coalesce(c.placement_profile, 'none')
                             || '; current: ' || __NOW__ || '): placement not inherited, '
                             || 're-evaluate with "ameesh canon sync"'
                        ELSE c.placement_diagnostic END,
                   c.placement_ref, c.placement_profile,
                   -- L42 (0031) : l'éphémère appartient au canon de son créateur
                   c.canon
              FROM agent_registry c
             CROSS JOIN l
             WHERE c.name = %s
               AND coalesce(c.responsible, '') <> ''
               AND c.status <> 'stopped'
               AND (NOT c.ephemeral OR c.ephemeral_expires_at > now())
            ON CONFLICT (name) DO NOTHING
            RETURNING name, responsible, host, harness, capabilities, created_by,
                      placement_ok, placement_diagnostic,
                      extract(epoch from ephemeral_expires_at)::float8 AS ephemeral_expires_ts
            """.replace("__LOCK__", NAME_LOCK_CTE).replace("__NOW__", profile_sql("c")),
            (name_lock_key(name), name, cwd, ttl_seconds, ttl_seconds, creator),
        )
        return created[0] if created else None

    def exists(self, name) -> bool:
        return bool(self.db.query("SELECT 1 AS x FROM agent_registry WHERE name = %s", (name,)))
