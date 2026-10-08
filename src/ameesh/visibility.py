# SPDX-License-Identifier: AGPL-3.0-only
"""Règle de visibilité d'une persona (lot L31, décision 0029).

**Une persona ne tourne sur la machine d'un humain que si cet humain a accès au
dépôt de mémoire de la persona.** Pour un serveur ou un hôte de cluster, la
règle porte sur son responsable (`responsible` de la fiche `Host`) et sur ses
administrateurs (`admins`).

La vérification est faite par l'API de la forge (`gh api --hostname <forge>`,
droits du dépôt) et, en repli pour un poste personnel (un seul humain), par une
copie d'essai (`git ls-remote`). Le résultat est mis en cache peu de temps
(`visibility_checks`, `AMEESH_VISIBILITY_TTL`) **pour un contexte donné** :
dépôt ET forge, humains requis, comptes de forge résolus, mode de vérification.
Tout changement de contexte invalide le cache et force une revérification, et
un contexte illisible est un refus. Le contrôle est **fail closed** : un doute
n'admet pas. Sans dépôt de mémoire déclaré, la règle est sans objet.

`ameesh canon check` reste hors ligne : seule la synchronisation du canon et
l'ouverture d'une session interrogent la forge.

Plusieurs canons (L43, décision 0031 points 5 et 6) : `canon` est toujours le
canon de la PERSONA. `H(hôte)` vient de la fiche Host de ce canon, et chaque
humain (`human:<id>`) se résout dans les fiches Member de ce canon — jamais
dans celles d'un autre canon de l'hôte. Un humain qui travaille dans deux
canons y a une fiche Member dans chacun.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field

from . import storage

#: niveaux de permission de la forge qui valent un accès en lecture
READ_PERMISSIONS = ("read", "pull", "triage", "push", "maintain", "admin", "write")

#: forges (hôtes) que `gh api` sait interroger sans configuration explicite ;
#: `AMEESH_FORGE_HOSTS` en ajoute (GitHub Enterprise), séparés par des virgules.
DEFAULT_FORGE_HOSTS = ("github.com",)

#: délai maximal accordé à une commande de forge (repli : la configuration)
DEFAULT_TIMEOUT = 10.0

_SSH_RE = re.compile(r"^(?:[a-zA-Z0-9._-]+@)?(?P<host>[A-Za-z0-9._-]+):"
                     r"(?P<path>[^\s:]+?)(?:\.git)?$")
_URL_RE = re.compile(r"^(?:ssh|https?|git)://(?:[^@/]+@)?(?P<host>[A-Za-z0-9._-]+)"
                     r"(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Repo:
    """Un dépôt de forge identifié : hôte, propriétaire, nom."""

    forge: str
    owner: str
    name: str
    url: str

    @property
    def slug(self) -> str:
        return "%s/%s" % (self.owner, self.name)

    @property
    def identity(self) -> str:
        """Forme canonique du dépôt, forge comprise (clé du cache)."""
        return "%s/%s" % (self.forge.lower(), self.slug)


def parse_repository(url: str | None) -> Repo | None:
    """Dépôt d'une URL de mémoire (`git@forge:owner/name.git`, `https://…`)."""
    text = (url or "").strip()
    if not text:
        return None
    # une URL avec schéma d'abord : `ssh://git@forge/o/n` ne doit pas être lue
    # comme la forme SCP `forge:o/n`.
    match = _URL_RE.match(text) or _SSH_RE.match(text)
    if not match:
        return None
    path = match.group("path").strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return Repo(forge=match.group("host"), owner=parts[-2], name=parts[-1], url=text)


@dataclass
class Result:
    ok: bool
    diagnostic: str = ""
    repository: str | None = None


def host_human_refs(canon, host: str) -> list[str] | None:
    """Références humaines BRUTES de `H(hôte)` (responsable + admins), ou None.

    None si l'hôte n'a pas de fiche : on ne peut alors rien résoudre."""
    fiche = canon.host(host)
    if fiche is None:
        return None
    return [ref for ref in [fiche.responsible] + list(fiche.admins or []) if ref]


def host_humans(canon, host: str) -> list[str] | None:
    """`H(hôte)` : responsable + administrateurs résolus, ou None (fail closed).

    None dès qu'un humain déclaré ne résout pas vers un `Member` unique : on ne
    peut pas prouver son accès, donc on n'admet pas."""
    refs = host_human_refs(canon, host)
    if refs is None:
        return None
    out: list[str] = []
    for ref in refs:
        resolved = canon.resolve_human(ref)
        if resolved is None:
            return None
        if resolved not in out:
            out.append(resolved)
    return out


def human_login(canon, human: str) -> str | None:
    """Compte de forge d'un humain (`forge` ou `github` de sa fiche Member)."""
    ident = (human or "").split(":", 1)[-1].strip()
    member = next((m for m in canon.members if m.title == ident), None)
    if member is None:
        return None
    data = member.fiche.data or {}
    for key in ("forge", "github", "login"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _default_run(argv, capture_output=True, text=True, timeout=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


@dataclass
class Forge:
    """Façade bornée sur `gh` et `git`, injectable pour les tests."""

    mode: str = "auto"          # auto | gh | git | none
    timeout: float = DEFAULT_TIMEOUT
    #: forges (hôtes) que l'API `gh` sait interroger explicitement
    hosts: frozenset = field(default_factory=lambda: frozenset(DEFAULT_FORGE_HOSTS))
    gh: object = _default_run
    git: object = _default_run

    @classmethod
    def from_config(cls, cfg) -> "Forge":
        declared = getattr(cfg, "forge_hosts", None) or DEFAULT_FORGE_HOSTS
        hosts = frozenset(str(h).strip().lower() for h in declared if str(h).strip())
        return cls(mode=(getattr(cfg, "forge", "") or "auto").strip().lower(),
                   timeout=max(1.0, float(getattr(cfg, "visibility_timeout",
                                                 DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)),
                   hosts=hosts or frozenset(DEFAULT_FORGE_HOSTS))

    def supports(self, repo: Repo) -> bool:
        """La forge déclarée est-elle interrogeable explicitement par `gh` ?"""
        return bool(repo.forge) and repo.forge.lower() in self.hosts

    def permission(self, repo: Repo, login: str) -> str | None:
        """Permission d'un compte sur le dépôt, sur la FORGE DÉCLARÉE.

        `--hostname` est explicite : la réponse ne dépend ni de `GH_HOST`, ni
        d'un dépôt homonyme du github.com par défaut. Une forge non prise en
        charge rend None (doute), jamais un repli silencieux."""
        if not self.supports(repo):
            return None
        argv = ["gh", "api", "--hostname", repo.forge,
                "repos/%s/collaborators/%s/permission" % (repo.slug, login),
                "--jq", ".permission"]
        try:
            proc = self.gh(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        return (proc.stdout or "").strip().lower() or None

    def reachable(self, repo: Repo) -> bool:
        """Copie d'essai : `git ls-remote` suffit à prouver la lecture."""
        try:
            proc = self.git(["git", "ls-remote", "--exit-code", repo.url],
                            capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        return getattr(proc, "returncode", 1) == 0

    def visible(self, repo: Repo, humans: list[str], logins: dict) -> tuple[bool, str]:
        """(visible, raison) pour l'ensemble `H(hôte)`.

        `logins` : `{human: login|None}`. En mode `git`, seule la copie d'essai
        est utilisée, et seulement pour un hôte à UN humain (elle ne prouve rien
        pour les administrateurs). En mode `gh`/`auto`, chaque humain lié à un
        compte est vérifié sur la forge déclarée ; un humain sans compte n'est
        prouvable que par la copie d'essai s'il est seul."""
        if self.mode in ("none", "off", "0"):
            return False, "vérification de visibilité désactivée (AMEESH_FORGE=%s)" % self.mode
        if self.mode == "git":
            if len(humans) == 1 and self.reachable(repo):
                return True, "copie d'essai réussie (responsable de l'hôte)"
            return False, ("copie d'essai impossible ou hôte à plusieurs humains "
                           "(elle ne prouve que le responsable)")
        if not self.supports(repo):
            if len(humans) == 1 and self.reachable(repo):
                return True, ("copie d'essai réussie (forge %s non interrogeable par gh)"
                              % repo.forge)
            return False, ("forge %s non prise en charge par l'API (AMEESH_FORGE_HOSTS) "
                           "et hôte à plusieurs humains" % repo.forge)
        inconnus = [h for h in humans if not logins.get(h)]
        if inconnus:
            if len(humans) == 1 and self.reachable(repo):
                return True, "copie d'essai réussie (poste à un seul humain)"
            return False, ("sans compte de forge pour %s, la copie d'essai ne prouve rien "
                           "pour les administrateurs" % ", ".join(inconnus))
        for human in humans:
            permission = self.permission(repo, logins[human])
            if permission is None:
                if len(humans) == 1 and self.reachable(repo):
                    return True, "copie d'essai réussie (API de la forge indisponible)"
                return False, "droits de %s illisibles sur %s" % (human, repo.identity)
            if permission not in READ_PERMISSIONS:
                return False, "%s n'a pas accès au dépôt %s (permission %s)" % (
                    human, repo.identity, permission)
        return True, "responsable et administrateurs de l'hôte ont accès au dépôt"


def decision_context(canon, host: str, repository: str | None, forge: Forge) -> dict:
    """Contexte de décision d'un contrôle : ce qui détermine le verdict.

    `fingerprint` est la clé du cache : dépôt résolu AVEC sa forge (et l'URL
    brute), références humaines brutes, humains résolus, comptes de forge
    résolus, mode et forges prises en charge. Deux contextes différents ne
    partagent jamais un verdict."""
    repo = parse_repository(repository)
    refs = host_human_refs(canon, host)
    humans = host_humans(canon, host)
    logins = {h: human_login(canon, h) for h in (humans or [])}
    fingerprint = "|".join([
        "repo=%s" % (repo.identity if repo is not None else "illisible"),
        "url=%s" % (repository or ""),
        "refs=%s" % ",".join(refs or []),
        "humans=%s" % (",".join(humans) if humans is not None else "illisible"),
        "logins=%s" % ",".join("%s=%s" % (h, logins.get(h) or "")
                               for h in sorted(humans or [])),
        "mode=%s" % forge.mode,
        "forges=%s" % ",".join(sorted(forge.hosts or [])),
    ])
    return {"fingerprint": fingerprint, "repo": repo, "humans": humans, "logins": logins,
            "refs": refs}


def check(canon, persona, host: str, *, db, forge: Forge | None = None,
          now: float | None = None, ttl: float | None = None) -> Result:
    """Verdict de visibilité de `persona` sur `host`, avec cache par contexte.

    Sans dépôt de mémoire déclaré : sans objet (visible). Le contexte est
    résolu AVANT le cache ; un changement de dépôt, de forge, de responsable,
    d'administrateur ou de compte, ou un contexte illisible, ne réutilise
    jamais un ancien OK (au pire, un refus est mémorisé pour le même contexte)."""
    repository = getattr(persona, "memory_repository", None)
    name = getattr(persona, "title", None) or str(persona)
    if not repository:
        return Result(True, "règle de visibilité sans objet (pas de dépôt de mémoire)")
    now = time.time() if now is None else float(now)
    f = forge or Forge.from_config(db.cfg)
    ctx = decision_context(canon, host, repository, f)
    cached = storage.of(db).visibility.cached(name, host, now, ctx["fingerprint"])
    if cached is not None:
        return Result(bool(cached["ok"]), cached["diagnostic"], cached["repository"])
    if ctx["repo"] is None:
        result = Result(False, "dépôt de mémoire illisible : %s" % repository, repository)
    elif not ctx["humans"]:
        result = Result(False, "hôte %s sans responsable humain résolu" % host, repository)
    else:
        visible, why = f.visible(ctx["repo"], ctx["humans"], ctx["logins"])
        result = Result(visible, why, repository)
    if ttl is None:
        ttl = float(getattr(db.cfg, "visibility_ttl", 300.0) or 300.0)
    storage.of(db).visibility.put(name, host, ok=result.ok, diagnostic=result.diagnostic,
                                  repository=result.repository, context=ctx["fingerprint"],
                                  ttl_s=ttl)
    return result


def annotate(canon, persona, host: str, verdict, *, db, forge: Forge | None = None):
    """Replie la visibilité dans un verdict de placement ADMIS (0029).

    Un verdict déjà refusé n'est pas touché. En cas d'écart, le verdict devient
    faux avec le diagnostic `persona-hidden-from-host`. Rend (visibilité|None,
    diagnostic de visibilité)."""
    if not getattr(verdict, "ok", False) or not getattr(persona, "memory_repository", None):
        return None, ""
    result = check(canon, persona, host, db=db, forge=forge)
    if not result.ok:
        verdict.ok = False
        verdict.diagnostic = "persona-hidden-from-host : %s" % result.diagnostic
    return result.ok, result.diagnostic
