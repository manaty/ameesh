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
from .placement import PROFILE_PARAMS_SQL, profile_params, profile_sql

_CAPS_SQL = ("CASE WHEN %s::text IS NULL THEN NULL "
             "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END")


def _caps_param(capabilities) -> str | None:
    return None if capabilities is None else json.dumps(list(capabilities))


class Canon(interface.Canon):

    def record_state(self, host, status, *, root, source, commit, good,
                     diagnostic) -> None:
        self.db.query(
            """
            INSERT INTO canon_state AS s
                (host, status, root, source, last_good_commit, last_good_at, diagnostic,
                 checked_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, now())
            ON CONFLICT (host) DO UPDATE SET
                status           = excluded.status,
                root             = excluded.root,
                source           = excluded.source,
                last_good_commit = CASE WHEN excluded.status = 'ok'
                                        THEN excluded.last_good_commit
                                        ELSE s.last_good_commit END,
                last_good_at     = CASE WHEN excluded.status = 'ok'
                                        THEN excluded.last_good_at
                                        ELSE s.last_good_at END,
                diagnostic       = excluded.diagnostic,
                checked_at       = excluded.checked_at
            RETURNING host
            """,
            (host, status, root, source, commit, good, diagnostic),
        )

    def state(self, host) -> dict | None:
        rows = self.db.query(
            """
            SELECT host, status, root, source, last_good_commit, diagnostic,
                   extract(epoch from last_good_at)::float8 AS last_good_ts,
                   extract(epoch from checked_at)::float8   AS checked_ts,
                   auth_status, auth_diagnostic,
                   extract(epoch from auth_checked_at)::float8 AS auth_checked_ts
              FROM canon_state WHERE host = %s
            """,
            (host,),
        )
        return rows[0] if rows else None

    def record_auth_state(self, host, status, diagnostic) -> dict | None:
        rows = self.db.query(
            "SELECT auth_status, auth_diagnostic FROM canon_state WHERE host = %s", (host,))
        self.db.query(
            "UPDATE canon_state SET auth_status = %s, auth_diagnostic = %s, "
            "auth_checked_at = now() WHERE host = %s RETURNING host",
            (status, diagnostic, host))
        return rows[0] if rows else None

    def host_rows(self, host, names) -> list[dict]:
        sql = """
            SELECT name, harness, host, cwd, model, budget_usd, responsible, team, provider,
                   credential_mode, capabilities, canon_ref, ephemeral, priority,
                   admitted_hosts, admitted_tags, memory_repository,
                   visibility_ok, visibility_diagnostic,
                   status, status_text,
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

    def write_declared(self, name, values) -> None:
        """Le profil évalué (`placement_profile`) est celui des valeurs écrites
        (celles que `placement.evaluate` a jugées), calculé par
        `ameesh_placement_profile` dans la même instruction."""
        caps = _caps_param(values["capabilities"])
        ahosts = _caps_param(values.get("admitted_hosts"))
        atags = _caps_param(values.get("admitted_tags"))
        self.db.query(
            """
            INSERT INTO agent_registry
                (name, harness, host, cwd, model, budget_usd, responsible, team, provider,
                 credential_mode, capabilities, canon_ref, ephemeral, ephemeral_expires_at,
                 priority, admitted_hosts, admitted_tags, memory_repository,
                 visibility_ok, visibility_diagnostic,
                 placement_ok, placement_diagnostic, placement_ref, placement_profile)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, __CAPS__, %s, false, NULL,
                    %s, __AHOSTS__, __ATAGS__, %s, %s, %s, %s, %s, %s, __PROFILE__)
            ON CONFLICT (name) DO UPDATE SET
                harness         = excluded.harness,
                host            = excluded.host,
                cwd             = excluded.cwd,
                model           = excluded.model,
                budget_usd      = excluded.budget_usd,
                responsible     = excluded.responsible,
                team            = excluded.team,
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
                updated_at      = now()
            RETURNING name
            """.replace("__CAPS__", _CAPS_SQL)
               .replace("__AHOSTS__", _CAPS_SQL)
               .replace("__ATAGS__", _CAPS_SQL)
               .replace("__PROFILE__", PROFILE_PARAMS_SQL),
            (name, values["harness"], values["host"], values["cwd"], values["model"],
             values["budget_usd"], values["responsible"], values["team"], values["provider"],
             values["credential_mode"], caps, caps, values["canon_ref"],
             int(values.get("priority") or 0), ahosts, ahosts, atags, atags,
             values.get("memory_repository"),
             values.get("visibility_ok"), values.get("visibility_diagnostic") or "",
             values["placement_ok"], values["placement_diagnostic"], values["placement_ref"])
            + profile_params(values),
        )

    def clear_responsible(self, name, host) -> None:
        self.db.execute(
            "UPDATE agent_registry SET responsible = NULL, updated_at = now() "
            "WHERE name = %s AND host = %s AND NOT ephemeral AND responsible IS NOT NULL",
            (name, host))

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
            SELECT name, created_by, ephemeral, canon_ref, host,
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
               AND %s = ANY(verrou.admitted_hosts)
             RETURNING r.name, r.host
            """,
            (name, target, canon_ref, diagnostic, ref, bool(keep_session),
             bool(keep_session), bool(keep_session), bool(keep_session), summary, summary,
             bool(keep_session), "déplacé vers %s" % target,
             host, owner, int(epoch), target),
        )
        return rows[0] if rows else None

    def stop_removed(self, name, host, *, pending_text, stop_text) -> dict | None:
        stopped = self.db.query(
            """
            UPDATE agent_registry
               SET status = CASE WHEN status = 'running' AND lease_expires_at > now()
                                 THEN status ELSE 'stopped' END,
                   status_text = CASE WHEN status = 'running' AND lease_expires_at > now()
                                      THEN %s ELSE %s END,
                   updated_at = now()
             WHERE name = %s AND host = %s AND status <> 'stopped'
               AND NOT ephemeral AND canon_ref IS NOT NULL
            RETURNING name, status
            """,
            (pending_text, stop_text, name, host),
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
            INSERT INTO agent_registry
                (name, chantier, harness, host, cwd, model, responsible, team, provider,
                 credential_mode, capabilities, ephemeral, ephemeral_expires_at, created_by,
                 placement_ok, placement_diagnostic, placement_ref, placement_profile)
            SELECT %s, c.chantier, c.harness, c.host, coalesce(%s, c.cwd), c.model,
                   c.responsible, c.team, c.provider, c.credential_mode,
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
                        THEN 'profil de ' || c.name || ' divergé depuis l''évaluation de son '
                             || 'placement (évalué : ' || coalesce(c.placement_profile, 'aucun')
                             || ' ; actuel : ' || __NOW__ || ') : placement non hérité, à '
                             || 'réévaluer par « ameesh canon sync »'
                        ELSE c.placement_diagnostic END,
                   c.placement_ref, c.placement_profile
              FROM agent_registry c
             WHERE c.name = %s
               AND coalesce(c.responsible, '') <> ''
               AND c.status <> 'stopped'
               AND (NOT c.ephemeral OR c.ephemeral_expires_at > now())
            ON CONFLICT (name) DO NOTHING
            RETURNING name, responsible, host, harness, capabilities, created_by,
                      placement_ok, placement_diagnostic,
                      extract(epoch from ephemeral_expires_at)::float8 AS ephemeral_expires_ts
            """.replace("__NOW__", profile_sql("c")),
            (name, cwd, ttl_seconds, ttl_seconds, creator),
        )
        return created[0] if created else None

    def exists(self, name) -> bool:
        return bool(self.db.query("SELECT 1 AS x FROM agent_registry WHERE name = %s", (name,)))
