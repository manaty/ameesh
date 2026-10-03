# SPDX-License-Identifier: AGPL-3.0-only
"""Du canon vers le registre (spec §4.4) : `canon sync` et agents éphémères.

`sync` met à jour, pour les agents placés sur un hôte, les colonnes
**déclaratives** du registre (harnais, hôte, dossier, modèle, budget,
responsable, équipe, fournisseur, mode d'identifiants, capacités, canon_ref).
Il ne touche jamais aux colonnes d'**état** (bail, session, statut, dépense,
consigne), avec deux exceptions qui sont les siennes :

* un agent **retiré du canon** (il en venait : `canon_ref` posé, non éphémère)
  passe `stopped` ; s'il est en plein tour (statut `running`, bail vivant),
  sync ne tue rien : il marque seulement l'arrêt demandé dans `status_text`,
  et c'est la synchronisation suivante, une fois le tour fini, qui l'arrête ;
* un agent arrêté par sync puis **réintégré** au canon repart `idle`.

Responsabilité (R14) : la colonne `responsible` ne reçoit que le responsable
**résolu** (fiche Member humaine unique) d'un agent sans erreur bloquante.
Sinon elle est vidée : l'agent n'est plus réclamable quand le responsable est
requis (`registry.claimable`). Une erreur sans sujet (fédération, fiche du
profil illisible…) bloque tous les agents de l'hôte : un canon invalide
empêche les nouvelles réclamations sans arrêter les agents déjà réclamés.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import canon as canon_mod
from .canon import Canon, Finding
from .config import NAME_RE
from .db import Db

#: préfixes de `status_text` posés par sync (et reconnus par lui seul)
STOP_MARK = "retiré du canon"
PENDING_MARK = "arrêt demandé en fin de tour : retiré du canon"
REVIVED_TEXT = "réintégré au canon"

MAX_EPHEMERAL_TTL = 7 * 24 * 3600.0
_TTL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)

DECLARATIVE = ("harness", "host", "cwd", "model", "budget_usd", "responsible", "team",
               "provider", "credential_mode", "capabilities", "canon_ref", "ephemeral")


class SpawnError(ValueError):
    pass


@dataclass
class SyncAction:
    agent: str
    action: str     # créé | mis à jour | inchangé | arrêté | arrêt demandé | réintégré | déplacé | laissé
    detail: str = ""
    blocked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"agent": self.agent, "action": self.action, "detail": self.detail}
        if self.blocked:
            out["blocked"] = self.blocked
        return out


@dataclass
class SyncReport:
    host: str
    source: str
    untrusted: bool
    actions: list[SyncAction]
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return canon_mod.errors(self.findings)

    def to_dict(self) -> dict:
        return {"host": self.host, "source": self.source, "untrusted": self.untrusted,
                "actions": [a.to_dict() for a in self.actions],
                "errors": len(self.errors),
                "findings": [f.to_dict() for f in self.findings]}


def _member_of(canon_ref: str | None) -> str:
    return (canon_ref or "").split(":", 1)[0]


def _caps_param(capabilities) -> str | None:
    return None if capabilities is None else json.dumps(list(capabilities))


_CAPS_SQL = ("CASE WHEN %s::text IS NULL THEN NULL "
             "ELSE ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)) END")


def _write_declared(db: Db, name: str, values: dict) -> None:
    caps = _caps_param(values["capabilities"])
    db.query(
        """
        INSERT INTO agent_registry
            (name, harness, host, cwd, model, budget_usd, responsible, team, provider,
             credential_mode, capabilities, canon_ref, ephemeral, ephemeral_expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, __CAPS__, %s, false, NULL)
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
            updated_at      = now()
        RETURNING name
        """.replace("__CAPS__", _CAPS_SQL),
        (name, values["harness"], values["host"], values["cwd"], values["model"],
         values["budget_usd"], values["responsible"], values["team"], values["provider"],
         values["credential_mode"], caps, caps, values["canon_ref"]),
    )


def _clear_responsible(db: Db, name: str, host: str) -> None:
    db.execute(
        "UPDATE agent_registry SET responsible = NULL, updated_at = now() "
        "WHERE name = %s AND host = %s AND NOT ephemeral AND responsible IS NOT NULL",
        (name, host))


def _rows(db: Db, host: str, names: list[str]) -> dict[str, dict]:
    sql = """
        SELECT name, harness, host, cwd, model, budget_usd, responsible, team, provider,
               credential_mode, capabilities, canon_ref, ephemeral, status, status_text,
               (status = 'running' AND lease_expires_at > now()) AS in_turn
          FROM agent_registry
         WHERE host = %s
    """
    params: list = [host]
    if names:
        sql += " OR name = ANY(ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)))"
        params.append(json.dumps(names))
    return {row["name"]: row for row in db.query(sql, tuple(params))}


def _same(row: dict, values: dict) -> bool:
    for key in DECLARATIVE:
        old, new = row.get(key), values.get(key)
        if key == "budget_usd":
            old = None if old is None else float(old)
            new = None if new is None else float(new)
        if key == "capabilities":
            old = None if old is None else list(old)
        if old != new:
            return False
    return True


def sync(db: Db, canon: Canon, host: str, findings: list[Finding] | None = None) -> SyncReport:
    """Aligne le registre de `host` sur le canon. Refuse un canon illisible."""
    if not canon.readable:
        reasons = "; ".join("%s : %s" % (f.code, f.message)
                            for f in canon_mod.errors(canon.load_findings))
        raise canon_mod.CanonError("canon illisible, registre inchangé : %s" % (
            reasons or "aucune source"))
    if findings is None:
        findings = canon_mod.validate(canon)
    block = canon_mod.blocking(findings)
    actions: list[SyncAction] = []

    placed_here: dict[str, canon_mod.Placement] = {}
    elsewhere: dict[str, list[canon_mod.Placement]] = {}
    for agent in canon.agents:
        placements = [p for p in canon.placements_of(agent.title)]
        here = [p for p in placements if p.host == host]
        if here:
            placed_here.setdefault(agent.title, here[0])
        elif placements:
            elsewhere[agent.title] = placements

    rows = _rows(db, host, sorted(placed_here))

    for name in sorted(placed_here):
        placement = placed_here[name]
        agent = canon.agent(name)
        if agent is None or not NAME_RE.match(name):
            continue
        reasons = block.reasons(name, [host])
        responsible = canon.resolve_human(agent.responsible) if not reasons else None
        values = {
            "harness": agent.harness or "other",
            "host": host,
            "cwd": os.path.expanduser(placement.cwd) if placement.cwd else None,
            "model": agent.model,
            "budget_usd": agent.budget_usd_per_day,
            "responsible": responsible,
            "team": agent.team,
            "provider": agent.provider,
            "credential_mode": placement.credential_mode or agent.credential_mode,
            # `approve` n'entre jamais au registre (R8) ; la fiche est de toute
            # façon bloquée par l'erreur agent-approve-capability
            "capabilities": None if agent.capabilities is None else [
                c for c in agent.capabilities
                if c.strip().lower() not in canon_mod.FORBIDDEN_CAPABILITIES],
            "canon_ref": agent.fiche.ref,
            "ephemeral": False,
        }
        row = rows.get(name)
        if row is None:
            _write_declared(db, name, values)
            action = SyncAction(name, "créé")
        elif _same(row, values):
            action = SyncAction(name, "inchangé")
        else:
            _write_declared(db, name, values)
            action = SyncAction(name, "mis à jour")
        if reasons:
            action.blocked = reasons
            action.detail = "non réclamable : erreur bloquante du canon"
        actions.append(action)
        if row is not None and row.get("status") == "stopped" \
                and (row.get("status_text") or "").startswith(STOP_MARK):
            revived = db.query(
                """
                UPDATE agent_registry
                   SET status = 'idle', status_text = %s, updated_at = now()
                 WHERE name = %s AND status = 'stopped' AND starts_with(status_text, %s)
                RETURNING name
                """,
                (REVIVED_TEXT, name, STOP_MARK),
            )
            if revived:
                actions.append(SyncAction(name, "réintégré", "arrêté par sync, de retour au canon"))
        elif row is not None and (row.get("status_text") or "").startswith(PENDING_MARK):
            db.execute(
                "UPDATE agent_registry SET status_text = '' "
                "WHERE name = %s AND starts_with(status_text, %s)",
                (name, PENDING_MARK))

    loaded = canon.loaded_members()
    for name, row in sorted(rows.items()):
        if name in placed_here or row.get("host") != host:
            continue
        if row.get("ephemeral") or not row.get("canon_ref"):
            continue  # éphémère, ou jamais venu du canon : sync ne le gouverne pas
        member = _member_of(row.get("canon_ref"))
        unverifiable = ("membre %s non lu" % member if member not in loaded
                        else "canon invalide" if block.global_errors else "")
        if unverifiable:
            # Fiche invérifiable : ni arrêté (le retrait n'est pas constaté), ni
            # réclamable (son responsable n'est plus confirmé par le canon).
            _clear_responsible(db, name, host)
            actions.append(SyncAction(
                name, "laissé", "%s : retrait différé, non réclamable" % unverifiable,
                block.reasons(name) if block.global_errors else []))
            continue
        moved = elsewhere.get(name)
        if moved and len(moved) == 1 and canon.agent(name) is not None:
            target = moved[0].host or ""
            db.execute(
                """
                UPDATE agent_registry
                   SET host = %s, responsible = NULL, canon_ref = %s, updated_at = now()
                 WHERE name = %s AND host = %s
                """,
                (target, canon.agent(name).fiche.ref, name, host))
            actions.append(SyncAction(
                name, "déplacé",
                "placé sur %s : non réclamable avant la synchronisation de cet hôte" % target))
            continue
        why = "plus de placement" if canon.agent(name) is not None else "fiche absente"
        stopped = db.query(
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
            ("%s (%s)" % (PENDING_MARK, why), "%s (%s)" % (STOP_MARK, why), name, host),
        )
        if not stopped:
            continue
        if stopped[0]["status"] == "stopped":
            actions.append(SyncAction(name, "arrêté", why))
        else:
            actions.append(SyncAction(name, "arrêt demandé",
                                      "%s : tour en cours, rien n'est tué" % why))
    for name, row in sorted(rows.items()):
        if row.get("host") == host and not row.get("canon_ref") and not row.get("ephemeral") \
                and name not in placed_here:
            actions.append(SyncAction(name, "hors canon",
                                      "inscrit à la main : non gouverné par sync"))
    return SyncReport(host=host, source=canon.source_label(), untrusted=canon.untrusted,
                      actions=actions, findings=findings)


# --------------------------------------------------------------------------
# agents éphémères (R14)
# --------------------------------------------------------------------------

def parse_ttl(text: str | None) -> float:
    """« 30m », « 2h », « 1d », « 3600 » → secondes, borné à 7 jours. Obligatoire."""
    if not text:
        raise SpawnError("échéance obligatoire : --ttl 30m, 2h, 1d…")
    match = _TTL_RE.match(text)
    if not match:
        raise SpawnError("échéance illisible : %r (attendu 30m, 2h, 1d ou des secondes)" % text)
    seconds = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[
        match.group(2).lower()]
    if seconds <= 0:
        raise SpawnError("échéance nulle")
    if seconds > MAX_EPHEMERAL_TTL:
        raise SpawnError("échéance trop lointaine pour un éphémère (maximum %d jours)"
                         % (MAX_EPHEMERAL_TTL // 86400))
    return seconds


def spawn(db: Db, name: str, creator: str, ttl_seconds: float, *,
          cwd: str | None = None, canon: Canon | None = None) -> dict:
    """Crée un agent éphémère : responsable copié du créateur, capacités ⊆ {read, propose}.

    L'insertion est atomique (INSERT … SELECT sur la ligne du créateur) : le
    responsable copié est celui du créateur au moment de la création. Un
    créateur éphémère transmet aussi son échéance (l'enfant ne lui survit pas).
    """
    if not NAME_RE.match(name or ""):
        raise SpawnError("nom d'agent invalide : %r" % (name,))
    if not NAME_RE.match(creator or ""):
        raise SpawnError("créateur invalide : %r" % (creator,))
    if name == creator:
        raise SpawnError("un agent ne se crée pas lui-même")
    if canon is not None and canon.readable and canon.agent(name) is not None:
        raise SpawnError("%s a une fiche dans le canon : ce n'est pas un éphémère" % name)
    rows = db.query(
        """
        SELECT name, responsible, ephemeral, status, capabilities,
               (NOT ephemeral OR ephemeral_expires_at > now()) AS alive
          FROM agent_registry WHERE name = %s
        """,
        (creator,),
    )
    if not rows:
        raise SpawnError("créateur inconnu du registre : %s" % creator)
    parent = rows[0]
    if not (parent.get("responsible") or "").strip():
        raise SpawnError("le créateur %s n'a pas d'humain responsable résolu (R14) : "
                         "il ne peut pas transmettre de responsabilité" % creator)
    if not parent.get("alive"):
        raise SpawnError("le créateur %s est un éphémère échu" % creator)
    if parent.get("status") == "stopped":
        raise SpawnError("le créateur %s est arrêté" % creator)
    created = db.query(
        """
        INSERT INTO agent_registry
            (name, chantier, harness, host, cwd, model, responsible, team, provider,
             credential_mode, capabilities, ephemeral, ephemeral_expires_at, created_by)
        SELECT %s, c.chantier, c.harness, c.host, coalesce(%s, c.cwd), c.model,
               c.responsible, c.team, c.provider, c.credential_mode,
               ARRAY(SELECT x FROM unnest(ARRAY['read', 'propose']::text[]) AS x
                      WHERE c.capabilities IS NULL OR x = ANY(c.capabilities)),
               true,
               CASE WHEN c.ephemeral
                    THEN least(now() + make_interval(secs => %s), c.ephemeral_expires_at)
                    ELSE now() + make_interval(secs => %s) END,
               c.name
          FROM agent_registry c
         WHERE c.name = %s
           AND coalesce(c.responsible, '') <> ''
           AND c.status <> 'stopped'
           AND (NOT c.ephemeral OR c.ephemeral_expires_at > now())
        ON CONFLICT (name) DO NOTHING
        RETURNING name, responsible, host, harness, capabilities, created_by,
                  extract(epoch from ephemeral_expires_at)::float8 AS ephemeral_expires_ts
        """,
        (name, os.path.abspath(os.path.expanduser(cwd)) if cwd else None,
         ttl_seconds, ttl_seconds, creator),
    )
    if not created:
        if db.query("SELECT 1 AS x FROM agent_registry WHERE name = %s", (name,)):
            raise SpawnError("un agent %s existe déjà" % name)
        raise SpawnError("le créateur %s a changé pendant la création : réessayez" % creator)
    return created[0]
