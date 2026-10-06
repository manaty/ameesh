# SPDX-License-Identifier: AGPL-3.0-only
"""Placement gouverné (C4, R18 ; décision 0014).

Le responsable du projet décide où tourne un agent (fiche `Placement`), dans
la politique posée par le responsable de l'hôte (fiche `Host`). ameesh :

* évalue l'admissibilité du placement d'un agent sur un hôte (`evaluate`,
  fail closed) ; `ameesh canon sync` l'écrit dans les colonnes déclaratives
  `placement_ok`, `placement_diagnostic` et `placement_ref` (0022), avec le
  PROFIL ÉVALUÉ (`placement_profile` : `ameesh_placement_profile` de l'hôte,
  du harnais, du fournisseur, du modèle et du mode d'identifiants jugés) ;
  la condition de réclamation (`registry.canon_claim_predicate_sql`) exige
  `placement_ok` pour un agent gouverné par le canon, et que son profil
  courant soit encore le profil évalué (sinon : « profil divergé depuis
  l'évaluation », jusqu'au prochain `canon sync`) ;
* propose les placements admissibles d'un agent (`proposals`) : les hôtes du
  canon dont la politique l'admet, avec les modes d'identifiants admis ;
* ne déplace jamais un agent de lui-même : rien ici n'écrit, ni dans le canon
  ni au registre (`ameesh placement check` est en lecture seule). Changer un
  placement, c'est une PR sur le canon, puis `ameesh canon sync`.

Le SQL est dans le stockage (`storage.of(db).placements`, spec §10).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import canon as canon_mod
from . import storage
from .canon import Canon, Finding
from .db import Db
from .storage.postgres import placement as _pg_placement

#: modes d'identifiants du profil (spec §4.2)
CREDENTIAL_MODES = ("api-key", "subscription")

#: profil d'un placement (0022) : colonnes du registre qui le forment, dans
#: l'ordre des arguments de la fonction SQL `ameesh_placement_profile`, et
#: expressions SQL du profil courant (`profile_sql`) ou passé en paramètres
#: (`PROFILE_PARAMS_SQL`, `profile_params`) — pilote Postgres, alias de
#: compatibilité (voir `storage.postgres.placement`)
PROFILE_COLUMNS = _pg_placement.PROFILE_COLUMNS
profile_sql = _pg_placement.profile_sql
PROFILE_PARAMS_SQL = _pg_placement.PROFILE_PARAMS_SQL
profile_params = _pg_placement.profile_params


def diverged_diagnostic(evaluated: str | None, current: str | None) -> str:
    """Raison lisible : le profil de l'agent n'est plus celui que le verdict a jugé."""
    return ("profil divergé depuis l'évaluation du placement (évalué : %s ; actuel : %s) : "
            "non réclamable jusqu'au prochain « ameesh canon sync »" % (
                evaluated or "aucun", current or "?"))


def admitted_hosts(canon: Canon, admission) -> list[str]:
    """Hôtes du canon admis par une admission (L31, 0029).

    Ce sont les hôtes NOMMÉS (`hosts`, et l'ancien `host`) plus ceux dont une
    étiquette figure dans `host_tags` ; sans doublon, triés par nom.
    """
    if admission is None:
        return []
    titles = set(admission.host_names())
    tags = set(admission.tags())
    if tags:
        for host in canon.hosts:
            if tags & set(host.tags or []):
                titles.add(host.title)
    return sorted(titles)


def admission_of(canon: Canon, agent: str):
    """L'admission unique d'une persona, ou None (aucune, ou plusieurs)."""
    found = canon.placements_of(agent)
    return found[0] if len(found) == 1 else None



@dataclass
class Verdict:
    """Le placement d'un agent sur un hôte est-il admis ?

    `ok` est None quand rien n'a pu être évalué sur cet hôte (agent déplacé
    vers un hôte pas encore synchronisé) : comme faux, cela ferme la
    réclamation.
    """

    agent: str
    host: str
    ok: bool | None
    diagnostic: str = ""
    ref: str | None = None              # canon_ref de la fiche Placement
    credential_mode: str | None = None  # celui du placement, sinon de la fiche
    #: profil évalué (texte de `ameesh_placement_profile`) quand il est connu
    #: au moment de l'écrire : écrit dans `placement_profile` avec le verdict
    profile: str | None = None

    def to_dict(self) -> dict:
        return {"agent": self.agent, "host": self.host, "placement_ok": self.ok,
                "placement_diagnostic": self.diagnostic, "placement_ref": self.ref,
                "credential_mode": self.credential_mode}

    def describe(self) -> str:
        if self.ok:
            return "admis (mode %s)" % (self.credential_mode or "non déclaré")
        if self.ok is None:
            return "non évalué — %s" % (self.diagnostic or "?")
        return "REFUSÉ — %s" % (self.diagnostic or "?")


def evaluate(canon: Canon, agent: str, host: str) -> Verdict:
    """Admissibilité de `agent` sur `host` (C4, L31), fail closed.

    Refusé si la fiche Agent est absente ou en double, si l'agent n'a pas
    d'admission ou en a plusieurs (ambiguë), si `host` n'est pas admis par
    l'admission (hôtes nommés ou étiquettes), si l'hôte n'a pas de fiche Host
    (politique inconnue) ou en a plusieurs, ou si la politique de l'hôte
    refuse le harnais, le fournisseur, le modèle ou le mode d'identifiants
    (`canon.placement_violations`).
    """
    fiches = [a for a in canon.agents if a.title == agent]
    placements = canon.placements_of(agent)
    admission = placements[0] if len(placements) == 1 else None
    fiche = fiches[0] if fiches else None
    verdict = Verdict(
        agent, host, False, ref=admission.fiche.ref if admission else None,
        credential_mode=((admission.credential_mode if admission else None)
                         or (fiche.credential_mode if fiche else None)))
    if fiche is None:
        verdict.diagnostic = "fiche Agent %s introuvable dans le canon" % agent
        return verdict
    if len(fiches) > 1:
        verdict.diagnostic = "fiche Agent %s déclarée %d fois : placement ambigu" % (
            agent, len(fiches))
        return verdict
    if len(placements) > 1:
        verdict.diagnostic = "admission ambiguë : %s admis %d fois" % (agent, len(placements))
        return verdict
    if admission is None:
        verdict.diagnostic = "aucune admission de %s" % agent
        return verdict
    hosts = [h for h in canon.hosts if h.title == host]
    if not hosts:
        verdict.diagnostic = "hôte %s sans fiche Host : politique inconnue" % host
        return verdict
    if len(hosts) > 1:
        verdict.diagnostic = "hôte %s déclaré %d fois : politique ambiguë" % (host, len(hosts))
        return verdict
    admis = admitted_hosts(canon, admission)
    if host not in admis:
        verdict.diagnostic = "hôte %s non admis pour %s (admis : %s)" % (
            host, agent, ", ".join(admis) or "aucun")
        return verdict
    reasons = canon_mod.placement_violations(fiche, hosts[0], admission)
    verdict.ok = not reasons
    verdict.diagnostic = " ; ".join(reasons)
    return verdict


def verdicts(canon: Canon, host: str | None = None) -> list[Verdict]:
    """Un verdict par couple (agent, hôte admis) du canon, ou de `host`."""
    seen: dict[tuple[str, str], Verdict] = {}
    for admission in canon.placements:
        if not admission.agent:
            continue
        for name in admitted_hosts(canon, admission):
            if host is not None and name != host:
                continue
            key = (admission.agent, name)
            if key not in seen:
                seen[key] = evaluate(canon, *key)
    return [seen[key] for key in sorted(seen)]


def proposals(canon: Canon, agent: str,
              findings: list[Finding] | None = None) -> list[dict]:
    """Chaque hôte du canon : `agent` y serait-il admis, et avec quel mode ?

    Admissible : l'admission de l'agent admet cet hôte (hôtes nommés ou
    étiquettes), la politique de l'hôte admet le harnais, le fournisseur et le
    modèle de l'agent, au moins un mode d'identifiants, il reste de la place
    (`max_agents`, sans compter l'agent lui-même), et la fiche Host n'a pas
    d'erreur bloquante. `admitted` dit si l'admission couvre l'hôte ;
    `credential_modes` : les modes admis (None = tous). Rien n'est écrit :
    c'est une proposition au responsable du projet.
    """
    fiche = canon.agent(agent)
    if fiche is None:
        return []
    if findings is None:
        findings = canon_mod.validate(canon)
    block = canon_mod.blocking(findings)
    admission = admission_of(canon, agent)
    admis = set(admitted_hosts(canon, admission))
    out = []
    for title in sorted({h.title for h in canon.hosts}):
        hosts = [h for h in canon.hosts if h.title == title]
        reasons: list[str] = []
        modes: list[str] | None = None
        if admission is None:
            reasons.append("aucune admission de %s, ou plusieurs (ambiguë)" % agent)
        elif title not in admis:
            reasons.append("hôte %s non admis pour %s (admis : %s)" % (
                title, agent, ", ".join(sorted(admis)) or "aucun"))
        if len(hosts) > 1:
            reasons.append("hôte %s déclaré %d fois : politique ambiguë" % (title, len(hosts)))
        else:
            policy = hosts[0].policy
            # harnais, fournisseur, modèle : ce que l'admission ne peut pas changer
            reasons += canon_mod.placement_violations(fiche, {"title": title, "policy": {
                "harnesses": policy.harnesses, "providers": policy.providers,
                "models": policy.models}})
            modes = None if policy.credential_modes is None else list(policy.credential_modes)
            if modes == []:
                reasons.append("aucun mode d'identifiants admis par l'hôte %s" % title)
            others = [p for p in canon.placements if p.agent != agent
                      and title in admitted_hosts(canon, p)]
            if policy.max_agents is not None and len(others) >= policy.max_agents:
                reasons.append("hôte %s complet : %d placement(s) pour max_agents = %d"
                               % (title, len(others), policy.max_agents))
        for finding in block.by_host.get(title, []):
            reasons.append("fiche Host en erreur : %s (%s)" % (finding.code, finding.where()))
        if modes is None:
            mode = fiche.credential_mode
        elif fiche.credential_mode in modes:
            mode = fiche.credential_mode
        else:
            mode = modes[0] if modes else None
        current = title in admis
        out.append({
            "host": title, "admissible": not reasons, "admitted": title in admis,
            "credential_modes": modes,
            "credential_mode": mode, "current": current,
            "current_credential_mode": (admission.credential_mode or fiche.credential_mode)
            if current and admission is not None else None,
            "reasons": reasons,
        })
    return out


def report(canon: Canon, agent: str | None = None,
           findings: list[Finding] | None = None) -> list[dict]:
    """`ameesh placement check` : admissions actuelles et hôtes admissibles, par agent."""
    if findings is None:
        findings = canon_mod.validate(canon)
    names = [agent] if agent else sorted({a.title for a in canon.agents})
    out = []
    for name in names:
        fiche = canon.agent(name)
        admission = admission_of(canon, name)
        hosts = admitted_hosts(canon, admission)
        choices = proposals(canon, name, findings)
        out.append({
            "agent": name, "known": fiche is not None,
            "harness": fiche.harness if fiche else None,
            "provider": fiche.provider if fiche else None,
            "model": fiche.model if fiche else None,
            "credential_mode": fiche.credential_mode if fiche else None,
            "admitted": hosts,
            "placements": [evaluate(canon, name, h).to_dict() for h in hosts],
            "admissible": [c for c in choices if c["admissible"]],
            "refused": [c for c in choices if not c["admissible"]],
        })
    return out


#: clés ajoutées par `annotate` / rendues par `recorded`
RECORDED_KEYS = ("placement_ok", "placement_diagnostic", "placement_ref", "placement_profile",
                 "placement_profile_current", "placement_profile_ok")


def recorded(db: Db, host: str | None = None) -> dict[str, dict]:
    """Verdicts écrits au registre (0022), confrontés au profil COURANT de
    chaque agent, par nom (ceux de `host` seulement si donné).

    `placement_profile_ok` : None sans verdict (agent inscrit à la main, pas
    encore évalué) ; faux si le profil a divergé depuis l'évaluation — la
    condition de réclamation le refuse alors, et `placement_diagnostic` le
    dit (« profil divergé depuis l'évaluation », profils évalué et actuel).
    """
    out: dict[str, dict] = {}
    for row in storage.of(db).placements.recorded(host):
        entry = dict(row)
        if row.get("placement_ok") is None and row.get("placement_profile") is None:
            entry["placement_profile_ok"] = None
        else:
            entry["placement_profile_ok"] = (
                row.get("placement_profile") == row.get("placement_profile_current"))
        if entry["placement_profile_ok"] is False:
            why = diverged_diagnostic(row.get("placement_profile"),
                                      row.get("placement_profile_current"))
            if row.get("placement_ok") is not True and row.get("placement_diagnostic"):
                why += " ; verdict à l'évaluation : %s" % row["placement_diagnostic"]
            entry["placement_diagnostic"] = why
        out[row["name"]] = entry
    return out


def annotate(db: Db, rows: list[dict]) -> list[dict]:
    """Ajoute le verdict de placement (0022) aux lignes de `registry.overview`
    (`mesh list --json`) : `placement_ok`, `placement_diagnostic`,
    `placement_ref`, `placement_profile` (évalué), `placement_profile_current`
    et `placement_profile_ok` (voir `recorded`)."""
    columns = recorded(db)
    for row in rows:
        found = columns.get(row.get("name"), {})
        for key in RECORDED_KEYS:
            row[key] = found.get(key)
    return rows
