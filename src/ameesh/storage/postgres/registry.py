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

from ameesh import work_lifecycle as lifecycle

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
    -- #4293 : binaire du harnais pour cet agent (canari/bascule d'adoption)
    harness_bin,
    budget_usd, spent_usd, turns, pending_prompt, current_prompt, last_error,
    -- L165 (EP3) : brief de reprise d'une relève élue, lu une fois par le
    -- nouveau détenteur (contenu libre, jamais lisible par le superviseur)
    election_brief,
    lease_owner, lease_epoch, runner_seen_host, runner_seen_owner,
    extract(epoch from runner_seen_at)::float8 as runner_seen_ts,
    extract(epoch from lease_wait_since)::float8 as lease_wait_since_ts,
    extract(epoch from lease_since)::float8 as lease_since_ts,
    extract(epoch from lease_expires_at)::float8 as lease_expires_ts,
    extract(epoch from last_seen)::float8         as last_seen_ts,
    extract(epoch from created_at)::float8        as created_ts,
    extract(epoch from last_turn_at)::float8      as last_turn_ts,
    extract(epoch from last_event_at)::float8     as last_event_ts,
    responsible, team, projects, provider, credential_mode, capabilities, roles,
    canon_ref, canon,
    -- L165 (EP1) : relève (`hote`/`election`) et hôtes candidats d'un rôle élu
    releve, candidats,
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
    extract(epoch from operator_pause_at)::float8 as operator_pause_ts,
    extract(epoch from operator_pause_until)::float8 as operator_pause_until_ts,
    -- #3460 (0484) : retrait d'un agent (`ameesh agent retire`)
    retired_by, retired_reason, review_hold,
    extract(epoch from retired_at)::float8 as retired_ts
"""


# #5099 : get() sert aussi au courrier AVANT la pose des migrations.
# Lire les colonnes présentes, puis projeter le contrat connu, évite qu'une
# nouvelle colonne additive rende ce courrier impossible sur l'ancien schéma.
_GET_FIELDS = [part.strip() for part in re.sub(r"--[^\n]*", "", AGENT_COLUMNS).split(",")]
_GET_NAMES = [part.rsplit(" as ", 1)[-1].strip() for part in _GET_FIELDS]
_GET_NAMES += ["last_mail_ts", "last_mail_host"]
_GET_TIMESTAMPS = [re.sub(
    r"from (\w+)", r"from (to_jsonb(agent_registry)->>'\1')::timestamptz", part)
    for part in _GET_FIELDS if part.startswith("extract(")]
GET_COLUMNS = "agent_registry.*, " + ", ".join(_GET_TIMESTAMPS)


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


def _host_expr(alias: str, host: str | None = None) -> str:
    """Expression SQL de l'hôte qui réclame : la ligne, ou un hôte validé."""
    if host is None:
        return "%s.host" % _alias(alias)
    # NAME_RE interdit déjà les guillemets ; on garde l'échappement explicite.
    return "'%s'" % check_name(host).replace("'", "''")


def _pinned_placement_sql(alias: str) -> str:
    """Verdict de placement de l'agent épinglé sur l'hôte de SA ligne.

    ADMISSION seule : l'état du canon de l'hôte est jugé séparément par
    `canon_claim_predicate_sql`, jamais ici (un canon momentanément invalide
    ne bloque pas une attribution, seule une réclamation)."""
    a = _alias(alias)
    return """(coalesce(%(a)s.placement_ok, false)
       AND %(a)s.placement_profile IS NOT DISTINCT FROM ameesh_placement_profile(
               %(a)s.host, %(a)s.harness, %(a)s.provider, %(a)s.model,
               %(a)s.credential_mode))""" % {"a": a}


def _election_placement_sql(alias: str, host: str | None = None) -> str:
    """Verdict de placement de l'agent élu sur l'hôte candidat qui le réclame.

    L'hôte de la ligne n'est que le dernier détenteur (ou l'hôte de la
    dernière synchronisation) : le verdict qui compte est celui du candidat
    qui prend le bail, écrit par `canon sync` sur cet hôte dans
    `agent_candidates` (L165, EP1). Le profil évalué est comparé aux colonnes
    de la ligne, donc une inscription, un import ou un SQL à la main qui
    change harnais, fournisseur, modèle ou mode ferme la réclamation.
    """
    a = _alias(alias)
    h = _host_expr(a, host)
    return """(%(a)s.releve = 'election'
       AND %(h)s = ANY(%(a)s.candidats)
       AND %(h)s = ANY(%(a)s.admitted_hosts)
       AND EXISTS (SELECT 1 FROM agent_candidates c
                    WHERE c.name = %(a)s.name AND c.host = %(h)s
                      AND c.canon = coalesce(%(a)s.canon, '')
                      AND c.placement_ok
                      AND c.placement_profile IS NOT DISTINCT FROM ameesh_placement_profile(
                              %(h)s, %(a)s.harness, %(a)s.provider, %(a)s.model,
                              %(a)s.credential_mode)))""" % {"a": a, "h": h}


def canon_claim_predicate_sql(alias: str = "agent_registry", *, probe: bool = False,
                              host: str | None = None) -> str:
    """Condition SQL de réclamation liée au canon (§4.1), pour un `WHERE`.

    Vraie si l'agent de la ligne `alias` (une ligne de `agent_registry`)
    n'est pas retiré (#3460), et s'il n'est pas gouverné par le canon
    (`canon_governed_sql`), ou si le dernier état de **son canon** sur l'hôte
    qui le réclame (`canon_state`, clé (host, canon) depuis L42/0031 ; colonne
    `canon` de la ligne, NULL = canon par défaut ; écrit par
    `ameesh canon sync`) est `ok` **et** que son placement sur cet hôte est
    admis par la politique de l'hôte (`placement_ok`, colonne déclarative
    écrite par `canon sync`, recopiée du créateur pour un éphémère ; C4, 0022)
    **pour le profil qu'il a maintenant** : `placement_profile` (le profil
    évalué, écrit avec le verdict) égal à `ameesh_placement_profile` de ses
    colonnes hôte, harnais, fournisseur, modèle et mode d'identifiants. Un
    canon `invalid` ou `unreadable`, l'absence d'état, un placement refusé,
    absent de cet hôte ou pas encore évalué (`placement_ok` faux ou NULL), un
    profil divergé depuis l'évaluation (inscription, import ou SQL à la main)
    refusent donc toute NOUVELLE réclamation d'un agent du canon (et de ses
    éphémères) jusqu'au prochain `canon sync`, sans rien toucher aux baux,
    sessions ni tours en cours : fail closed.

    #3460 : un agent retiré (`retired_at`, `ameesh agent retire`) n'est
    jamais réclamable. La colonne n'est que sur `agent_registry`, pas sur la
    vue `agent_mesh_overview` : `alias` nomme une ligne du registre.

    `host` (L165, EP1) : l'hôte qui réclame, pour un agent `releve: election`.
    La condition porte alors sur le verdict de placement de CET hôte candidat
    (`agent_candidates`) et sur son état de canon, pas sur l'hôte de la ligne.
    None : l'hôte de la ligne. Un agent `hote` ignore ce paramètre : son
    verdict reste celui de sa ligne.

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

    Un agent `releve: election` n'est pas fencé par L232e : le pilote n'a pas
    de travail local à publier, et le bail (epoch) suffit à fencer l'ancien
    détenteur (EP1).
    """
    a = _alias(alias)
    # L42 (0031) : l'état est celui du canon DE L'AGENT sur l'hôte qui réclame
    # (colonne `canon` ; NULL = canon par défaut, clé '')
    h = _host_expr(a, host)
    open_sql = ("ameesh_agent_claims_open(%s, %s.name)" % (h, a) if probe
                else "ameesh_host_claims_open(%s)" % h)
    sql = """(%(open)s
       AND %(a)s.retired_at IS NULL
       AND (%(a)s.releve = 'election' OR NOT ameesh_handoff_fenced(%(a)s.name))
       AND (NOT %(governed)s
            OR (EXISTS (SELECT 1 FROM canon_state s
                         WHERE s.host = %(h)s
                           AND s.canon = coalesce(%(a)s.canon, '')
                           AND s.status = 'ok')
                AND CASE WHEN %(a)s.releve = 'election' THEN %(election)s
                         ELSE %(pinned)s END)))"""
    return sql % {"governed": canon_governed_sql(a), "a": a,
                  "h": _host_expr(a, host), "open": open_sql,
                  "election": _election_placement_sql(a, host),
                  "pinned": _pinned_placement_sql(a)}


def placement_admitted_sql(alias: str = "agent_registry",
                           *, host: str | None = None) -> str:
    """L46 : l'ADMISSION seule (placement admis pour le profil actuel), sans
    l'état du canon de l'hôte — ce que `registry.wakeable` juge : un canon
    momentanément invalide ne bloque pas une attribution (la réclamation,
    elle, reste fermée par `canon_claim_predicate_sql`).

    `host` (L165, EP1) : l'hôte candidat jugé pour un agent `releve: election`
    (None : l'hôte de la ligne)."""
    a = _alias(alias)
    return """(NOT %(governed)s
       OR (CASE WHEN %(a)s.releve = 'election' THEN %(election)s
                ELSE %(pinned)s END))""" % {
        "governed": canon_governed_sql(a), "a": a,
        "election": _election_placement_sql(a, host),
        "pinned": _pinned_placement_sql(a)}


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
                       expect_reason=None, not_after=None, until=None,
                       indefinite=False) -> dict | None:
        # #2152 : reprise conditionnelle (une seule instruction SQL, verrou de
        # ligne) ; `expect_reason` n'a de sens que pour LEVER une pause.
        if expect_reason is not None:
            if paused:
                raise ValueError("expect_reason only lifts an operator pause")
            rows = self.db.query(
                "SELECT * FROM ameesh_operator_resume_if(%s, %s, %s, %s, %s)",
                (name, reason, actor, principal, expect_reason))
            return rows[0] if rows else None
        if paused and (until is not None or indefinite):
            # #3961 : pose avec échéance (ou sans fin, choix explicite) ; la
            # barrière `not_after` (#2184/R3) passe par la même instruction.
            if until is not None and indefinite:
                raise ValueError("until and indefinite are exclusive")
            rows = self.db.query(
                "SELECT * FROM ameesh_operator_pause_until("
                "%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
                (name, reason, actor, principal, until, not_after))
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

    def operator_pause_expire(self, name=None) -> list[dict]:
        return self.db.query("SELECT * FROM ameesh_operator_pause_expire(%s)", (name,))

    def retire(self, name, *, actor, reason) -> dict | None:
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT name, retired_at IS NOT NULL AS retired, lease_owner,"
                "       (lease_owner IS NOT NULL"
                "        AND lease_expires_at > clock_timestamp()) AS lease_live"
                "  FROM agent_registry"
                " WHERE name = (SELECT ameesh_resolve_agent_name(%s, %s)) FOR UPDATE",
                (name, "agents.retire"))
            if not rows:
                return None
            row = rows[0]
            agent = row["name"]
            if row["retired"]:
                return {"name": agent, "changed": False, "blockers": []}
            blockers = []
            if row["lease_live"]:
                blockers.append({"kind": "lease", "owner": row["lease_owner"]})
            blockers += [{"kind": "lot", "id": int(lot["id"]), "state": lot["state"]}
                         for lot in tx.query(
                             # #3460 (B de dev3) : un lot livré (`merged`,
                             # `promoted`) garde son assigné mais ne retient pas
                             "SELECT id, state FROM work_items"
                             " WHERE assignee = %s"
                             f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                             " ORDER BY id",
                             (agent,))]
            blockers += [{"kind": "external_executor", "id": int(lot["id"]),
                          "state": lot["state"]}
                         for lot in tx.query(
                             "SELECT id, state FROM work_items"
                             " WHERE external_executor = %s"
                             f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                             " ORDER BY id", (agent,))]
            blockers += [{"kind": "watch", "host": watch["host"]}
                         for watch in tx.query(
                             "SELECT host FROM agent_watches WHERE agent = %s", (agent,))]
            if blockers:
                return {"name": agent, "changed": False, "blockers": blockers}
            tx.execute(
                "UPDATE agent_registry SET retired_at = clock_timestamp(),"
                "       retired_by = %s, retired_reason = %s, updated_at = now()"
                " WHERE name = %s",
                (actor, reason, agent))
            return {"name": agent, "changed": True, "blockers": []}

    def restore(self, name, *, actor) -> dict | None:
        rows = self.db.query(
            "SELECT name, retired_at IS NOT NULL AS retired FROM agent_registry"
            " WHERE name = (SELECT ameesh_resolve_agent_name(%s, %s))",
            (name, "agents.restore"))
        if not rows:
            return None
        agent = rows[0]["name"]
        changed = self.db.query(
            "UPDATE agent_registry SET retired_at = NULL, retired_by = NULL,"
            "       retired_reason = NULL, updated_at = now()"
            " WHERE name = %s AND retired_at IS NOT NULL RETURNING name",
            (agent,))
        return {"name": agent, "changed": bool(changed)}

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

    def review_hold(self, name, *, reason, actor, clear=False) -> dict | None:
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT name, review_hold FROM agent_registry"
                " WHERE name = ameesh_resolve_agent_name(%s, 'review_hold') FOR UPDATE",
                (name,))
            if not rows:
                return None
            row = rows[0]
            value = None if clear else {"reason": reason, "actor": actor}
            changed = row["review_hold"] != value
            if changed:
                tx.execute("UPDATE agent_registry SET review_hold = %s::jsonb WHERE name = %s",
                           (json.dumps(value) if value is not None else None, row["name"]))
                from .events import Events
                Events(tx).record("agent.review_hold", "agent:" + row["name"], None,
                                  {"agent": row["name"], "actor": actor, "reason": reason, "cleared": clear})
            return {"name": row["name"], "review_hold": value, "changed": changed}

    def get(self, name) -> dict | None:
        # #3729 : le dernier message déposé et son hôte, comme `overview`
        # (#3337) — `ameesh show` lit la même preuve de vie que `list`.
        rows = self.db.query(
            """
            SELECT %s, presence.last_mail_ts, presence.last_mail_host
              FROM agent_registry
              LEFT JOIN LATERAL (
                  SELECT extract(epoch from m.created_at)::float8 AS last_mail_ts,
                         m.host AS last_mail_host
                    FROM agent_mailbox m
                   WHERE m.sender = agent_registry.name
                   ORDER BY m.id DESC LIMIT 1) presence ON true
             WHERE name = (SELECT ameesh_resolve_agent_name(%%s, %%s))
            """ % GET_COLUMNS,
            (name, "agents.get"))
        return {key: rows[0].get(key) for key in _GET_NAMES} if rows else None

    def resolve_name(self, name, *, operation="lookup") -> str:
        return self.db.query("SELECT ameesh_resolve_agent_name(%s, %s) AS name",
                             (name, operation))[0]["name"]

    def same_identity(self, first, second) -> bool:
        first, second = str(first or "").strip(), str(second or "").strip()
        if not first or not second:
            return False
        if first.casefold() == second.casefold():
            return True
        rows = self.db.query(
            "SELECT EXISTS (SELECT 1 FROM agent_registry r"
            "                WHERE (r.name = %s OR %s = ANY(r.aliases))"
            "                  AND (r.name = %s OR %s = ANY(r.aliases))) AS same",
            (first, first, second, second))
        return bool(rows and rows[0]["same"])

    def harnesses(self) -> dict[str, str]:
        rows = self.db.query("SELECT name, harness FROM agent_registry")
        return {row["name"]: row["harness"] or "" for row in rows}

    def overview(self) -> list[dict]:
        return self.db.query(
            """
            SELECT name, (SELECT r.review_hold FROM agent_registry r WHERE r.name=o.name) AS review_hold,
                   (SELECT r.aliases FROM agent_registry r WHERE r.name=o.name) AS aliases,
                   -- #3185 : projets relus hors équipe (la vue ne porte pas la colonne)
                   (SELECT r.relit FROM agent_registry r WHERE r.name=o.name) AS relit,
                   -- #3460 : retrait (la vue ne porte pas la colonne)
                   (SELECT extract(epoch from r.retired_at)::float8 FROM agent_registry r
                     WHERE r.name=o.name) AS retired_ts,
                   chantier, harness, host, cwd, session_id, status, status_text,
                   model,
                   -- #4293 : colonne absente de la vue figée ; lue sur la ligne
                   (SELECT r.harness_bin FROM agent_registry r
                     WHERE r.name = o.name) AS harness_bin,
                   budget_usd, spent_usd, turns, pending_prompt, last_error,
                   -- #3337 : un message déposé est un signe de vie distinct de
                   -- `last_seen` (hook/exécuteur). L'index (sender, id desc)
                   -- rend la lecture ponctuelle ; une identité `externe` écrit
                   -- sans exécuteur, son `last_seen` ne bouge donc pas.
                   (SELECT extract(epoch from m.created_at)::float8
                      FROM agent_mailbox m WHERE m.sender = o.name
                     ORDER BY m.id DESC LIMIT 1) AS last_mail_ts,
                   -- #3729 : l'hôte du dernier dépôt, pour nommer un signe de
                   -- vie venu d'ailleurs que l'hôte déclaré.
                   (SELECT m.host FROM agent_mailbox m WHERE m.sender = o.name
                     ORDER BY m.id DESC LIMIT 1) AS last_mail_host,
                   lease_owner, lease_epoch, lease_expires_ts, last_seen_ts, last_turn_ts,
                   public_key_fingerprint, key_role, key_ready, has_owner_key, unread,
                   passive_unread,
                   responsible, team, projects, provider, credential_mode, capabilities,
                   -- #4779 : les rôles déclarés, requis pour la désignation des
                   -- relecteurs (la vue les porte depuis 0483)
                   roles,
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
                   p.operator_pause_for, p.operator_pause_ts, p.operator_pause_until_ts,
                   __GOVERNED__ AS canon_governed,
                   (SELECT s.status FROM canon_state s WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_status,
                   (SELECT s.diagnostic FROM canon_state s
                     WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_diagnostic,
                   (SELECT extract(epoch from s.checked_at)::float8 FROM canon_state s
                     WHERE s.host = o.host
                       AND s.canon = coalesce(o.canon, '')) AS canon_checked_ts,
                   -- L165 (EP1) : la vue est figée à sa création et ne porte
                   -- pas `releve`/`candidats` ; le prédicat les lit sur la
                   -- ligne du registre, sous-requête plutôt qu'une jointure
                   -- pour ne pas qualifier toutes les colonnes de la vue.
                   (SELECT __CLAIM_OK__ FROM agent_registry r
                     WHERE r.name = o.name) AS canon_claim_ok,
                   -- #1250 : le harnais de la session VIVANTE (0384), absent de
                   -- la vue dont les colonnes sont figées à sa création ;
                   -- sous-requête pour ne pas qualifier toutes les colonnes.
                   (SELECT r.session_harness FROM agent_registry r
                     WHERE r.name = o.name) AS session_harness
            FROM agent_mesh_overview o
            LEFT JOIN LATERAL (
                SELECT operator_paused, operator_pause_reason, operator_pause_by,
                       operator_pause_for,
                       extract(epoch from operator_pause_at)::float8 AS operator_pause_ts,
                       extract(epoch from operator_pause_until)::float8
                           AS operator_pause_until_ts
                  FROM agent_registry WHERE name = o.name
            ) p ON true
            ORDER BY last_seen_ts DESC NULLS LAST
            """.replace("__GOVERNED__", canon_governed_sql("o"))
               .replace("__CLAIM_OK__", canon_claim_predicate_sql("r"))
        )

    def reachability(self) -> list[dict]:
        # #3631 : la passe de redésignation n'a besoin que de ces sept
        # colonnes ; `overview()` y ajoutait le canon, les baux, les jauges et
        # des colonnes larges (`pending_prompt`, `last_error`), et expirait
        # sous la pression de la base commune. `retired_ts` est la forme que
        # lit `review_designation.is_retired` ; `status_text` porte le préfixe
        # `auth_blocked` (#4491), court et nécessaire pour que le siège bloqué
        # ne soit pas laissé en place.
        return self.db.query(
            "SELECT name, aliases, review_hold,"
            "       extract(epoch from retired_at)::float8 AS retired_ts,"
            "       mode, status, status_text, host"
            "  FROM agent_registry"
            " ORDER BY name")

    def stopped_with(self, reasons) -> list[str]:
        # #3907 : le balayage #753 de chaque passe ne lit plus `overview()` ;
        # `status` et `stop_reason` sont les colonnes du registre que la vue
        # recopie telles quelles.
        reasons = sorted({str(reason) for reason in reasons or () if reason})
        if not reasons:
            return []
        rows = self.db.query(
            "SELECT name FROM agent_registry"
            " WHERE status = 'stopped' AND stop_reason IN (%s)"
            " ORDER BY name" % ", ".join(["%s"] * len(reasons)), tuple(reasons))
        return [row["name"] for row in rows]

    def claimable(self, host, names, *, require_responsible) -> list[dict]:
        # L165 (EP1) : un agent `releve: election` est visible sur tout hôte
        # candidat, même si sa ligne est encore sur un autre. Le dossier et
        # l'hôte rendus sont ceux du candidat qui va réclamer.
        sql = """
            SELECT %s,
                   CASE WHEN releve = 'election' THEN
                     (SELECT c.cwd FROM agent_candidates c
                       WHERE c.name = agent_registry.name AND c.host = %%s
                         AND c.canon = coalesce(agent_registry.canon, ''))
                   ELSE cwd END AS claim_cwd
            FROM agent_registry
            WHERE ((releve <> 'election' AND host = %%s)
                   OR (releve = 'election' AND %%s = ANY(candidats)))
              AND status <> 'stopped'
              -- L46 : un agent `externe` (session humaine) n'est jamais mené
              -- par l'exécuteur, même avec une session et du courrier.
              AND mode = 'execute'
              AND (lease_owner IS NULL OR lease_expires_at < clock_timestamp())
              AND (NOT ephemeral OR ephemeral_expires_at > clock_timestamp())
              -- #254 : un hôte en pause ne prend aucun bail
              AND NOT EXISTS (SELECT 1 FROM host_holds hh
                               WHERE hh.host = CASE WHEN releve = 'election' THEN %%s
                                                    ELSE agent_registry.host END
                                 AND hh.until > clock_timestamp())
              AND %s
        """ % (AGENT_COLUMNS, canon_claim_predicate_sql("agent_registry", host=host))
        if require_responsible:
            sql += " AND coalesce(responsible, '') <> ''"
        params: list[Any] = [host, host, host, host]
        if names:
            sql += " AND name IN (%s)" % name_list(names)
        sql += " ORDER BY last_seen"
        rows = self.db.query(sql, tuple(params))
        for row in rows:
            row["cwd"] = row.pop("claim_cwd", row.get("cwd"))
            # L165 (EP3) : l'hôte de la ligne reste l'ancien détenteur (ou
            # l'hôte de la dernière synchronisation). Le candidat qui va
            # réclamer a besoin de le connaître pour distinguer une relève
            # croisée d'une simple reprise sur le même hôte (briefing, session
            # neuve) ; `host` reste, lui, l'hôte qui réclame.
            row["previous_host"] = row.get("host")
            row["host"] = host
        return rows

    def runner_seen(self, host, owner, names) -> None:
        write_runner_seen(self.db, host, owner, names)

    def note_unleased(self, name, host, error) -> bool:
        if not error.startswith("lease_not_taken: "):
            raise ValueError("unleased diagnostic must start with lease_not_taken: ")
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, host, mode, status, lease_owner, lease_expires_at,
                       last_error, releve, candidats
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET last_error = %s, updated_at = now()
              FROM verrou
             WHERE r.name = verrou.name
               AND (verrou.host = %s
                    OR (verrou.releve = 'election' AND %s = ANY(verrou.candidats)))
               AND verrou.mode = 'execute'
               AND verrou.status IN ('idle', 'queued', 'dead')
               AND (verrou.lease_owner IS NULL OR verrou.lease_expires_at IS NULL
                    OR verrou.lease_expires_at <= clock_timestamp())
               AND verrou.last_error IS DISTINCT FROM %s
            RETURNING r.name
            """, (name, error, host, host, error))
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
        # elle décrit cette session, inconnue sinon (déclencheur de 0384).
        # L165 (EP3) : une session ouverte consomme le brief de relève ; le
        # garder le ferait réapparaître à la prochaine session neuve.
        if harness is not None or cwd is not None:
            self.db.execute(
                "UPDATE agent_registry SET session_id = %s, session_account = %s,"
                " session_harness = %s, session_cwd = %s, session_origin = %s,"
                " election_brief = NULL,"
                " updated_at = now() WHERE name = %s",
                (session_id, account, harness, cwd, session_id, name),
            )
        else:
            self.db.execute(
                "UPDATE agent_registry SET session_id = %s, session_account = %s,"
                " election_brief = NULL,"
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
                   r.canon_ref, r.placement_ok, r.placement_diagnostic, r.releve,
                   -- #3460 : le retrait est un état, pas une attente de canon
                   extract(epoch from r.retired_at)::float8 AS retired_ts,
                   -- #2200 : la pause opérateur fait partie de la disponibilité
                   r.operator_paused, r.operator_pause_reason, r.operator_pause_by,
                   extract(epoch from r.operator_pause_until)::float8
                       AS operator_pause_until_ts,
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

    def claim(self, name, owner, ttl_seconds, *, require_responsible,
              host=None, election_brief=None) -> dict | None:
        """Prend le bail s'il est libre ou expiré. Renvoie la ligne, sinon None.

        Le verrou de ligne est pris **d'abord** (`SELECT ... FOR UPDATE`), puis
        les conditions sont recontrôlées avec `clock_timestamp()` dans
        l'instruction qui écrit : un verrou attendu au-delà de l'échéance
        refuse la réclamation, même si le détenteur n'a fait qu'un `SELECT ...
        FOR UPDATE` sans modifier la ligne (EvalPlanQual ne réévalue pas un
        `WHERE` sans nouvelle version — grille codex3). Mêmes règles que
        `claimable`, dans la même instruction.

        L165 (EP1) : pour un agent `releve: election`, `host` est l'hôte
        candidat qui réclame. La réclamation déplace la ligne sur cet hôte
        (dossier et verdict de placement du candidat, lus dans
        `agent_candidates`) et lève la clôture L232e : le pilote n'a pas de
        travail local à publier, le bail et son epoch suffisent. Un agent
        `hote` ignore `host` et ne peut être réclamé que sur l'hôte de sa
        ligne.

        L165 (EP3) : `election_brief` est le briefing de reprise construit par
        l'hôte qui gagne une relève CROISÉE (l'ancien détenteur est un autre
        hôte). La même instruction oublie la session de l'ancien hôte
        (`session_id`, `session_work_item`, `session_account`, `session_reset_at`),
        écrit le brief dans `election_brief` (résumé de la session neuve),
        met une consigne courte EN TÊTE de `pending_prompt` et passe l'agent
        `queued` : aucun état « bail pris sans briefing » n'est visible, et
        l'ancien détenteur reste fencé par l'epoch. Même hôte (redémarrage de
        l'exécuteur) ou agent `hote` : le brief est ignoré, la session est
        gardée. Une relève croisée SANS brief (`p.brief` NULL, cas du contrat
        médié qui le force) est **refusée** : le CAS ne déplace pas la ligne,
        sinon l'hôte changerait en gardant la session et le compte de
        l'ancien. Rend en plus `previous_host`, `previous_owner`,
        `previous_epoch`, `previous_session` et `elected` (vrai seulement si
        le brief a été appliqué).
        """
        h = _host_expr("r", host)
        # L165 (EP3) : une relève n'est croisée que si la ligne était encore
        # chez un AUTRE hôte. Le brief ne peut alors pas être un reste d'un
        # tour local : la session de l'ancien hôte n'est jamais reprise ici.
        takeover = """(p.brief IS NOT NULL AND r.releve = 'election'
                       AND verrou.host IS DISTINCT FROM __H__)"""
        r5 = """CASE
                           WHEN r.current_prompt IS NOT NULL
                                AND (r.lease_owner IS DISTINCT FROM %s
                                     OR r.lease_expires_at IS NULL
                                     OR r.lease_expires_at < clock_timestamp())
                           THEN CASE
                               WHEN r.pending_prompt IS NULL THEN r.current_prompt
                               ELSE r.current_prompt || E'\\n\\n' || r.pending_prompt
                           END
                           ELSE r.pending_prompt END"""
        sql = """
            WITH verrou AS (
                SELECT name, host, session_id, lease_owner, lease_epoch
                  FROM agent_registry WHERE name = %s FOR UPDATE
            ), p AS (SELECT %s::text AS brief)
            UPDATE agent_registry AS r
               SET lease_owner      = %s,
                   lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                   lease_epoch      = r.lease_epoch + 1,
                   -- L165 (EP1) : le détenteur élu est l'hôte qui réclame ;
                   -- son dossier et son verdict de placement sont ceux de ce
                   -- candidat, pour que le profil reste celui de l'hôte.
                   host             = CASE WHEN r.releve = 'election' THEN __H__
                                            ELSE r.host END,
                   cwd              = CASE WHEN r.releve = 'election' THEN
                         (SELECT c.cwd FROM agent_candidates c
                           WHERE c.name = r.name AND c.host = __H__
                             AND c.canon = coalesce(r.canon, ''))
                       ELSE r.cwd END,
                   placement_ok     = CASE WHEN r.releve = 'election' THEN
                         (SELECT c.placement_ok FROM agent_candidates c
                           WHERE c.name = r.name AND c.host = __H__
                             AND c.canon = coalesce(r.canon, ''))
                       ELSE r.placement_ok END,
                   placement_diagnostic = CASE WHEN r.releve = 'election' THEN
                         (SELECT c.placement_diagnostic FROM agent_candidates c
                           WHERE c.name = r.name AND c.host = __H__
                             AND c.canon = coalesce(r.canon, ''))
                       ELSE r.placement_diagnostic END,
                   placement_ref    = CASE WHEN r.releve = 'election' THEN
                         (SELECT c.placement_ref FROM agent_candidates c
                           WHERE c.name = r.name AND c.host = __H__
                             AND c.canon = coalesce(r.canon, ''))
                       ELSE r.placement_ref END,
                   placement_profile = CASE WHEN r.releve = 'election' THEN
                         (SELECT c.placement_profile FROM agent_candidates c
                           WHERE c.name = r.name AND c.host = __H__
                             AND c.canon = coalesce(r.canon, ''))
                       ELSE r.placement_profile END,
                   handoff_fenced   = CASE WHEN r.releve = 'election' THEN false
                                           ELSE r.handoff_fenced END,
                   -- R5 : si le bail n'était plus vivant (autre main, ou même
                   -- exécuteur relancé avec le même identifiant), la consigne
                   -- inachevée repart en attente. Si une consigne était déjà en
                   -- attente, les deux sont conservées (B6b, B6c).
                   -- L165 (EP3) : sur une relève croisée, le brief de reprise
                   -- est écrit à part (`election_brief`, lu comme résumé de la
                   -- session neuve) et une consigne courte ouvre le premier
                   -- tour ; la consigne inachevée de l'ancien hôte la suit.
                   pending_prompt   = CASE WHEN __TAKEOVER__
                       THEN concat_ws(E'\n\n',
                                      'Elected on host ' || __H__ || ': your session is '
                                      'new; read the resume briefing that opens this turn, '
                                      'then check the mesh state (lots, mail, decisions, '
                                      'alerts) before acting.', __R5__)
                       ELSE __R5__ END,
                   current_prompt   = CASE
                       WHEN __TAKEOVER__ THEN NULL
                       WHEN r.lease_owner IS DISTINCT FROM %s
                            OR r.lease_expires_at IS NULL
                            OR r.lease_expires_at < clock_timestamp()
                       THEN NULL
                       ELSE r.current_prompt END,
                   -- L165 (EP3) : briefing persisté de la relève, consommé par
                   -- le nouveau détenteur ; aucune session ne traverse un hôte.
                   election_brief   = CASE WHEN __TAKEOVER__ THEN p.brief
                                           ELSE r.election_brief END,
                   -- L165 (EP3) : aucune session ne traverse un hôte. La
                   -- remise à zéro est durable (`session_reset_at`), donc un
                   -- fichier local périmé ne ressuscite pas la session, et
                   -- l'ancien compte est laissé au choix de cet hôte.
                   session_id       = CASE WHEN __TAKEOVER__ THEN NULL
                                           ELSE r.session_id END,
                   session_work_item = CASE WHEN __TAKEOVER__ THEN NULL
                                            ELSE r.session_work_item END,
                   session_reset_at = CASE WHEN __TAKEOVER__ THEN clock_timestamp()
                                           ELSE r.session_reset_at END,
                   session_account  = CASE WHEN __TAKEOVER__ THEN NULL
                                           ELSE r.session_account END,
                   -- une demande de redémarrage de l'ancien hôte ne doit pas
                   -- effacer la session neuve que ce brief vient d'ouvrir.
                   restart_brief    = CASE WHEN __TAKEOVER__ THEN NULL
                                           ELSE r.restart_brief END,
                   restart_requested_at = CASE WHEN __TAKEOVER__ THEN NULL
                                           ELSE r.restart_requested_at END,
                   status           = CASE
                       WHEN __TAKEOVER__ AND r.status <> 'stopped' THEN 'queued'
                       WHEN r.status = 'dead' THEN 'idle'
                       ELSE r.status END,
                   status_text      = CASE
                       WHEN __TAKEOVER__ AND r.status <> 'stopped'
                       THEN 'elected: new session from resume brief'
                       ELSE r.status_text END,
                   last_error       = CASE
                       WHEN starts_with(coalesce(r.last_error, ''), 'lease_not_taken: ')
                       THEN NULL ELSE r.last_error END,
                   updated_at       = now()
              FROM verrou, p
             WHERE r.name = verrou.name
               -- L46 : même règle que `claimable` — jamais un agent `externe`.
               AND r.mode = 'execute'
               AND (r.releve = 'election' OR r.host = __H__)
               -- L165 (EP3) : une relève CROISÉE d'un rôle élu ne se fait
               -- jamais sans brief. Sans lui, la ligne changerait d'hôte en
               -- gardant la session et le compte de l'ancien (le contrat médié
               -- force le brief à null) : le CAS refuse, sans rien écrire.
               AND (r.releve <> 'election' OR verrou.host = __H__
                    OR p.brief IS NOT NULL)
               AND (r.lease_owner IS NULL OR r.lease_owner = %s
                    OR r.lease_expires_at < clock_timestamp())
               AND (NOT r.ephemeral OR r.ephemeral_expires_at > clock_timestamp())
               -- #254 : sur un hôte en pause, aucun nouveau bail (le sien,
               -- encore vivant, se renouvelle)
               AND ((r.lease_owner = %s AND r.lease_expires_at >= clock_timestamp())
                    OR NOT EXISTS (SELECT 1 FROM host_holds hh
                                    WHERE hh.host = CASE WHEN r.releve = 'election'
                                                         THEN __H__ ELSE r.host END
                                      AND hh.until > clock_timestamp()))
               __RESPONSABLE__
               AND (r.releve = 'election' OR NOT r.handoff_fenced)
               AND __CANON__
            RETURNING r.name, r.lease_owner, r.lease_epoch, r.host, r.cwd,
                      r.election_brief,
                      verrou.host AS previous_host,
                      verrou.lease_owner AS previous_owner,
                      verrou.lease_epoch AS previous_epoch,
                      verrou.session_id AS previous_session,
                      __TAKEOVER__ AS elected,
                      extract(epoch from r.session_reset_at)::float8 as session_reset_ts,
                      extract(epoch from r.lease_expires_at)::float8 as lease_expires_ts
        """
        # `__TAKEOVER__` porte lui-même `__H__` : le remplacer AVANT l'hôte.
        sql = sql.replace("__TAKEOVER__", takeover)
        sql = sql.replace("__H__", h)
        sql = sql.replace("__R5__", r5)
        sql = sql.replace("__RESPONSABLE__",
                          "AND coalesce(r.responsible, '') <> ''" if require_responsible else "")
        # L232b : réclamation d'un agent nommé (pas de tour du contrôleur) —
        # seul l'agent sonde de la fermeture de release rouvre la condition.
        sql = sql.replace("__CANON__", canon_claim_predicate_sql("r", probe=True, host=host))
        rows = self.db.query(sql, (name, election_brief, owner, ttl_seconds,
                                   owner, owner, owner, owner, owner))
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
               AND (r.releve = 'election' OR NOT r.handoff_fenced)
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
        l'expiration est refusé (grille codex3).

        Le renouvellement ne raccourcit jamais l'échéance : `GREATEST` avec
        l'échéance tenue, calculé sous le verrou de ligne, garde le plus tardif
        des deux (#3115). Une lecture faite avant le verrou (ou avec l'horloge
        de l'hôte) ne peut donc plus écraser un renouvellement concurrent.
        """
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET lease_expires_at = GREATEST(
                       r.lease_expires_at,
                       clock_timestamp() + make_interval(secs => %s)),
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

        L165 (EP3) : un brief de relève non consommé part avec la session ; il
        décrivait la session qu'on oublie et ne doit pas rouvrir la suivante.
        """
        rows = self.db.query(
            """
            WITH verrou AS (
                SELECT name, lease_owner, lease_epoch, lease_expires_at
                  FROM agent_registry WHERE name = %s FOR UPDATE
            )
            UPDATE agent_registry AS r
               SET session_id = NULL, session_work_item = NULL,
                   election_brief = NULL,
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
                 cost_usd, turn_id=None) -> bool:
        from .sandbox import ameesh_turn_close
        with self.db.transaction() as tx:
            return ameesh_turn_close(
                tx, name, owner, epoch, status=status, status_text=status_text,
                error=error, cost_usd=cost_usd, turn_id=turn_id)


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
    #: pouvant atteindre 0,5 s, pas 123 ms. À -6, 9 chiffres significatifs
    #: arrondissent aux dizaines de secondes ; à -7, aux centaines. On couvre
    #: donc -2 à 6 décimales : l'époque de la clé doit être EXACTEMENT l'un
    #: des arrondis que l'ancienne connexion a pu produire (les deux voisins
    #: en cas d'égalité car PostgreSQL arrondit au pair), jamais une fenêtre
    #: de proximité. Un tour étranger à un autre instant n'en fait pas partie,
    #: même à 0,2 s ; une clé ISO (repli exact) est lue comme son époque. La
    #: ligne ou la réservation doit aussi suivre le marqueur : au même
    #: arrondi, celle d'un tour antérieur reste étrangère.
    _HISTORICAL_ROUNDINGS = 6
    _HISTORICAL_FLOOR = -2

    def _get_sql(self) -> str:
        """`get` en UNE instruction : marqueur et clé durable du MÊME état.

        La clé se retrouve par l'identité du tour, jamais par ordre temporel :
        `turn_id` exact ; sinon `started_at` (instant de pose du marqueur) ;
        sinon, pour une ligne d'avant 0423, l'époque portée par sa clé, qui
        doit être un arrondi historique exact de l'instant du marqueur. Une
        ligne ou une réservation antérieure au marqueur est étrangère, même
        quand son arrondi tombe dans le même intervalle. L'index de début seul
        ne prouve rien : il est réutilisé après une rotation du flux, donc une
        réservation (ou une ligne) au même index mais à un autre instant est
        étrangère. Elle n'est jamais retenue ; une réservation étrangère sans
        candidat, une réservation étrangère encore ouverte, ou une ligne
        étrangère au repli rendent la clé indémontrable (`ledger_ambiguous`), et
        l'appelant suspend au lieu de deviner. Un ancien worker peut écrire sa
        ligne APRÈS le marqueur de son remplaçant (#1469) : la première ligne
        après `created_at` n'est pas forcément celle du marqueur. Le repli lit
        les lignes de l'agent par son index (#2920) ; il est transitoire, le
        temps d'effacer les marqueurs d'avant 0471.

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
           (m.agent || ':' || m.start_index || ':') AS prefix,
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
                   FROM generate_series({self._HISTORICAL_FLOOR},
                                        {self._HISTORICAL_ROUNDINGS}) AS g(d)
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
           (left(tc.spend_key, length(m.prefix)) = m.prefix) AS meme_index,
           -- une ligne du tour ne peut pas précéder son marqueur : celle d'un
           -- index réutilisé, même au même arrondi, est étrangère.
           (tc.recorded_at >= m.created_at::timestamptz) AS posterieure,
           substr(tc.spend_key, length(m.prefix) + 1) AS suffixe
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
           (left(ar.spend_key, length(m.prefix)) = m.prefix) AS meme_index,
           -- une réservation du tour est prise APRÈS son marqueur : celle
           -- d'un tour antérieur, même au même arrondi, est étrangère.
           (ar.created_at >= m.created_at::timestamptz) AS posterieure,
           substr(ar.spend_key, length(m.prefix) + 1) AS suffixe,
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
