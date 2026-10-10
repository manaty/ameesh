# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : registre des agents (`agent_registry`) et baux (R3, R5).

SQL déplacé tel quel depuis `ameesh.registry` (lot L1 ; marqueur comptable
`spend_pending` de L13 compris) et, pour l'effacement du modèle d'un agent
(`ameesh set model=`), depuis `ameesh.mesh_cli`, et pour le compte des
agents, depuis `ameesh.cli` (`doctor`) (phase 2). Rappel des
garanties, inchangées :

* `claim()` est atomique : un `UPDATE ... WHERE` sous READ COMMITTED, donc
  deux exécuteurs concurrents ne peuvent pas gagner le même agent ;
* `renew()` et `end_turn()` sont *fencés* par `lease_epoch` : un exécuteur
  dont le bail a expiré ne peut ni le prolonger, ni écrire un résultat ;
* un bail expiré est reprenable par n'importe quel autre exécuteur de l'hôte
  (`lease_expires_at < now()`), donc la mort d'un exécuteur ne bloque rien ;
* verrou de ligne pris D'ABORD (`SELECT ... FOR UPDATE`), conditions
  recontrôlées avec `clock_timestamp()` dans l'instruction qui écrit
  (EvalPlanQual ne réévalue pas un `WHERE` sans nouvelle version — grille
  codex3).
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from ...config import NAME_RE
from .. import interface

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: profondeur maximale d'une lignée d'éphémères suivie (au-delà : gouverné)
LINEAGE_MAX = 32

AGENT_COLUMNS = """
    name, chantier, harness, host, cwd, session_id, status, status_text, model,
    budget_usd, spent_usd, turns, pending_prompt, current_prompt, last_error,
    lease_owner, lease_epoch,
    extract(epoch from lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from last_seen)::float8         as last_seen_ts,
    extract(epoch from last_turn_at)::float8      as last_turn_ts,
    extract(epoch from last_event_at)::float8     as last_event_ts,
    responsible, team, provider, credential_mode, capabilities, canon_ref,
    ephemeral, created_by, priority, admitted_hosts, admitted_tags, memory_repository,
    extract(epoch from ephemeral_expires_at)::float8 as ephemeral_expires_ts,
    session_policy, effort, tier, session_work_item,
    extract(epoch from status_since)::float8         as status_since_ts,
    extract(epoch from restart_requested_at)::float8 as restart_requested_ts,
    extract(epoch from session_reset_at)::float8     as session_reset_ts,
    mode, stop_reason, session_account, context_max_tokens,
    turn_max_seconds, turn_mail_max
"""


def check_name(name: str) -> str:
    """Nom d'agent sûr (NAME_RE) : seul un nom contrôlé entre dans un littéral."""
    if not NAME_RE.match(name or ""):
        raise ValueError("nom d'agent invalide : %r" % (name,))
    return name


def name_list(names: Sequence[str]) -> str:
    return ", ".join("'%s'" % check_name(n).replace("'", "''") for n in names)


# --------------------------------------------------------------------------
# canon (§4.1, C2/C3) : condition de réclamation, partagée par claimable(),
# claim() et `ameesh attach`
# --------------------------------------------------------------------------

def _alias(alias: str) -> str:
    if not _ALIAS_RE.match(alias or ""):
        raise ValueError("alias SQL invalide : %r" % (alias,))
    return alias


def canon_governed_sql(alias: str = "agent_registry") -> str:
    """Expression SQL : l'agent de la ligne `alias` est-il gouverné par le canon ?

    Oui s'il vient du canon (`canon_ref` posé), ou si c'est un éphémère dont la
    lignée de créateurs (`created_by`, suivie de proche en proche) atteint un
    agent du canon. Fail closed : un éphémère dont la lignée est rompue
    (créateur absent) ou trop longue est tenu pour gouverné. Seuls les agents
    inscrits à la main (ni `canon_ref`, ni éphémère) et les éphémères issus
    d'eux ne le sont pas.
    """
    a = _alias(alias)
    return """(%(a)s.canon_ref IS NOT NULL
       OR (%(a)s.ephemeral AND NOT EXISTS (
           WITH RECURSIVE lignee (name, created_by, canon_ref, ephemeral, depth) AS (
               SELECT c.name, c.created_by, c.canon_ref, c.ephemeral, 1
                 FROM agent_registry c WHERE c.name = %(a)s.created_by
               UNION ALL
               SELECT c.name, c.created_by, c.canon_ref, c.ephemeral, l.depth + 1
                 FROM lignee l JOIN agent_registry c ON c.name = l.created_by
                WHERE l.ephemeral AND l.canon_ref IS NULL AND l.depth < %(max)d
           )
           SELECT 1 FROM lignee
            WHERE NOT lignee.ephemeral AND lignee.canon_ref IS NULL)))""" % {
        "a": a, "max": LINEAGE_MAX}


def canon_claim_predicate_sql(alias: str = "agent_registry") -> str:
    """Condition SQL de réclamation liée au canon (§4.1), pour un `WHERE`.

    Vraie si l'agent de la ligne `alias` (une ligne de `agent_registry`, ou de
    la vue `agent_mesh_overview`) n'est pas gouverné par le canon
    (`canon_governed_sql`), ou si le dernier état de **son canon** sur **son
    hôte** (`canon_state`, clé (host, canon) depuis L42/0031 ; colonne `canon`
    de la ligne, NULL = canon par défaut ; écrit par `ameesh canon sync`) est
    `ok` **et** que son
    placement sur cet hôte est admis par la politique de l'hôte
    (`placement_ok`, colonne déclarative écrite par `canon sync`, recopiée du
    créateur pour un éphémère ; C4, 0022) **pour le profil qu'il a
    maintenant** : `placement_profile` (le profil évalué, écrit avec le
    verdict) égal à `ameesh_placement_profile` de ses colonnes hôte,
    harnais, fournisseur, modèle et mode d'identifiants. Un canon `invalid`
    ou `unreadable`, l'absence d'état, un placement refusé, absent de cet
    hôte ou pas encore évalué (`placement_ok` faux ou NULL), un profil
    divergé depuis l'évaluation (inscription, import ou SQL à la main)
    refusent donc toute NOUVELLE réclamation d'un agent du canon (et de ses
    éphémères) jusqu'au prochain `canon sync`, sans rien toucher aux baux,
    sessions ni tours en cours : fail closed.

    Indépendante de la configuration (AMEESH_CANON, AMEESH_REQUIRE_RESPONSIBLE) :
    un agent venu du canon sur un hôte où aucune synchronisation n'a constaté
    un canon valide et un placement admis n'est jamais réclamable. Sans
    paramètre lié (`%s`) : elle s'insère telle quelle dans une requête
    paramétrée, avec les deux pilotes. Utilisée par `claimable()` et
    `claim()` ; à réutiliser telle quelle pour `ameesh attach` (C9).
    """
    a = _alias(alias)
    # L42 (0031) : l'état est celui du canon DE L'AGENT sur son hôte (colonne
    # `canon` ; NULL = canon par défaut, clé '') — un canon invalide ne ferme
    # que ses propres agents et leurs éphémères (qui portent son canon)
    return """(NOT %(governed)s
       OR (EXISTS (SELECT 1 FROM canon_state s
                    WHERE s.host = %(a)s.host AND s.canon = coalesce(%(a)s.canon, '')
                      AND s.status = 'ok')
           AND coalesce(%(a)s.placement_ok, false)
           AND %(a)s.placement_profile IS NOT DISTINCT FROM ameesh_placement_profile(
                   %(a)s.host, %(a)s.harness, %(a)s.provider, %(a)s.model,
                   %(a)s.credential_mode)))""" % {
        "governed": canon_governed_sql(a), "a": a}


def placement_admitted_sql(alias: str = "agent_registry") -> str:
    """L46 : l'ADMISSION seule (placement admis pour le profil actuel), sans
    l'état du canon de l'hôte — ce que `registry.wakeable` juge : un canon
    momentanément invalide ne bloque pas une attribution (la réclamation,
    elle, reste fermée par `canon_claim_predicate_sql`)."""
    a = _alias(alias)
    return """(NOT %(governed)s
       OR (coalesce(%(a)s.placement_ok, false)
           AND %(a)s.placement_profile IS NOT DISTINCT FROM ameesh_placement_profile(
                   %(a)s.host, %(a)s.harness, %(a)s.provider, %(a)s.model,
                   %(a)s.credential_mode)))""" % {
        "governed": canon_governed_sql(a), "a": a}


# --------------------------------------------------------------------------
# lignes du registre
# --------------------------------------------------------------------------

class Agents(interface.Agents):

    def upsert(self, name, *, chantier, harness, host, cwd, session_id, status,
               status_text, model, budget_usd, mode=None) -> dict:
        # L37 (0030) : `mode` ne vaut qu'à l'INSERT — un agent existant
        # (`execute`) n'est jamais basculé par une inscription.
        rows = self.db.query(
            """
            INSERT INTO agent_registry
                (name, chantier, harness, host, cwd, session_id, status, status_text,
                 model, budget_usd, mode, last_seen, updated_at)
            VALUES (%s, coalesce(%s,''), coalesce(%s,'other'), coalesce(%s,''), %s, %s,
                    coalesce(%s,'idle'), coalesce(%s,''), %s, %s, coalesce(%s,'execute'),
                    now(), now())
            ON CONFLICT (name) DO UPDATE SET
                chantier    = coalesce(nullif(%s,''), agent_registry.chantier),
                harness     = coalesce(nullif(%s,''), agent_registry.harness),
                host        = coalesce(nullif(%s,''), agent_registry.host),
                cwd         = coalesce(excluded.cwd,        agent_registry.cwd),
                session_id  = coalesce(excluded.session_id, agent_registry.session_id),
                -- L39 (0033) : une AUTRE session inscrite (register --session,
                -- hook) a un compte d'origine inconnu
                session_account = CASE
                    WHEN excluded.session_id IS NOT NULL
                     AND excluded.session_id IS DISTINCT FROM agent_registry.session_id
                    THEN NULL ELSE agent_registry.session_account END,
                status      = coalesce(%s, agent_registry.status),
                status_text = coalesce(nullif(excluded.status_text,''), agent_registry.status_text),
                model       = coalesce(excluded.model,      agent_registry.model),
                budget_usd  = coalesce(excluded.budget_usd, agent_registry.budget_usd),
                last_seen   = now(),
                updated_at  = now()
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", AGENT_COLUMNS),
            (name, chantier, harness, host, cwd, session_id, status, status_text,
             model, budget_usd, mode, chantier, harness, host, status),
        )
        return rows[0]

    def upsert_unleased(self, name, *, chantier, harness, host, cwd,
                        session_id) -> dict:
        # L46 : inscription par un hook SANS bail, en UNE instruction (pas de
        # course lecture → écriture). Ligne neuve : agent `externe`. Ligne
        # existante d'un agent `execute` : seul `last_seen` avance — ni hôte,
        # ni harnais, ni session, ni dossier, ni chantier. Ligne `externe` :
        # règle de L36 (hôte, harnais, chantier suivis ; session et dossier
        # écrits seulement s'il n'y avait pas encore de session).
        rows = self.db.query(
            """
            INSERT INTO agent_registry
                (name, chantier, harness, host, cwd, session_id, mode,
                 last_seen, updated_at)
            VALUES (%s, coalesce(%s,''), coalesce(%s,'other'), coalesce(%s,''), %s, %s,
                    'externe', now(), now())
            ON CONFLICT (name) DO UPDATE SET
                chantier = CASE WHEN agent_registry.mode = 'execute'
                                THEN agent_registry.chantier
                                ELSE coalesce(nullif(excluded.chantier,''),
                                              agent_registry.chantier) END,
                harness  = CASE WHEN agent_registry.mode = 'execute' OR %s::text IS NULL
                                THEN agent_registry.harness
                                ELSE coalesce(nullif(excluded.harness,''),
                                              agent_registry.harness) END,
                host     = CASE WHEN agent_registry.mode = 'execute'
                                THEN agent_registry.host
                                ELSE coalesce(nullif(excluded.host,''),
                                              agent_registry.host) END,
                cwd      = CASE WHEN agent_registry.mode = 'execute'
                                  OR agent_registry.session_id IS NOT NULL
                                THEN agent_registry.cwd
                                ELSE coalesce(excluded.cwd, agent_registry.cwd) END,
                session_id = CASE WHEN agent_registry.mode = 'execute'
                                    OR agent_registry.session_id IS NOT NULL
                                  THEN agent_registry.session_id
                                  ELSE excluded.session_id END,
                session_account = CASE
                    WHEN agent_registry.mode <> 'execute'
                     AND agent_registry.session_id IS NULL
                     AND excluded.session_id IS NOT NULL
                    THEN NULL ELSE agent_registry.session_account END,
                last_seen  = now(),
                updated_at = CASE WHEN agent_registry.mode = 'execute'
                                  THEN agent_registry.updated_at ELSE now() END
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", AGENT_COLUMNS),
            (name, chantier, harness, host, cwd, session_id, harness),
        )
        return rows[0]

    def get(self, name) -> dict | None:
        rows = self.db.query("SELECT %s FROM agent_registry WHERE name = %%s" % AGENT_COLUMNS,
                             (name,))
        return rows[0] if rows else None

    def harnesses(self) -> dict[str, str]:
        rows = self.db.query("SELECT name, harness FROM agent_registry")
        return {row["name"]: row["harness"] or "" for row in rows}

    def overview(self) -> list[dict]:
        return self.db.query(
            """
            SELECT name, chantier, harness, host, cwd, session_id, status, status_text,
                   model, budget_usd, spent_usd, turns, pending_prompt, last_error,
                   lease_owner, lease_epoch, lease_expires_ts, last_seen_ts, last_turn_ts,
                   public_key_fingerprint, key_role, key_ready, has_owner_key, unread,
                   responsible, team, provider, credential_mode, capabilities, canon_ref,
                   ephemeral, created_by, ephemeral_expires_ts, responsible_ok, priority,
                   mode, stop_reason, canon,
                   __GOVERNED__ AS canon_governed,
                   (SELECT s.status FROM canon_state s WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_status,
                   (SELECT s.diagnostic FROM canon_state s
                     WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_diagnostic,
                   (SELECT extract(epoch from s.checked_at)::float8 FROM canon_state s
                     WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_checked_ts,
                   __CLAIM_OK__ AS canon_claim_ok
            FROM agent_mesh_overview o
            ORDER BY last_seen_ts DESC NULLS LAST
            """.replace("__GOVERNED__", canon_governed_sql("o"))
               .replace("__CLAIM_OK__", canon_claim_predicate_sql("o"))
        )

    def claimable(self, host, names, *, require_responsible) -> list[dict]:
        sql = """
            SELECT %s
            FROM agent_registry
            WHERE host = %%s
              AND status <> 'stopped'
              -- L46 : un agent `externe` (session humaine) n'est jamais mené
              -- par l'exécuteur, même avec une session et du courrier.
              AND mode = 'execute'
              AND (lease_owner IS NULL OR lease_expires_at < clock_timestamp())
              AND (NOT ephemeral OR ephemeral_expires_at > clock_timestamp())
              AND %s
        """ % (AGENT_COLUMNS, canon_claim_predicate_sql("agent_registry"))
        if require_responsible:
            sql += " AND coalesce(responsible, '') <> ''"
        params: list[Any] = [host]
        if names:
            sql += " AND name IN (%s)" % name_list(names)
        sql += " ORDER BY last_seen"
        return self.db.query(sql, tuple(params))

    def mark_event_wake(self, name) -> None:
        self.db.execute(
            "UPDATE agent_registry SET last_event_at = now(), updated_at = now() WHERE name = %s",
            (name,),
        )

    def set_session(self, name, session_id, account=None) -> None:
        # L39 (0033) : la session ET le compte sous lequel elle tourne, en une
        # écriture (NULL : hôte sans comptes déclarés)
        self.db.execute(
            "UPDATE agent_registry SET session_id = %s, session_account = %s,"
            " updated_at = now() WHERE name = %s",
            (session_id, account, name),
        )

    def set_session_account(self, name, account) -> bool:
        rows = self.db.query(
            "UPDATE agent_registry SET session_account = %s, updated_at = now()"
            " WHERE name = %s AND session_id IS NOT NULL RETURNING name",
            (account, name))
        return bool(rows)

    def cwd_used(self, cwd, exclude) -> bool:
        rows = self.db.query(
            "SELECT 1 AS ok FROM agent_registry WHERE cwd = %s AND name <> %s LIMIT 1",
            (cwd, exclude),
        )
        return bool(rows)

    def set_status(self, name, status, status_text, error, stop_reason=None) -> None:
        # L37 : la raison d'arrêt n'est gardée que pour `stopped`/`dead` (le
        # déclencheur de 0030 l'efface pour tout autre statut).
        self.db.execute(
            """
            UPDATE agent_registry
               SET status = %s,
                   status_text = coalesce(%s, status_text),
                   last_error = %s,
                   stop_reason = %s,
                   last_seen = now(),
                   updated_at = now()
             WHERE name = %s
            """,
            (status, status_text, error, stop_reason, name),
        )

    def set_mode(self, name, mode) -> bool:
        rows = self.db.query(
            "UPDATE agent_registry SET mode = %s, updated_at = now() WHERE name = %s"
            " RETURNING name", (mode, name))
        return bool(rows)

    def wake_check(self, name) -> dict | None:
        """Ce que `registry.wakeable` (L37, 0030) juge : mode, responsable,
        statut, éphémère vivant, et la condition de réclamation du canon
        (`canon_claim_predicate_sql`, la même que `claim`)."""
        rows = self.db.query(
            """
            SELECT r.name, r.mode, r.status, r.stop_reason, r.responsible, r.host,
                   r.canon_ref, r.placement_ok, r.placement_diagnostic,
                   (NOT r.ephemeral OR r.ephemeral_expires_at > now()) AS alive,
                   __GOVERNED__ AS canon_governed,
                   __CLAIM_OK__ AS canon_claim_ok,
                   __ADMITTED__ AS placement_admitted,
                   (SELECT s.status FROM canon_state s WHERE s.host = r.host
                       AND s.canon = coalesce(r.canon, '')) AS canon_status
              FROM agent_registry r WHERE r.name = %s
            """.replace("__GOVERNED__", canon_governed_sql("r"))
               .replace("__CLAIM_OK__", canon_claim_predicate_sql("r"))
               .replace("__ADMITTED__", placement_admitted_sql("r")),
            (name,))
        return rows[0] if rows else None

    def human_known(self, human) -> bool:
        # L46 : un humain déjà résolu par un canon (responsable d'un agent ou
        # d'un paquet : `canon sync` n'écrit que des responsables résolus)
        rows = self.db.query(
            "SELECT 1 AS ok WHERE EXISTS (SELECT 1 FROM agent_registry WHERE responsible = %s)"
            " OR EXISTS (SELECT 1 FROM work_packages WHERE responsible = %s)",
            (human, human))
        return bool(rows)

    def set_pending_prompt(self, name, prompt) -> None:
        self.db.execute(
            """
            UPDATE agent_registry
               SET pending_prompt = %s,
                   status = CASE WHEN %s::text IS NULL THEN status ELSE 'queued' END,
                   updated_at = now()
             WHERE name = %s
            """,
            (prompt, prompt, name),
        )

    def clear_model(self, name) -> None:
        self.db.execute("UPDATE agent_registry SET model = NULL WHERE name = %s",
                        (name,))

    def count(self) -> int:
        return self.db.query("SELECT count(*)::int AS n FROM agent_registry")[0]["n"]


# --------------------------------------------------------------------------
# baux
# --------------------------------------------------------------------------

class Leases(interface.Leases):

    def claim(self, name, owner, ttl_seconds, *, require_responsible) -> dict | None:
        """Prend le bail s'il est libre ou expiré. Renvoie la ligne, sinon None.

        Le verrou de ligne est pris **d'abord** (`SELECT ... FOR UPDATE`), puis
        les conditions sont recontrôlées avec `clock_timestamp()` dans
        l'instruction qui écrit : un verrou attendu au-delà de l'échéance
        refuse la réclamation, même si le détenteur n'a fait qu'un `SELECT ...
        FOR UPDATE` sans modifier la ligne (EvalPlanQual ne réévalue pas un
        `WHERE` sans nouvelle version — grille codex3). Mêmes règles que
        `claimable`, dans la même instruction.
        """
        sql = """
            WITH verrou AS (
                SELECT name FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET lease_owner      = %s,
                   lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                   lease_epoch      = lease_epoch + 1,
                   -- R5 : si le bail n'était plus vivant (autre main, ou même
                   -- exécuteur relancé avec le même identifiant), la consigne
                   -- inachevée repart en attente. Si une consigne était déjà en
                   -- attente, les deux sont conservées (B6b, B6c).
                   pending_prompt   = CASE
                       WHEN r.current_prompt IS NOT NULL
                            AND (r.lease_owner IS DISTINCT FROM %s
                                 OR r.lease_expires_at IS NULL
                                 OR r.lease_expires_at < clock_timestamp())
                       THEN CASE
                           WHEN r.pending_prompt IS NULL THEN r.current_prompt
                           ELSE r.current_prompt || E'\n\n' || r.pending_prompt
                       END
                       ELSE r.pending_prompt END,
                   current_prompt   = CASE
                       WHEN r.lease_owner IS DISTINCT FROM %s
                            OR r.lease_expires_at IS NULL
                            OR r.lease_expires_at < clock_timestamp()
                       THEN NULL
                       ELSE r.current_prompt END,
                   status           = CASE WHEN r.status = 'dead' THEN 'idle' ELSE r.status END,
                   updated_at       = now()
              FROM verrou
             WHERE r.name = verrou.name
               -- L46 : même règle que `claimable` — jamais un agent `externe`.
               AND r.mode = 'execute'
               AND (r.lease_owner IS NULL OR r.lease_owner = %s
                    OR r.lease_expires_at < clock_timestamp())
               AND (NOT r.ephemeral OR r.ephemeral_expires_at > clock_timestamp())
               __RESPONSABLE__
               AND __CANON__
            RETURNING r.name, r.lease_owner, r.lease_epoch,
                      extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts
        """
        sql = sql.replace("__RESPONSABLE__",
                          "AND coalesce(r.responsible, '') <> ''" if require_responsible else "")
        sql = sql.replace("__CANON__", canon_claim_predicate_sql("r"))
        rows = self.db.query(sql, (name, owner, ttl_seconds, owner, owner, owner))
        return rows[0] if rows else None

    def attach_claim(self, name, owner, ttl_seconds, *, require_responsible) -> dict | None:
        """Prend le bail pour une session interactive (`ameesh attach`, C9).

        Un bail vivant mais sans tour ni session en cours peut être repris ;
        un tour vivant (`status = 'running'` + bail vivant) jamais. Mêmes
        règles que `claimable` ; verrou pris d'abord et échéances
        recontrôlées avec `clock_timestamp()`.

        L46 : contrairement à `claimable` / `claim`, le mode n'est PAS filtré —
        `attach` est une session humaine interactive, ce qu'est par nature un
        agent `externe` ; l'exécuteur, lui, ne le réclamera jamais.
        """
        sql = """
            WITH verrou AS (
                SELECT name FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET lease_owner      = %s,
                   lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                   lease_epoch      = lease_epoch + 1,
                   status           = 'running',
                   status_text      = 'session interactive (attach)',
                   updated_at       = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND r.status <> 'stopped'
               AND NOT (r.status = 'running' AND r.lease_owner IS NOT NULL
                        AND r.lease_expires_at > clock_timestamp())
               AND (NOT r.ephemeral OR r.ephemeral_expires_at > clock_timestamp())
               __RESPONSABLE__
               AND __CANON__
            RETURNING r.name, r.lease_owner, r.lease_epoch,
                      extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts
        """
        sql = sql.replace("__RESPONSABLE__",
                          "AND coalesce(r.responsible, '') <> ''" if require_responsible else "")
        sql = sql.replace("__CANON__", canon_claim_predicate_sql("r"))
        rows = self.db.query(sql, (name, owner, ttl_seconds))
        return rows[0] if rows else None

    def state(self, name) -> dict | None:
        rows = self.db.query(
            """
            SELECT lease_owner, lease_epoch, status,
                   (lease_expires_at IS NOT NULL AND lease_expires_at > clock_timestamp()) AS live
              FROM agent_registry WHERE name = %s
            """,
            (name,),
        )
        return rows[0] if rows else None

    def renew(self, name, owner, epoch, ttl_seconds) -> float | None:
        """Verrou pris d'abord, échéance recontrôlée avec `clock_timestamp()`
        dans l'écriture : un renouvellement qui a attendu le verrou au-delà de
        l'expiration est refusé (grille codex3)."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                   last_seen        = now(),
                   updated_at       = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
            RETURNING extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts
            """,
            (name, ttl_seconds, owner, epoch),
        )
        return rows[0]["lease_expires_ts"] if rows else None

    def release(self, name, owner, epoch) -> bool:
        rows = self.db.query(
            """
            UPDATE agent_registry
               SET lease_owner      = NULL,
                   lease_expires_at = NULL,
                   lease_epoch      = lease_epoch + 1,
                   status           = CASE WHEN status = 'running' THEN 'idle' ELSE status END,
                   updated_at       = now()
             WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
            RETURNING lease_epoch
            """,
            (name, owner, epoch),
        )
        return bool(rows)

    def turn_in_progress(self, name) -> bool:
        rows = self.db.query(
            """
            SELECT (status = 'running' AND lease_owner IS NOT NULL
                    AND lease_expires_at > clock_timestamp()) AS en_cours
              FROM agent_registry WHERE name = %s
            """,
            (name,),
        )
        return bool(rows and rows[0]["en_cours"])

    def reap(self, host) -> list[dict]:
        sql = """
            UPDATE agent_registry
               SET status = 'dead', status_text = 'bail expiré', stop_reason = 'bail_expire',
                   updated_at = now()
             WHERE status = 'running' AND lease_expires_at < clock_timestamp()
        """
        params: tuple = ()
        if host is not None:
            sql += " AND host = %s"
            params = (host,)
        sql += " RETURNING name, lease_owner, lease_epoch"
        return self.db.query(sql, params)

    def clear_session(self, name, owner, epoch) -> bool:
        """Fencé par un bail valide : verrou pris d'abord, échéance
        recontrôlée avec `clock_timestamp()` dans l'écriture."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET session_id = NULL, session_work_item = NULL, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
            RETURNING r.name
            """,
            (name, owner, epoch),
        )
        return bool(rows)

    def hold_note(self, name, owner, epoch, status_text) -> bool:
        """Texte d'attente (L31b) : même verrou et mêmes recontrôles que
        `pause`, mais le statut reste ce qu'il est (`idle` ou `queued`)."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, status, status_text, lease_owner, lease_epoch,
                       lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET status_text = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND verrou.status IN ('idle', 'queued')
            RETURNING r.name
            """,
            (name, status_text, owner, int(epoch)),
        )
        return bool(rows)

    def release_hold(self, name, owner, epoch, status_text, restore="") -> bool:
        """Levée (L31b) : seulement notre texte, sous notre bail vivant ; une
        pause `blocked` repasse `queued` ou `idle` selon la consigne."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, status, status_text, lease_owner, lease_epoch,
                       lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET status = CASE WHEN verrou.status <> 'blocked' THEN r.status
                                 WHEN r.pending_prompt IS NULL THEN 'idle'
                                 ELSE 'queued' END,
                   status_text = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND verrou.status IN ('idle', 'queued', 'blocked')
               AND coalesce(verrou.status_text, '') = %s
            RETURNING r.name
            """,
            (name, restore or "", owner, int(epoch), status_text),
        )
        return bool(rows)

    def pause(self, name, owner, epoch, status_text) -> bool:
        """Pause `blocked` fencée par un bail vivant (L31, 0028) : verrou
        d'abord, puis recontrôle que le bail est toujours le nôtre, vivant, et
        que l'agent n'est pas en tour (jamais de coupure au milieu d'un tour)."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, status, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET status = 'blocked', status_text = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND verrou.status NOT IN ('running', 'stopped')
            RETURNING r.name
            """,
            (name, status_text, owner, int(epoch)),
        )
        return bool(rows)

    #: blocage marqué (L35) : même forme que `pause` — verrou de ligne d'abord,
    #: bail (propriétaire, epoch, échéance `clock_timestamp()`) et état
    #: recontrôlés dans l'écriture. La décision est cette seule instruction ;
    #: si elle n'écrit rien, une lecture dit seulement POURQUOI (bail ou statut
    #: concurrent), sans rien décider : un bail perdu entre les deux ne peut
    #: que faire répondre « lease », ce qui arrête le worker de toute façon.
    _MARKED = """
        WITH verrou AS (
            SELECT name, status, status_text, last_error, lease_owner, lease_epoch,
                   lease_expires_at
              FROM agent_registry WHERE name = %%s FOR UPDATE
        )
        UPDATE agent_registry AS r
           SET %s,
               last_seen = now(), updated_at = now()
          FROM verrou
         WHERE r.name = verrou.name
           AND verrou.lease_owner = %%s AND verrou.lease_epoch = %%s
           AND verrou.lease_expires_at > clock_timestamp()
           AND %s
        RETURNING r.name
    """
    _MARK_OK = ("verrou.status_text = %s"
                " AND starts_with(coalesce(verrou.last_error, ''), %s)")

    def _marked_issue(self, rows, name, owner, epoch) -> str:
        if rows:
            return "done"
        bail = self.db.query(
            """
            SELECT (lease_owner = %s AND lease_epoch = %s
                    AND lease_expires_at > clock_timestamp()) AS ok
              FROM agent_registry WHERE name = %s
            """, (owner, int(epoch), name))
        return "kept" if bail and bail[0]["ok"] else "lease"

    def set_marked_block(self, name, owner, epoch, status_text, error, error_prefix) -> str:
        sql = self._MARKED % (
            "status = 'blocked', status_text = %s, last_error = %s",
            "verrou.status <> 'stopped' AND (verrou.status <> 'blocked' OR ("
            + self._MARK_OK + "))")
        rows = self.db.query(sql, (name, status_text, error, owner, int(epoch),
                                   status_text, error_prefix))
        return self._marked_issue(rows, name, owner, epoch)

    def clear_marked_block(self, name, owner, epoch, status_text, error_prefix) -> str:
        sql = self._MARKED % (
            "status = CASE WHEN r.pending_prompt IS NULL THEN 'idle' ELSE 'queued' END,"
            " status_text = '', last_error = NULL",
            "verrou.status = 'blocked' AND " + self._MARK_OK)
        rows = self.db.query(sql, (name, owner, int(epoch), status_text, error_prefix))
        return self._marked_issue(rows, name, owner, epoch)

    def take_pending_prompt(self, name, owner, epoch) -> str | None:
        """Verrou pris d'abord, échéance recontrôlée avec `clock_timestamp()`
        dans l'écriture (grille codex3) : une consigne n'est pas consommée
        sous un bail expiré pendant l'attente du verrou, ni par un agent
        arrêté entre la lecture du registre et cette écriture (L35)."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, status, pending_prompt, lease_owner, lease_epoch,
                       lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET pending_prompt = NULL,
                   current_prompt = verrou.pending_prompt,
                   status = 'running',
                   status_text = 'tour en cours',
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.pending_prompt IS NOT NULL
               AND verrou.status <> 'stopped'
               AND verrou.lease_expires_at > clock_timestamp()
            RETURNING verrou.pending_prompt
            """,
            (name, owner, epoch),
        )
        return rows[0]["pending_prompt"] if rows else None

    def begin_turn(self, name, owner, epoch, status_text) -> bool:
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, status, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET status = 'running', status_text = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND verrou.status <> 'stopped'   -- arrêté entre lecture et tour (L35)
            RETURNING r.name
            """,
            (name, status_text, owner, epoch),
        )
        return bool(rows)

    def restore_prompt(self, name, owner, epoch) -> bool:
        rows = self.db.query(
            """
            UPDATE agent_registry
               SET pending_prompt = CASE
                       WHEN current_prompt IS NULL THEN pending_prompt
                       WHEN pending_prompt IS NULL THEN current_prompt
                       ELSE current_prompt || E'\n\n' || pending_prompt
                   END,
                   current_prompt = NULL,
                   updated_at = now()
             WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
               AND current_prompt IS NOT NULL
            RETURNING name
            """,
            (name, owner, epoch),
        )
        return bool(rows)

    def end_turn(self, name, owner, epoch, *, status, status_text, error,
                 cost_usd) -> bool:
        rows = self.db.query(
            """
            UPDATE agent_registry
               SET status      = %s,
                   status_text = coalesce(%s, status_text),
                   last_error  = %s,
                   current_prompt = NULL,
                   turns       = turns + 1,
                   spent_usd   = spent_usd + coalesce(%s, 0),
                   last_turn_at = now(),
                   last_seen   = now(),
                   updated_at  = now()
             WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
            RETURNING name
            """,
            (status, status_text, error, cost_usd, name, owner, epoch),
        )
        return bool(rows)


# --------------------------------------------------------------------------
# marqueur comptable d'un tour (`spend_pending`, 0025 ; L13)
# --------------------------------------------------------------------------

class PendingSpend(interface.PendingSpend):

    def put(self, name, start_index, turn, model) -> bool:
        rows = self.db.query(
            """
            INSERT INTO spend_pending (agent, start_index, turn, model, created_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (agent) DO UPDATE SET
                start_index = excluded.start_index,
                turn        = excluded.turn,
                model       = excluded.model,
                created_at  = now()
            RETURNING agent
            """,
            (name, int(start_index), turn, model),
        )
        return bool(rows)

    def set_model(self, name, model) -> bool:
        rows = self.db.query(
            "UPDATE spend_pending SET model = %s WHERE agent = %s RETURNING agent",
            (model if model is not None else "", name),
        )
        return bool(rows)

    def get(self, name) -> dict | None:
        rows = self.db.query(
            """
            SELECT agent, start_index, turn, model,
                   extract(epoch from created_at)::float8 as created_ts
              FROM spend_pending WHERE agent = %s
            """,
            (name,),
        )
        return rows[0] if rows else None

    def clear(self, name) -> bool:
        rows = self.db.query("DELETE FROM spend_pending WHERE agent = %s RETURNING agent",
                             (name,))
        return bool(rows)
