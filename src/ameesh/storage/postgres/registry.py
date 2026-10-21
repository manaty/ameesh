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

import json
import re
from typing import Any, Sequence

from ...persona_sessions import track_change
from ...config import NAME_RE
from .. import interface

_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: profondeur maximale d'une lignée d'éphémères suivie (au-delà : gouverné)
LINEAGE_MAX = 32

AGENT_COLUMNS = """
    name, aliases, chantier, harness, host, cwd, session_id, status, status_text, model,
    budget_usd, spent_usd, turns, pending_prompt, current_prompt, last_error,
    lease_owner, lease_epoch, runner_seen_host, runner_seen_owner,
    extract(epoch from runner_seen_at)::float8 as runner_seen_ts,
    extract(epoch from lease_wait_since)::float8 as lease_wait_since_ts,
    extract(epoch from lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from last_seen)::float8         as last_seen_ts,
    extract(epoch from created_at)::float8        as created_ts,
    extract(epoch from last_turn_at)::float8      as last_turn_ts,
    extract(epoch from last_event_at)::float8     as last_event_ts,
    responsible, team, projects, provider, credential_mode, capabilities, canon_ref, canon,
    ephemeral, created_by, priority, admitted_hosts, admitted_tags, memory_repository,
    extract(epoch from ephemeral_expires_at)::float8 as ephemeral_expires_ts,
    session_policy, effort, tier, session_work_item,
    extract(epoch from status_since)::float8         as status_since_ts,
    extract(epoch from status_reason_since)::float8  as status_reason_since_ts,
    extract(epoch from restart_requested_at)::float8 as restart_requested_ts,
    extract(epoch from session_reset_at)::float8     as session_reset_ts,
    canon, handoff_fenced, mode, stop_reason, session_account, context_max_tokens,
    turn_max_seconds, turn_mail_max, turn_grace_seconds, lot_wake,
    -- #1037 : le harnais, le dossier et l'id d'OUVERTURE de la session ACTIVE
    -- (0384) ; NULL hors d'une session ouverte par l'exécuteur. Le harnais
    -- déclaré (`harness`) ne dit pas sous lequel la session tourne (L242b).
    session_harness, session_cwd, session_origin,
    operator_paused, operator_pause_reason, operator_pause_by, operator_pause_for,
    extract(epoch from operator_pause_at)::float8 as operator_pause_ts
"""


def check_name(name: str) -> str:
    """Nom d'agent sûr (NAME_RE) : seul un nom contrôlé entre dans un littéral."""
    if not NAME_RE.match(name or ""):
        raise ValueError("invalid agent name: %r" % (name,))
    return name


def name_list(names: Sequence[str]) -> str:
    return ", ".join("'%s'" % check_name(n).replace("'", "''") for n in names)


# --------------------------------------------------------------------------
# canon (§4.1, C2/C3) : condition de réclamation, partagée par claimable(),
# claim() et `ameesh attach`
# --------------------------------------------------------------------------

def _alias(alias: str) -> str:
    if not _ALIAS_RE.match(alias or ""):
        raise ValueError("invalid SQL alias: %r" % (alias,))
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


def canon_claim_predicate_sql(alias: str = "agent_registry", *, probe: bool = False) -> str:
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

    `probe=True` (L232b) : la réclamation d'un agent **nommé** (le pas de tour
    du contrôleur) passe par `ameesh_agent_claims_open`, qui rouvre la
    fermeture de release au seul agent sonde enrôlé de l'hôte. Les décisions
    d'ordonnancement (`claimable`, `wakeable`, aperçu) gardent le prédicat
    strict d'une fermeture.
    """
    a = _alias(alias)
    # L42 (0031) : l'état est celui du canon DE L'AGENT sur son hôte (colonne
    # `canon` ; NULL = canon par défaut, clé '') — un canon invalide ne ferme
    # que ses propres agents et leurs éphémères (qui portent son canon)
    open_sql = ("ameesh_agent_claims_open(%(a)s.host, %(a)s.name)" if probe
                else "ameesh_host_claims_open(%(a)s.host)")
    sql = """(""" + open_sql + """ AND NOT ameesh_handoff_fenced(%(a)s.name) AND (NOT %(governed)s
       OR (EXISTS (SELECT 1 FROM canon_state s
                    WHERE s.host = %(a)s.host AND s.canon = coalesce(%(a)s.canon, '')
                      AND s.status = 'ok')
           AND coalesce(%(a)s.placement_ok, false)
           AND %(a)s.placement_profile IS NOT DISTINCT FROM ameesh_placement_profile(
                   %(a)s.host, %(a)s.harness, %(a)s.provider, %(a)s.model,
                   %(a)s.credential_mode))))"""
    return sql % {"governed": canon_governed_sql(a), "a": a}


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


def write_runner_seen(db, host, owner, names, *, admission_sql=None, admission_params=()):
    """#508 : une écriture groupée, verrous ordonnés comme le cycle de vie hôte.

    Le chemin médié fournit un prédicat d'admission revérifié par l'UPDATE,
    après l'attente éventuelle d'une révocation concurrente du placement.
    """
    if names is not None and not names:
        return
    selected = "host = %s AND mode = 'execute'"
    if names is not None:
        selected += " AND name IN (%s)" % name_list(names)
    sql = f"""UPDATE agent_registry r
                SET runner_seen_at = clock_timestamp(),
                    runner_seen_host = %s, runner_seen_owner = %s
               FROM (SELECT name FROM agent_registry WHERE {selected}
                     ORDER BY name FOR UPDATE) s
              WHERE r.name = s.name AND r.host = %s
                AND r.mode = 'execute'"""
    params = [host, owner, host, host]
    if admission_sql is not None:
        sql += " AND (" + admission_sql + ")"
        params.extend(admission_params)
    db.execute(sql, tuple(params))


# --------------------------------------------------------------------------
# verrou par nom (L976)
# --------------------------------------------------------------------------

#: L976 : verrou advisory par nom, partagé par TOUS les chemins de création
#: d'une ligne du registre et par la décision « boîte morte »
#: (`mailbox.forward`, `mailbox.expire_dead_letters`). `FOR SHARE`/`FOR UPDATE`
#: ne verrouillent que les lignes PRÉSENTES : sans ce verrou, une inscription
#: concurrente pouvait se committer entre la lecture d'absence et l'écriture,
#: et le courrier d'un agent vivant était expiré ou renvoyé (relecture codex3,
#: B1 résiduel). La décision prend le verrou dans une instruction À PART, avant
#: sa lecture : l'instantané de l'instruction suivante voit alors l'inscription
#: qui a gagné le verrou.
NAME_LOCK_EXPR = "pg_advisory_xact_lock(hashtextextended(%s, 0))"
#: la prise de verrou seule (décision : instruction à part, avant la lecture)
NAME_LOCK_SQL = "SELECT " + NAME_LOCK_EXPR
#: la même prise de verrou, en CTE d'une instruction de création (le verrou
#: est alors tenu jusqu'à la fin de l'instruction, donc du commit)
NAME_LOCK_CTE = "WITH l AS (SELECT " + NAME_LOCK_EXPR + " AS _)"


def name_lock_key(name: str) -> str:
    """Clé du verrou advisory d'un nom (insensible à la casse)."""
    return "ameesh-agent-registry:" + (name or "").lower()


# --------------------------------------------------------------------------
# lignes du registre
# --------------------------------------------------------------------------

class Agents(interface.Agents):

    def operator_pause(self, name, *, paused, reason, actor, principal,
                       expect_reason=None, not_after=None) -> dict | None:
        # #2152 : reprise conditionnelle (une seule instruction SQL, verrou de
        # ligne) ; `expect_reason` n'a de sens que pour LEVER une pause.
        if expect_reason is not None:
            if paused:
                raise ValueError("expect_reason only lifts an operator pause")
            rows = self.db.query(
                "SELECT * FROM ameesh_operator_resume_if(%s, %s, %s, %s, %s)",
                (name, reason, actor, principal, expect_reason))
            return rows[0] if rows else None
        if not_after is not None:
            if not paused:
                raise ValueError("not_after only applies to an operator pause")
            # #2184/R3 : la base compare l'échéance à SA clock_timestamp(),
            # sous le verrou de ligne ; l'epoch client peut dériver.
            rows = self.db.query(
                "SELECT * FROM ameesh_operator_pause_before("
                "%s, %s, %s, %s, %s, to_timestamp(%s))",
                (name, paused, reason, actor, principal, not_after))
            return rows[0] if rows else None
        rows = self.db.query(
            "SELECT * FROM ameesh_operator_pause(%s, %s, %s, %s, %s)",
            (name, paused, reason, actor, principal))
        return rows[0] if rows else None

    def upsert(self, name, *, chantier, harness, host, cwd, session_id, status,
               status_text, model, budget_usd, mode=None) -> dict:
        # L37 (0030) : `mode` ne vaut qu'à l'INSERT — un agent existant
        # (`execute`) n'est jamais basculé par une inscription.
        rows = self.db.query(
            """
            __LOCK__
            INSERT INTO agent_registry
                (name, chantier, harness, host, cwd, session_id, status, status_text,
                 model, budget_usd, mode, last_seen, updated_at)
            SELECT %s, coalesce(%s,''), coalesce(%s,'other'), coalesce(%s,''), %s, %s,
                   coalesce(%s,'idle'), coalesce(%s,''), %s, %s, coalesce(%s,'execute'),
                   now(), now()
              FROM l
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
            """.replace("__LOCK__", NAME_LOCK_CTE).replace("__COLUMNS__", AGENT_COLUMNS),
            (name_lock_key(name),
             name, chantier, harness, host, cwd, session_id, status, status_text,
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
            __LOCK__
            INSERT INTO agent_registry
                (name, chantier, harness, host, cwd, session_id, mode,
                 last_seen, updated_at)
            SELECT %s, coalesce(%s,''), coalesce(%s,'other'), coalesce(%s,''), %s, %s,
                   'externe', now(), now()
              FROM l
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
            """.replace("__LOCK__", NAME_LOCK_CTE).replace("__COLUMNS__", AGENT_COLUMNS),
            (name_lock_key(name), name, chantier, harness, host, cwd, session_id, harness),
        )
        return rows[0]

    def get(self, name) -> dict | None:
        rows = self.db.query("SELECT %s FROM agent_registry WHERE name = "
                             "(SELECT ameesh_resolve_agent_name(%%s, %%s))" % AGENT_COLUMNS,
                             (name, "agents.get"))
        return rows[0] if rows else None

    def resolve_name(self, name, *, operation="lookup") -> str:
        return self.db.query("SELECT ameesh_resolve_agent_name(%s, %s) AS name",
                             (name, operation))[0]["name"]

    def harnesses(self) -> dict[str, str]:
        rows = self.db.query("SELECT name, harness FROM agent_registry")
        return {row["name"]: row["harness"] or "" for row in rows}

    def overview(self) -> list[dict]:
        return self.db.query(
            """
            SELECT name, (SELECT r.aliases FROM agent_registry r WHERE r.name=o.name) AS aliases,
                   chantier, harness, host, cwd, session_id, status, status_text,
                   model, budget_usd, spent_usd, turns, pending_prompt, last_error,
                   lease_owner, lease_epoch, lease_expires_ts, last_seen_ts, last_turn_ts,
                   public_key_fingerprint, key_role, key_ready, has_owner_key, unread,
                   passive_unread,
                   responsible, team, projects, provider, credential_mode, capabilities,
                   canon_ref,
                   ephemeral, created_by, ephemeral_expires_ts, responsible_ok, priority,
                   mode, stop_reason, canon,
                   (SELECT extract(epoch from r.runner_seen_at)::float8
                      FROM agent_registry r WHERE r.name = o.name) AS runner_seen_ts,
                   (SELECT r.runner_seen_host FROM agent_registry r
                     WHERE r.name = o.name) AS runner_seen_host,
                   (SELECT r.runner_seen_owner FROM agent_registry r
                     WHERE r.name = o.name) AS runner_seen_owner,
                   p.operator_paused, p.operator_pause_reason, p.operator_pause_by,
                   p.operator_pause_for, p.operator_pause_ts,
                   __GOVERNED__ AS canon_governed,
                   (SELECT s.status FROM canon_state s WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_status,
                   (SELECT s.diagnostic FROM canon_state s
                     WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_diagnostic,
                   (SELECT extract(epoch from s.checked_at)::float8 FROM canon_state s
                     WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_checked_ts,
                   __CLAIM_OK__ AS canon_claim_ok,
                   -- #1250 : le harnais de la session VIVANTE (0384), absent de
                   -- la vue dont les colonnes sont figées à sa création ;
                   -- sous-requête pour ne pas qualifier toutes les colonnes.
                   (SELECT r.session_harness FROM agent_registry r
                     WHERE r.name = o.name) AS session_harness
            FROM agent_mesh_overview o
            LEFT JOIN LATERAL (
                SELECT operator_paused, operator_pause_reason, operator_pause_by,
                       operator_pause_for,
                       extract(epoch from operator_pause_at)::float8 AS operator_pause_ts
                  FROM agent_registry WHERE name = o.name
            ) p ON true
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
              -- #254 : un hôte en pause ne prend aucun bail
              AND NOT EXISTS (SELECT 1 FROM host_holds hh
                               WHERE hh.host = agent_registry.host
                                 AND hh.until > clock_timestamp())
              AND %s
        """ % (AGENT_COLUMNS, canon_claim_predicate_sql("agent_registry"))
        if require_responsible:
            sql += " AND coalesce(responsible, '') <> ''"
        params: list[Any] = [host]
        if names:
            sql += " AND name IN (%s)" % name_list(names)
        sql += " ORDER BY last_seen"
        return self.db.query(sql, tuple(params))

    def runner_seen(self, host, owner, names) -> None:
        write_runner_seen(self.db, host, owner, names)

    def note_unleased(self, name, host, error) -> bool:
        if not error.startswith("lease_not_taken: "):
            raise ValueError("unleased diagnostic must start with lease_not_taken: ")
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, host, mode, status, lease_owner, lease_expires_at,
                       last_error
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET last_error = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name AND verrou.host = %s
               AND verrou.mode = 'execute'
               AND verrou.status IN ('idle', 'queued', 'dead')
               AND (verrou.lease_owner IS NULL OR verrou.lease_expires_at IS NULL
                    OR verrou.lease_expires_at <= clock_timestamp())
               AND verrou.last_error IS DISTINCT FROM %s
            RETURNING r.name
            """, (name, error, host, error))
        return bool(rows)

    def mark_event_wake(self, name) -> None:
        self.db.execute(
            "UPDATE agent_registry SET last_event_at = now(), updated_at = now() WHERE name = %s",
            (name,),
        )

    @track_change("replaced", returns_none=True)
    def set_session(self, name, session_id, account=None, *, harness=None, cwd=None) -> None:
        # L39 (0033) : la session ET le compte sous lequel elle tourne, en une
        # écriture (NULL : hôte sans comptes déclarés) ; #408 (0384) : et le
        # harnais et le dossier de son OUVERTURE s'ils sont donnés ; sinon
        # (session reprise) l'origine notée reste celle de son ouverture si
        # elle décrit cette session, inconnue sinon (déclencheur de 0384)
        if harness is not None or cwd is not None:
            self.db.execute(
                "UPDATE agent_registry SET session_id = %s, session_account = %s,"
                " session_harness = %s, session_cwd = %s, session_origin = %s,"
                " updated_at = now() WHERE name = %s",
                (session_id, account, harness, cwd, session_id, name),
            )
        else:
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

    def set_status_text(self, name, text) -> bool:
        # #2789 : le statut reste celui de l'exécuteur ; le texte d'un agent
        # retenu ou arrêté est un marqueur que sa levée relit à l'identique.
        rows = self.db.query(
            """
            UPDATE agent_registry
               SET status_text = %s, last_seen = now(), updated_at = now()
             WHERE name = %s AND status IN ('idle', 'queued', 'running')
            RETURNING name
            """,
            (text, name),
        )
        return bool(rows)

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
                   -- #2200 : la pause opérateur fait partie de la disponibilité
                   r.operator_paused, r.operator_pause_reason, r.operator_pause_by,
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
                   last_error       = CASE
                       WHEN starts_with(coalesce(r.last_error, ''), 'lease_not_taken: ')
                       THEN NULL ELSE r.last_error END,
                   updated_at       = now()
              FROM verrou
             WHERE r.name = verrou.name
               -- L46 : même règle que `claimable` — jamais un agent `externe`.
               AND r.mode = 'execute'
               AND (r.lease_owner IS NULL OR r.lease_owner = %s
                    OR r.lease_expires_at < clock_timestamp())
               AND (NOT r.ephemeral OR r.ephemeral_expires_at > clock_timestamp())
               -- #254 : sur un hôte en pause, aucun nouveau bail (le sien,
               -- encore vivant, se renouvelle)
               AND ((r.lease_owner = %s AND r.lease_expires_at >= clock_timestamp())
                    OR NOT EXISTS (SELECT 1 FROM host_holds hh
                                    WHERE hh.host = r.host
                                      AND hh.until > clock_timestamp()))
               __RESPONSABLE__
               AND NOT r.handoff_fenced
               AND __CANON__
            RETURNING r.name, r.lease_owner, r.lease_epoch,
                      extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts
        """
        sql = sql.replace("__RESPONSABLE__",
                          "AND coalesce(r.responsible, '') <> ''" if require_responsible else "")
        # L232b : réclamation d'un agent nommé (pas de tour du contrôleur) —
        # seul l'agent sonde de la fermeture de release rouvre la condition.
        sql = sql.replace("__CANON__", canon_claim_predicate_sql("r", probe=True))
        rows = self.db.query(sql, (name, owner, ttl_seconds, owner, owner, owner, owner))
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

        L719 : la ligne rendue porte `harness` et `created_ts` (l'identité de
        l'enregistrement, lue sous le verrou `FOR UPDATE`) pour que l'appelant
        lie ce qu'il applique à la registration **revendiquée**, et non à une
        lecture antérieure.
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
               -- #254 : sur un hôte en pause, aucun nouveau bail, interactif
               -- compris (le sien, encore vivant, se reprend)
               AND ((r.lease_owner = %s AND r.lease_expires_at >= clock_timestamp())
                    OR NOT EXISTS (SELECT 1 FROM host_holds hh
                                    WHERE hh.host = r.host
                                      AND hh.until > clock_timestamp()))
               __RESPONSABLE__
               AND NOT r.handoff_fenced
               AND __CANON__
            RETURNING r.name, r.lease_owner, r.lease_epoch,
                      extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts,
                      r.harness,
                      extract(epoch from r.created_at)::float8 as created_ts
        """
        sql = sql.replace("__RESPONSABLE__",
                          "AND coalesce(r.responsible, '') <> ''" if require_responsible else "")
        sql = sql.replace("__CANON__", canon_claim_predicate_sql("r"))
        rows = self.db.query(sql, (name, owner, ttl_seconds, owner))
        return rows[0] if rows else None

    def state(self, name) -> dict | None:
        rows = self.db.query(
            """
            SELECT name, lease_owner, lease_epoch, status,
                   (lease_expires_at IS NOT NULL AND lease_expires_at > clock_timestamp()) AS live,
                   projects, team, chantier
              FROM agent_registry WHERE name = (SELECT ameesh_resolve_agent_name(%s, 'registry.lease_matches'))
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
               SET status = 'dead', status_text = 'lease expired', stop_reason = 'bail_expire',
                   updated_at = now()
             WHERE status = 'running' AND lease_expires_at < clock_timestamp()
        """
        params: tuple = ()
        if host is not None:
            sql += " AND host = %s"
            params = (host,)
        sql += " RETURNING name, lease_owner, lease_epoch"
        return self.db.query(sql, params)

    @track_change("rotation")
    def clear_session(self, name, owner, epoch) -> bool:
        """Fencé par un bail valide : verrou pris d'abord, échéance
        recontrôlée avec `clock_timestamp()` dans l'écriture.

        L'oubli est **durable** : `session_reset_at` est posé dans la même
        écriture (#794, contrôle bureau du 14/10). Un fichier local que l'agent
        n'a pas pu retirer (PermissionError) reste ainsi antérieur au dernier
        oubli, donc `AgentWorker.current_session` ne le reprend pas — y compris
        sur un worker recréé, qui relit ce champ au registre.
        """
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET session_id = NULL, session_work_item = NULL,
                   session_reset_at = clock_timestamp(), updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
            RETURNING r.name
            """,
            (name, owner, epoch),
        )
        return bool(rows)

    def adopt_session_origin(self, name, owner, epoch, session, lot, *,
                             harness=None, cwd=None) -> bool:
        """#1900 B1 : origine d'une session sans origine, sous verrou.

        Verrou de ligne d'abord, puis recontrôle dans l'écriture : bail vivant
        (owner, epoch, échéance recontrôlée après l'attente du verrou), et
        ligne portant encore la session ET le lot attendus. Seuls
        `session_origin`, `session_cwd` et `session_harness` sont écrits —
        jamais `session_id`, qu'une rotation ou un remplaçant a pu poser
        pendant l'attente."""
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, session_id, session_work_item, lease_owner, lease_epoch,
                       lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET session_origin = %s, session_cwd = %s, session_harness = %s,
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND verrou.session_id = %s
               AND verrou.session_work_item = %s
            RETURNING r.name
            """,
            (name, session, cwd, harness, owner, int(epoch), session, str(lot)),
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
                SELECT name, status, handoff_fenced, lease_owner, lease_epoch, lease_expires_at
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
                SELECT name, status, handoff_fenced, operator_paused, pending_prompt, lease_owner, lease_epoch,
                       lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET pending_prompt = NULL,
                   current_prompt = verrou.pending_prompt,
                   status = 'running',
                   status_text = 'turn in progress',
                   updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.pending_prompt IS NOT NULL
               AND NOT verrou.operator_paused
               AND NOT verrou.handoff_fenced
               AND ameesh_agent_claims_open(r.host, r.name)
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
                SELECT name, status, handoff_fenced, operator_paused, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET status = 'running', status_text = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
               AND verrou.lease_expires_at > clock_timestamp()
               AND NOT verrou.handoff_fenced
               AND NOT verrou.operator_paused
               AND ameesh_agent_claims_open(r.host, r.name)
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
                   status = CASE WHEN operator_paused AND status = 'running'
                                 THEN 'queued' ELSE status END,
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

    #: #2166 : identité exacte du marqueur, en texte UTC à la microseconde.
    #: Jamais `extract(epoch ...)::float8` relu : une connexion qui l'imprime
    #: à précision réduite (`extra_float_digits=0`, pools, production) en perd
    #: les derniers chiffres, et la comparaison échouait après le règlement.
    _EXACT_CREATED_AT = (
        "to_char(created_at at time zone 'UTC',"
        " 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")

    #: #2166/B1 : l'instant de pose du marqueur vit aussi dans `started_at`
    #: et dans l'époque de la clé historique (`agent:index:epoch`). Cette clé
    #: a été écrite par une connexion dont `extra_float_digits` pouvait être
    #: réduit : à -5, PostgreSQL n'imprime que 10 chiffres significatifs, soit
    #: la seconde entière pour une époque actuelle (~1,8e9 s) — un arrondi
    #: pouvant atteindre 0,5 s, pas 123 ms. On ne compare donc pas à une
    #: fenêtre : l'époque de la clé doit être EXACTEMENT l'un des arrondis que
    #: l'ancienne connexion a pu produire (0 à 6 décimales, les deux voisins
    #: en cas d'égalité car PostgreSQL arrondit au pair). Un tour étranger
    #: à un autre instant n'en fait pas partie, même à 0,2 s ; une clé ISO
    #: (repli exact) est lue comme son époque. La ligne ou la réservation doit
    #: aussi suivre le marqueur : au même arrondi d'une seconde, celle d'un
    #: tour antérieur reste étrangère.
    _HISTORICAL_ROUNDINGS = 6

    def _get_sql(self) -> str:
        """`get` en UNE instruction : marqueur et clé durable du MÊME état.

        La clé se retrouve par l'identité du tour, jamais par ordre temporel :
        `turn_id` exact ; sinon `started_at` (instant de pose du marqueur) ;
        sinon, pour une ligne d'avant 0423, l'époque portée par sa clé, qui
        doit être un arrondi historique exact de l'instant du marqueur. Une
        ligne ou une réservation antérieure au marqueur est étrangère, même
        quand son arrondi tombe dans la même seconde. L'index de début seul ne
        prouve rien : il est réutilisé après une rotation du flux, donc une
        réservation (ou une ligne) au même index mais à un autre instant est
        étrangère. Elle n'est jamais retenue ; une réservation étrangère sans
        candidat, une réservation étrangère encore ouverte, ou une ligne
        étrangère au repli rendent la clé indémontrable (`ledger_ambiguous`), et
        l'appelant suspend au lieu de deviner. Un ancien worker peut écrire sa
        ligne APRÈS le marqueur de son remplaçant (#1469) : la première ligne
        après `created_at` n'est pas forcément celle du marqueur.

        La colonne `spend_key` n'est crue que si elle EST l'identité exacte du
        marqueur (`agent:index:created_at`, le format de `put`, #2905). Après un
        retour arrière de version (§14), l'ancien `put` réécrit l'identité du
        marqueur sans la clé : la colonne garde celle du tour précédent, et
        l'écrire ferait perdre le coût du tour (conflit `turn_costs.spend_key`).
        Toute autre valeur est traitée comme NULL : le repli par `turn_id`/
        `started_at` décide.
        """
        return f"""
WITH marqueur_brut AS (
    SELECT agent, start_index, turn, model, work_item_id, turn_id,
           resident_usage, spend_key,
           {self._EXACT_CREATED_AT} AS created_at,
           extract(epoch from created_at)::float8 AS created_ts,
           extract(epoch from created_at)::numeric AS epoch
      FROM spend_pending WHERE agent = %s
), marqueur AS (
    SELECT m.*,
           -- #2905 : la clé du marqueur n'est crue que si elle EST l'identité
           -- de ce marqueur (`agent:index:created_at`, le format de `put`).
           -- Après un retour arrière (§14), l'ancien `put` réécrit l'identité
           -- sans la clé : la colonne garde celle du tour précédent, qui
           -- ferait perdre le coût du tour (conflit `turn_costs.spend_key`).
           -- Toute autre valeur est traitée comme NULL : le repli décide.
           CASE WHEN m.spend_key = m.agent || ':' || m.start_index || ':'
                     || m.created_at
                THEN m.spend_key END AS cle_exacte,
           ARRAY(SELECT v
                   FROM generate_series(0, {self._HISTORICAL_ROUNDINGS}) AS g(d)
                   CROSS JOIN LATERAL (
                       SELECT round(m.epoch, d) AS v
                       UNION
                       -- égalité exacte : PostgreSQL arrondit au pair, donc
                       -- les deux voisins sont des sorties possibles
                       SELECT floor(m.epoch * power(10::numeric, d))
                              / power(10::numeric, d)
                        WHERE mod(m.epoch * power(10::numeric, d), 1) = 0.5
                   ) AS candidat
                  ORDER BY v) AS epoques
      FROM marqueur_brut m
), lignes_lues AS (
    SELECT tc.spend_key, tc.turn_id, tc.started_at, tc.recorded_at,
           m.turn_id AS marqueur_turn_id,
           m.epoques AS epoques,
           (left(tc.spend_key,
                 length(m.agent || ':' || m.start_index || ':'))
                = m.agent || ':' || m.start_index || ':') AS meme_index,
           -- une ligne du tour ne peut pas précéder son marqueur : celle d'un
           -- index réutilisé, même au même arrondi, est étrangère.
           (tc.recorded_at >= m.created_at::timestamptz) AS posterieure,
           substr(tc.spend_key,
                  length(m.agent || ':' || m.start_index || ':') + 1) AS suffixe
      FROM turn_costs tc
      JOIN marqueur m ON tc.agent = m.agent
     WHERE m.cle_exacte IS NULL
), lignes AS (
    SELECT *,
           CASE WHEN suffixe ~ '^[0-9]+([.][0-9]+)?$'
                THEN suffixe::numeric
                WHEN suffixe ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                              '[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}[.][0-9]{{6}}Z$'
                THEN extract(epoch from suffixe::timestamptz)::numeric
           END AS cle_epoch
      FROM lignes_lues
), lignes_identifiees AS (
    SELECT *,
           coalesce(cle_epoch = ANY(epoques), false) AS epoque_historique,
           coalesce(extract(epoch from started_at)::numeric = ANY(epoques),
                    false) AS debut_historique
      FROM lignes
), lignes_retenues AS (
    SELECT DISTINCT spend_key,
           CASE
             WHEN marqueur_turn_id IS NOT NULL
                  AND turn_id = marqueur_turn_id THEN 1
             WHEN turn_id IS NULL AND started_at IS NOT NULL AND meme_index
                  AND posterieure AND debut_historique THEN 2
             WHEN turn_id IS NULL AND started_at IS NULL AND meme_index
                  AND posterieure AND epoque_historique THEN 3
           END AS rang
      FROM lignes_identifiees
), meilleur AS (
    SELECT min(rang) AS rang FROM lignes_retenues
), retenues AS (
    SELECT DISTINCT spend_key FROM lignes_retenues
     WHERE rang = (SELECT rang FROM meilleur)
), lignes_etrangeres AS (
    -- repli sans turn_id ni started_at : l'époque de la clé doit être un
    -- arrondi historique exact du marqueur et la ligne le suivre, sinon c'est
    -- la ligne d'un index réutilisé.
    SELECT 1 FROM lignes_identifiees
     WHERE turn_id IS NULL AND started_at IS NULL AND meme_index
       AND (NOT posterieure OR NOT epoque_historique)
), lignes_contradictoires AS (
    -- l'instant de début et l'époque de la clé désignent deux arrondis
    -- différents : aucune identité démontrable, même si un seul candidat sort.
    SELECT 1 FROM lignes_identifiees
     WHERE turn_id IS NULL AND started_at IS NOT NULL AND meme_index
       AND posterieure AND debut_historique <> epoque_historique
), reservations_lues AS (
    SELECT ar.spend_key, ar.state, ar.created_at AS pose,
           (left(ar.spend_key,
                 length(m.agent || ':' || m.start_index || ':'))
                = m.agent || ':' || m.start_index || ':') AS meme_index,
           -- une réservation du tour est prise APRÈS son marqueur : celle
           -- d'un tour antérieur, même au même arrondi, est étrangère.
           (ar.created_at >= m.created_at::timestamptz) AS posterieure,
           substr(ar.spend_key,
                  length(m.agent || ':' || m.start_index || ':') + 1) AS suffixe,
           m.epoques AS epoques
      FROM account_reservations ar
      JOIN marqueur m ON ar.agent = m.agent
     WHERE m.cle_exacte IS NULL
), reservations_identifiees AS (
    SELECT *,
           CASE WHEN suffixe ~ '^[0-9]+([.][0-9]+)?$'
                THEN suffixe::numeric
                WHEN suffixe ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                              '[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}[.][0-9]{{6}}Z$'
                THEN extract(epoch from suffixe::timestamptz)::numeric
           END AS cle_epoch
      FROM reservations_lues
), reservations AS (
    -- la réservation du tour porte un arrondi historique de l'époque du
    -- marqueur dans sa clé et le suit : au même index, une réservation d'un
    -- autre instant est étrangère.
    SELECT DISTINCT spend_key FROM reservations_identifiees
     WHERE meme_index AND posterieure
       AND coalesce(cle_epoch = ANY(epoques), false)
), reservations_etrangeres AS (
    SELECT DISTINCT state FROM reservations_identifiees
     WHERE meme_index
       AND (NOT posterieure
            OR NOT coalesce(cle_epoch = ANY(epoques), false))
), candidats AS (
    SELECT spend_key FROM retenues WHERE spend_key IS NOT NULL
    UNION
    SELECT spend_key FROM reservations
), constat AS (
    SELECT
      (SELECT count(*) FROM candidats) AS n_candidats,
      EXISTS (SELECT 1 FROM retenues WHERE spend_key IS NULL) AS sans_cle,
      EXISTS (SELECT 1 FROM lignes_etrangeres) AS ligne_etrangere,
      EXISTS (SELECT 1 FROM lignes_contradictoires) AS contradictoire,
      EXISTS (SELECT 1 FROM reservations_etrangeres
               WHERE state = 'open') AS ouverte_etrangere,
      EXISTS (SELECT 1 FROM reservations_etrangeres) AS reservation_etrangere
)
SELECT m.agent, m.start_index, m.turn, m.model, m.work_item_id, m.turn_id,
       m.resident_usage, m.spend_key, m.created_at, m.created_ts,
       CASE
         WHEN m.cle_exacte IS NOT NULL THEN m.cle_exacte
         WHEN c.n_candidats = 1 AND NOT c.sans_cle
              AND NOT c.ligne_etrangere AND NOT c.contradictoire
              AND NOT c.ouverte_etrangere
              AND NOT (c.reservation_etrangere AND c.n_candidats = 0)
           THEN (SELECT min(spend_key) FROM candidats)
         WHEN c.n_candidats = 0 AND NOT c.sans_cle
              AND NOT c.ligne_etrangere AND NOT c.contradictoire
              AND NOT c.reservation_etrangere
           THEN m.agent || ':' || m.start_index || ':' || m.created_at
         ELSE NULL
       END AS ledger_key,
       (m.cle_exacte IS NULL
        AND (c.n_candidats > 1 OR c.sans_cle OR c.ligne_etrangere
             OR c.contradictoire OR c.ouverte_etrangere
             OR (c.reservation_etrangere AND c.n_candidats = 0)))
         AS ledger_ambiguous
  FROM marqueur m CROSS JOIN constat c
"""

    def put(self, name, start_index, turn, model, work_item_id=None, turn_id=None) -> bool:
        return self.put_get(name, start_index, turn, model,
                            work_item_id=work_item_id, turn_id=turn_id) is not None

    def put_get(self, name, start_index, turn, model,
                work_item_id=None, turn_id=None) -> dict | None:
        """Return THIS marker in its write, before any replacement can change it."""
        rows = self.db.query(
            """
            INSERT INTO spend_pending (agent, start_index, turn, model, work_item_id,
                                       turn_id, spend_key, created_at)
            VALUES (%s, %s, %s, %s, %s, %s,
                    %s || ':' || %s || ':' ||
                    to_char(now() at time zone 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                    now())
            ON CONFLICT (agent) DO UPDATE SET
                start_index  = excluded.start_index,
                turn         = excluded.turn,
                model        = excluded.model,
                work_item_id = excluded.work_item_id,
                turn_id      = excluded.turn_id,
                spend_key    = excluded.spend_key,
                resident_usage = null,
                created_at   = now()
            RETURNING agent, start_index, turn, model, work_item_id, turn_id,
                      resident_usage, spend_key,
                      """ + self._EXACT_CREATED_AT + """ as created_at,
                      extract(epoch from created_at)::float8 as created_ts
            """,
            (name, int(start_index), turn, model,
             None if work_item_id is None else int(work_item_id),
             turn_id or None, name, int(start_index)),
        )
        if not rows:
            return None
        row = rows[0]
        row["ledger_key"] = row.get("spend_key")
        row["ledger_ambiguous"] = False
        return row

    def set_model(self, name, model) -> bool:
        rows = self.db.query(
            "UPDATE spend_pending SET model = %s WHERE agent = %s RETURNING agent",
            (model if model is not None else "", name),
        )
        return bool(rows)

    def set_resident_usage(self, name, usage, created_at=None, spend_key=None) -> bool:
        """L80a : l'événement `resident.turn` d'un tour non réglé, dans le
        marqueur ; `created_at`/`spend_key` (#2166) bornent l'écriture au
        marqueur de CE tour, comparés en texte/timestamptz exacts."""
        value = None if usage is None else json.dumps(usage, ensure_ascii=False)
        if created_at is None:
            rows = self.db.query(
                "UPDATE spend_pending SET resident_usage = %s::jsonb"
                " WHERE agent = %s RETURNING agent", (value, name))
        else:
            rows = self.db.query(
                "UPDATE spend_pending SET resident_usage = %s::jsonb"
                " WHERE agent = %s"
                " AND created_at = %s::timestamptz"
                " AND spend_key IS NOT DISTINCT FROM %s"
                " RETURNING agent",
                (value, name, str(created_at), spend_key))
        return bool(rows)

    def get(self, name) -> dict | None:
        """Le marqueur et la clé durable de SON tour, en une seule lecture.

        `ledger_key` est la clé exacte du marqueur s'il la porte ; sinon celle
        de la ligne `turn_costs` ou de la réservation #1469 de ce tour,
        retrouvée par son identité (`turn_id`, `started_at`, époque de la clé),
        jamais par le seul index de début (réutilisé après une rotation) ni
        par ordre temporel : un ancien worker peut écrire sa ligne après le
        marqueur de son remplaçant (#1469). L'époque d'une clé historique est
        un arrondi float8 de l'instant du marqueur : elle est comparée aux
        arrondis exacts possibles (0 à 6 décimales), jamais à une fenêtre ; la
        ligne ou la réservation doit aussi suivre le marqueur, sinon elle est
        étrangère même au même arrondi d'une seconde. Aucun candidat : la clé
        exacte du marqueur. Candidats incohérents, réservation étrangère sans
        candidat ou encore ouverte, ligne étrangère au repli : `ledger_key`
        NULL et `ledger_ambiguous` vrai — l'appelant suspend, il ne devine pas.

        Une seule instruction : le marqueur lu et la clé résolue voient le
        même état, même si un remplaçant écrit entre-temps.
        """
        rows = self.db.query(
            self._get_sql(),
            (name,),
        )
        if not rows:
            return None
        row = rows[0]
        row["ledger_ambiguous"] = bool(row.get("ledger_ambiguous"))
        return row

    def clear(self, name) -> bool:
        rows = self.db.query("DELETE FROM spend_pending WHERE agent = %s RETURNING agent",
                             (name,))
        return bool(rows)

    def clear_if(self, name, created_at, spend_key=None) -> bool:
        """#1469 : efface le marqueur seulement s'il est encore celui de ce tour
        — même `created_at` exact et même `spend_key` (#2166), jamais celui
        d'un remplaçant, en une instruction."""
        rows = self.db.query(
            "DELETE FROM spend_pending WHERE agent = %s"
            " AND created_at = %s::timestamptz"
            " AND spend_key IS NOT DISTINCT FROM %s"
            " RETURNING agent", (name, str(created_at), spend_key))
        return bool(rows)
