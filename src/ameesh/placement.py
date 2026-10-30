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

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

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
    return ("profile diverged since the placement was evaluated (evaluated: %s; current: %s): "
            "not claimable until the next \"ameesh canon sync\"" % (
                evaluated or "none", current or "?"))


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


def covered_by_host(canon: Canon, *, without: str | None = None) -> dict[str, list]:
    """Hôte -> admissions qui le couvrent (#3101), hors celles de `without`.

    L'exclusion suit l'identité résolue (titre ou alias) ; une admission dont
    l'agent est inconnu du canon reste comptée.
    """
    target = canon.agent(without) if without else None
    covered: dict[str, list] = defaultdict(list)
    for admission in canon.placements:
        if not admission.agent or admission.agent == without:
            continue
        if target is not None and canon.agent(admission.agent) is target:
            continue
        for name in admitted_hosts(canon, admission):
            covered[name].append(admission)
    return covered


def host_load(canon: Canon) -> tuple[dict[str, int], dict[str, str | None]]:
    """Admissions épinglées par hôte et siège de chaque rôle élu (#3101)."""
    return canon_mod.admission_load(canon, covered_by_host(canon))


def capacity_baseline(canon: Canon, agent: str) -> tuple[dict[str, list], set[str]]:
    """Admissions sans `agent`, et rôles élus assis AVANT de le placer.

    #3101 (B4) : la référence est le canon tel qu'il est. Un rôle élu y
    concourt déjà pour sa place : la référence est le canon complet. Un agent
    épinglé, lui, est déplacé par la proposition : la référence est le canon
    sans lui. L'agent lui-même n'est pas dans les rôles assis rendus.
    """
    covered = covered_by_host(canon, without=agent)
    fiche = canon.agent(agent)
    elected = fiche is not None and fiche.releve == "election" and bool(fiche.candidats)
    seats = (host_load(canon) if elected else canon_mod.admission_load(canon, covered))[1]
    title = fiche.title if fiche is not None else agent
    return covered, {role for role, seat in seats.items() if seat is not None and role != title}


def capacity_reason(canon: Canon, agent: str, host: str,
                    baseline: tuple[dict[str, list], set[str]] | None = None) -> str | None:
    """Pourquoi `host` n'a pas de place pour `agent` sous `max_agents`, ou None.

    #3101 : les admissions épinglées des autres agents remplissent l'hôte, ou
    y placer l'agent ferait perdre sa place à un rôle élu assis dans le canon
    tel qu'il est (`capacity_baseline`, à passer quand l'appelant juge
    plusieurs hôtes). Seuls ces rôles assis sont rassis (B5) : un rôle déjà
    sans place ne prend la place de personne, et ne ferme le candidat de
    personne ; sa propre fiche seule est en erreur.
    """
    fiche = next((h for h in canon.hosts if h.title == host), None)
    limit = fiche.policy.max_agents if fiche is not None else None
    if limit is None:
        return None
    covered, seated = baseline if baseline is not None else capacity_baseline(canon, agent)
    pinned, with_agent = canon_mod.admission_load(canon, covered, extra=host, only=seated)
    if pinned.get(host, 0) > limit:
        return "host %s full: %d placement(s) for max_agents = %d" % (
            host, pinned[host] - 1, limit)
    lost = sorted(role for role, seat in with_agent.items() if seat is None)
    if lost:
        return "host %s full: elected role(s) %s would find no place for max_agents = %d" % (
            host, ", ".join(lost), limit)
    return None


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
            return "admitted (mode %s)" % (self.credential_mode or "not declared")
        if self.ok is None:
            return "not evaluated — %s" % (self.diagnostic or "?")
        return "REFUSED — %s" % (self.diagnostic or "?")


def evaluate(canon: Canon, agent: str, host: str) -> Verdict:
    """Admissibilité de `agent` sur `host` (C4, L31), fail closed.

    Refusé si la fiche Agent est absente ou en double, si l'agent n'a pas
    d'admission ou en a plusieurs (ambiguë), si `host` n'est pas admis par
    l'admission (hôtes nommés ou étiquettes), si l'hôte n'a pas de fiche Host
    (politique inconnue) ou en a plusieurs, ou si la politique de l'hôte
    refuse le harnais, le fournisseur, le modèle ou le mode d'identifiants
    (`canon.placement_violations`).
    """
    from .agent_names import resolve
    try:
        agent = resolve(agent, canon=canon)
    except ValueError as exc:
        return Verdict(agent, host, False, str(exc))
    fiches = [a for a in canon.agents if a.title == agent]
    placements = canon.placements_of(agent)
    admission = placements[0] if len(placements) == 1 else None
    fiche = fiches[0] if fiches else None
    verdict = Verdict(
        agent, host, False, ref=admission.fiche.ref if admission else None,
        credential_mode=((admission.credential_mode if admission else None)
                         or (fiche.credential_mode if fiche else None)))
    if fiche is None:
        verdict.diagnostic = "Agent card %s not found in the canon" % agent
        return verdict
    if len(fiches) > 1:
        verdict.diagnostic = "Agent card %s declared %d times: ambiguous placement" % (
            agent, len(fiches))
        return verdict
    if len(placements) > 1:
        verdict.diagnostic = "ambiguous admission: %s admitted %d times" % (agent, len(placements))
        return verdict
    if admission is None:
        verdict.diagnostic = "no admission for %s" % agent
        return verdict
    hosts = [h for h in canon.hosts if h.title == host]
    if not hosts:
        verdict.diagnostic = "host %s has no Host card: unknown policy" % host
        return verdict
    if len(hosts) > 1:
        verdict.diagnostic = "host %s declared %d times: ambiguous policy" % (host, len(hosts))
        return verdict
    admis = admitted_hosts(canon, admission)
    if host not in admis:
        verdict.diagnostic = "host %s not admitted for %s (admitted: %s)" % (
            host, agent, ", ".join(admis) or "none")
        return verdict
    reasons = canon_mod.placement_violations(fiche, hosts[0], admission)
    if not reasons and fiche.releve == "election" and fiche.candidats:
        # #3101 : un rôle élu ne prend qu'une place, sur un candidat qui en a
        # une ; un candidat sans place ne réclame pas le bail.
        full = capacity_reason(canon, agent, host)
        if full:
            reasons.append(full)
    verdict.ok = not reasons
    verdict.diagnostic = "; ".join(reasons)
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
    baseline = capacity_baseline(canon, agent)
    out = []
    for title in sorted({h.title for h in canon.hosts}):
        hosts = [h for h in canon.hosts if h.title == title]
        reasons: list[str] = []
        modes: list[str] | None = None
        if admission is None:
            reasons.append("no admission for %s, or several (ambiguous)" % agent)
        elif title not in admis:
            reasons.append("host %s not admitted for %s (admitted: %s)" % (
                title, agent, ", ".join(sorted(admis)) or "none"))
        if len(hosts) > 1:
            reasons.append("host %s declared %d times: ambiguous policy" % (title, len(hosts)))
        else:
            policy = hosts[0].policy
            # harnais, fournisseur, modèle : ce que l'admission ne peut pas changer
            reasons += canon_mod.placement_violations(fiche, {"title": title, "policy": {
                "harnesses": policy.harnesses, "providers": policy.providers,
                "models": policy.models}})
            modes = None if policy.credential_modes is None else list(policy.credential_modes)
            if modes == []:
                reasons.append("no credential mode admitted by host %s" % title)
            full = capacity_reason(canon, agent, title, baseline)
            if full:
                reasons.append(full)
        for finding in block.by_host.get(title, []):
            reasons.append("Host card in error: %s (%s)" % (finding.code, finding.where()))
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
                why += "; verdict at evaluation: %s" % row["placement_diagnostic"]
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


# ==========================================================================
# #2202 S2 : admission d'un tour en bac à sable (profil Agent + placement Host)
# ==========================================================================
#
# Le profil et le placement sont des cartes du canon, pas une seconde
# autorité : le bureau relit le canon PR et `admitted_ref` en est la preuve.
# Cette section ne lit que le canon chargé À LA RÉVISION AUDITÉE
# (`last_good_commit`) et rend l'instantané que `sandbox_turns` conserve :
# références et empreintes des deux cartes, limites, image, fournisseur et
# espace de noms. La fraîcheur de `canon_state` est vérifiée par
# `canon_fresh` ; une carte révoquée ou modifiée est vue par `sandbox_recheck`.

#: fenêtre de resynchronisation du canon par le coureur (§6) : un état plus
#: vieux refuse les NOUVEAUX tours, il ne change pas les limites d'un tour en
#: cours.
SANDBOX_CANON_MAX_AGE = 300.0

#: valeurs de l'instantané qui forment la BORNE du tour : si l'une change au
#: nouveau `last_good_commit`, le tour en cours doit être annulé (les
#: références changent à chaque commit, les empreintes seulement si la carte
#: change).
SANDBOX_GRANT_FIELDS = ("profile", "tranche", "limits", "image_digest", "provider",
                        "namespace", "credential", "session")


@dataclass
class SandboxAdmission:
    """Admission calculée d'un couple (agent, hôte) pour un tour en bac à sable.

    `ok` faux : `refusal` porte le code fermé (`profile-refused`) et
    `diagnostic` la raison lisible. `ok` vrai : l'instantané est complet et
    peut être écrit tel quel dans `sandbox_turns` (`limits` compris).
    """

    agent: str
    host: str
    ok: bool
    diagnostic: str = ""
    refusal: str = ""
    profile: str = ""
    profile_ref: str = ""
    profile_digest: str = ""
    placement_ref: str = ""
    placement_digest: str = ""
    canon_ref: str = ""
    admitted_ref: str = ""
    host_admitted_ref: str = ""
    tranche: str | None = None
    limits: dict = field(default_factory=dict)
    image_digest: str = ""
    #: référence épinglée complète, pour le Job du superviseur (le tableau
    #: `sandbox_turns` ne garde que `image_digest`)
    image: str = ""
    provider: str = ""
    namespace: str = ""
    #: défauts du Job de l'hôte, recopiés dans l'instantané (S4 les lit sans
    #: relire le canon)
    active_deadline_seconds: int = 0
    ttl_seconds_after_finished: int = 0
    tools: list[str] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    session: str = ""
    credential: str = ""

    def to_dict(self) -> dict:
        return {"agent": self.agent, "host": self.host, "ok": self.ok,
                "diagnostic": self.diagnostic, "refusal": self.refusal,
                "profile": self.profile, "profile_ref": self.profile_ref,
                "profile_digest": self.profile_digest,
                "placement_ref": self.placement_ref,
                "placement_digest": self.placement_digest,
                "canon_ref": self.canon_ref, "admitted_ref": self.admitted_ref,
                "host_admitted_ref": self.host_admitted_ref,
                "tranche": self.tranche, "limits": dict(self.limits),
                "image_digest": self.image_digest, "image": self.image,
                "provider": self.provider,
                "namespace": self.namespace, "tools": list(self.tools),
                "active_deadline_seconds": self.active_deadline_seconds,
                "ttl_seconds_after_finished": self.ttl_seconds_after_finished,
                "capabilities": dict(self.capabilities), "session": self.session,
                "credential": self.credential}


def sandbox_admission(canon: Canon, agent: str, host: str, *,
                      canon_ref: str | None = None,
                      reserve_usd: float | None = None) -> SandboxAdmission:
    """Admission d'un tour en bac à sable, fail closed ([] = aucune admission).

    `canon_ref` est la révision auditée (`canon_state.last_good_commit`) : le
    canon doit avoir été chargé à ce commit, jamais à la référence configurée
    qui peut être en avance. Sans `canon_ref`, le commit de la source racine
    est utilisé (banc, outil) ; une source non git est refusée. `reserve_usd`
    est le `reserve_usd` du profil de compte : `usd_per_turn` doit y tenir,
    sinon le compte et la tranche ne bornent pas le même montant (§6).
    """
    verdict = SandboxAdmission(agent=str(agent), host=str(host), ok=False)

    def refuse(message: str, reason: str = "profile-refused") -> SandboxAdmission:
        verdict.refusal = reason
        verdict.diagnostic = message
        return verdict

    if not canon.readable:
        return refuse("the canon is not readable: no sandbox admission")
    if canon.untrusted:
        return refuse("the canon is not an approved git source: no sandbox admission")
    commit = str(canon.sources[0].commit or "") if canon.sources else ""
    if not commit:
        return refuse("the canon has no committed revision: no sandbox admission")
    pinned = str(canon_ref if canon_ref is not None else commit).strip().lower()
    if not pinned:
        return refuse("no pinned canon revision (`last_good_commit`): no sandbox admission")
    if pinned != commit:
        return refuse("the canon is loaded at %s but the pinned revision is %s: read the "
                      "canon at `last_good_commit`" % (commit[:12], pinned[:12]))

    from .agent_names import resolve
    try:
        name = resolve(agent, canon=canon)
    except ValueError as exc:
        return refuse(str(exc))
    verdict.agent = name
    cards = [a for a in canon.agents if a.title == name]
    if len(cards) != 1:
        return refuse("Agent card %s is declared %d times: ambiguous sandbox profile"
                      % (name, len(cards)))
    card = cards[0]
    profile = card.sandbox
    if profile is None:
        return refuse("agent %s has no `sandbox` block" % name)
    verdict.profile = profile.profile
    verdict.admitted_ref = profile.admitted_ref
    verdict.tranche = profile.tranche
    verdict.tools = list(card.tools or [])
    verdict.capabilities = dict(profile.capabilities)
    verdict.session = profile.session
    verdict.credential = profile.credential
    if profile.status != "admitted":
        return refuse("sandbox profile %r of %s is %s, not admitted"
                      % (profile.profile, name, profile.status or "undeclared"))
    problems = canon_mod.sandbox_profile_problems(profile)
    if problems:
        return refuse("; ".join(problems))

    hosts = [h for h in canon.hosts if h.title == host]
    if len(hosts) != 1:
        return refuse("Host card %s is declared %d times: ambiguous sandbox placement"
                      % (host, len(hosts)))
    host_card = hosts[0]
    block = host_card.policy.sandbox
    if block is None:
        return refuse("host %s has no `policy.sandbox` placement" % host)
    verdict.host_admitted_ref = block.admitted_ref
    if profile.profile not in block.allowed_profiles:
        return refuse("sandbox profile %r is not in `allowed_profiles` of %s (%s)"
                      % (profile.profile, host,
                         ", ".join(block.allowed_profiles) or "none"))
    problems = canon_mod.sandbox_placement_problems(block,
                                                    max_agents=host_card.policy.max_agents)
    if problems:
        return refuse("; ".join(problems))
    if block.job.active_deadline_seconds < profile.limits.job_seconds:
        return refuse("`activeDeadlineSeconds` %d of %s is shorter than the profile's "
                      "`job_seconds` %d" % (block.job.active_deadline_seconds, host,
                                            profile.limits.job_seconds))

    # Le placement ordinaire (fiche Placement + politique de l'hôte) reste la
    # première porte : un profil sandbox ne contourne ni `max_agents` ni un
    # harnais/fournisseur/modèle refusé.
    ordinary = evaluate(canon, name, host)
    if not ordinary.ok:
        return refuse("placement refused: %s" % (ordinary.diagnostic or "no admission"))

    if reserve_usd is not None:
        try:
            reserve = float(reserve_usd)
        except (TypeError, ValueError):
            return refuse("the account `reserve_usd` is unreadable: %r" % (reserve_usd,))
        if not math.isfinite(reserve) or reserve <= 0:
            return refuse("the account `reserve_usd` is not a positive finite number: %r"
                          % (reserve_usd,))
        if profile.limits.usd_per_turn > reserve:
            return refuse("`usd_per_turn` %s exceeds the account `reserve_usd` %s"
                          % (profile.limits.usd_per_turn, reserve))

    try:
        verdict.profile_digest = canon_mod.card_digest(card.fiche)
        verdict.placement_digest = canon_mod.card_digest(host_card.fiche)
    except canon_mod.CanonError as exc:
        return refuse(str(exc))

    verdict.profile_ref = card.fiche.ref
    verdict.placement_ref = host_card.fiche.ref
    verdict.canon_ref = pinned
    verdict.limits = {
        "turns": profile.limits.turns,
        "max_attempts": profile.limits.max_attempts,
        "tool_calls": profile.limits.tool_calls,
        "turn_seconds": profile.limits.turn_seconds,
        "job_seconds": profile.limits.job_seconds,
        "context_tokens": profile.limits.context_tokens,
        "usd_per_turn": profile.limits.usd_per_turn,
        # défauts du Job de l'hôte (S4 les lit dans l'instantané)
        "active_deadline_seconds": block.job.active_deadline_seconds,
        "ttl_seconds_after_finished": block.job.ttl_seconds_after_finished,
        # ce que `ameesh_sandbox_complete` contrôle dans le résultat (§4)
        "tools": list(card.tools or []),
        "capabilities": dict(profile.capabilities),
        "session": profile.session,
    }
    verdict.image_digest = block.image_digest
    verdict.image = block.image
    verdict.provider = block.provider
    verdict.namespace = block.namespace
    verdict.active_deadline_seconds = block.job.active_deadline_seconds
    verdict.ttl_seconds_after_finished = block.job.ttl_seconds_after_finished
    verdict.ok = True
    verdict.diagnostic = "admitted (profile %s, placement %s)" % (profile.profile, host)
    return verdict


def canon_fresh(state: dict | None, *, now: float | None = None,
                max_age: float = SANDBOX_CANON_MAX_AGE) -> tuple[bool, str]:
    """Le `canon_state` d'un hôte autorise-t-il un NOUVEAU tour ? (§6)

    `status = 'ok'`, un `last_good_commit`, et un `checked_at` dans la fenêtre
    de resynchronisation. Un état absent, illisible, en erreur ou trop vieux
    ne refuse que les nouveaux tours : un tour en cours garde l'instantané de
    son admission.
    """
    if state is None:
        return False, "no canon state recorded for this host"
    status = str(state.get("status") or "")
    if status != "ok":
        return False, "canon state is %r" % (status or "unknown",)
    try:
        checked = float(state.get("checked_ts"))
    except (TypeError, ValueError):
        return False, "canon state has no readable `checked_at`"
    current = time.time() if now is None else float(now)
    age = current - checked
    if age > max_age:
        return False, ("canon checked %ds ago, outside the %ds freshness window"
                       % (int(age), int(max_age)))
    if not str(state.get("last_good_commit") or "").strip():
        return False, "canon state has no `last_good_commit`"
    return True, ""


@dataclass
class SandboxDrift:
    """Effet d'une nouvelle révision du canon sur un tour déjà admis.

    `same` : rien n'a changé ; `cosmetic` : l'empreinte a changé mais aucune
    borne du tour n'a bougé (la description, la preuve d'admission…) ;
    `changed` : une borne du tour a changé, l'instantané ne s'applique plus et
    le tour doit être annulé ; `revoked` : le profil/placement n'est plus admis
    du tout.
    """

    verdict: str
    diagnostic: str = ""
    admission: SandboxAdmission | None = None
    changed: list[str] = field(default_factory=list)


def sandbox_recheck(canon: Canon, admitted: SandboxAdmission, *,
                    canon_ref: str | None = None) -> SandboxDrift:
    """Relit les deux cartes au nouveau `last_good_commit` (§6).

    Appelé à la resynchronisation de 300 s : une révocation ou une borne
    modifiée demande l'annulation du tour ; un simple changement de commentaire
    ou de preuve ne l'annule pas. Le résultat n'écrit rien et ne change jamais
    l'instantané : la décision d'annuler est celle de l'appelant.
    """
    if not isinstance(admitted, SandboxAdmission) or not admitted.ok:
        return SandboxDrift("revoked", "the turn has no admitted snapshot")
    current = sandbox_admission(canon, admitted.agent, admitted.host, canon_ref=canon_ref)
    if not current.ok:
        return SandboxDrift("revoked",
                            "the grant no longer holds: %s"
                            % (current.diagnostic or "refused"), admission=current)
    if (current.profile_digest == admitted.profile_digest
            and current.placement_digest == admitted.placement_digest):
        return SandboxDrift("same", "", admission=current)
    changed = [name for name in SANDBOX_GRANT_FIELDS
               if getattr(current, name) != getattr(admitted, name)]
    if not changed:
        return SandboxDrift("cosmetic",
                            "the card digest changed without touching the grant",
                            admission=current)
    return SandboxDrift("changed", "the grant changed: %s" % ", ".join(changed),
                        admission=current, changed=changed)
