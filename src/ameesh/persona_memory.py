# SPDX-License-Identifier: AGPL-3.0-only
"""Mémoire de persona (lot L54, décisions 0029 et 0032 §5, étude « persona et
harnais » §4, lot P4).

La mémoire appartient à la persona, pas à une session ni à un harnais : un
dépôt git par persona (`memory.repository` de sa fiche), au format neutre :

    MEMORY.md                 index, une ligne par souvenir (≤ 200 lignes)
    souvenirs/<sujet>.md      un souvenir par fichier (frontmatter : type, modified)
    journal/<session>.md      ajouts d'UNE session, en ajout seul

ameesh n'en est pas propriétaire (ni table, ni format en base). Ce composant :

* **ouvre** une copie de travail dans le dossier d'état de l'hôte (0700) ;
* **désigne** la mémoire au premier message d'une session neuve : l'index et
  la règle d'écriture (journal de la session seulement) ;
* **synchronise** à la fin de chaque tour : commit du journal et push (le dépôt
  distant sert de verrou ; un push refusé garde la copie et le signale) ;
* refuse tout commit qui contient un secret probable ;
* prépare le **tour de mémoire** (consolidation du journal en souvenirs).

L'accès au dépôt est celui de l'hôte (agent SSH, ou `persona_memory.ssh_key`
dans la configuration de l'hôte : une clé limitée à ce dépôt).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

INDEX = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 8000
GIT_TIMEOUT = 120

#: motifs de secrets refusés au commit (filtre prudent : en cas de doute, refus)
SECRET_PATTERNS = [
    re.compile(p) for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bxox[abpr]-[A-Za-z0-9-]{10,}",
        r"(?i)\b(pass(word)?|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}",
    )
]


class MemoryError(RuntimeError):
    pass


def copy_dir(state_dir: str, agent: str) -> str:
    return os.path.join(state_dir, "memoire", agent)


def _git_env(cfg_raw: dict | None) -> dict:
    env = os.environ.copy()
    key = (cfg_raw or {}).get("ssh_key")
    if key:
        env["GIT_SSH_COMMAND"] = "ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes" % key
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def _git(path: str, *args: str, env=None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, env=env)
    if check and proc.returncode != 0:
        raise MemoryError("git %s : %s" % (" ".join(args[:2]),
                                           (proc.stderr or proc.stdout).strip()[-300:]))
    return proc


def open_copy(repository: str, state_dir: str, agent: str, *, cfg_raw=None) -> str:
    """Clone (ou met à jour) la copie de travail de la mémoire de `agent`."""
    path = copy_dir(state_dir, agent)
    env = _git_env(cfg_raw)
    if os.path.isdir(os.path.join(path, ".git")):
        _git(path, "pull", "--rebase", "--autostash", "-q", env=env)
        return path
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    proc = subprocess.run(["git", "clone", "-q", repository, path], capture_output=True,
                          text=True, timeout=GIT_TIMEOUT, env=env)
    if proc.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        raise MemoryError("clonage de la mémoire impossible : %s" % proc.stderr.strip()[-300:])
    os.chmod(path, 0o700)
    _git(path, "config", "user.name", agent)
    _git(path, "config", "user.email", "%s@personas.ameesh.invalid" % agent)
    return path


def index_text(path: str) -> str:
    try:
        with open(os.path.join(path, INDEX), encoding="utf-8") as fh:
            lines = fh.read(INDEX_MAX_BYTES).splitlines()[:INDEX_MAX_LINES]
    except OSError:
        return ""
    return "\n".join(lines).strip()


def journal_path(path: str, session: str | None) -> str:
    stamp = session or time.strftime("session-%Y%m%dT%H%M%S")
    return os.path.join(path, "journal", "%s.md" % re.sub(r"[^A-Za-z0-9._-]", "-", stamp))


def preface(path: str, agent: str, session: str | None) -> str:
    """Le texte mis en tête du premier message d'une session neuve."""
    index = index_text(path)
    lines = [
        "MÉMOIRE DE PERSONA (%s). Ta mémoire durable est le dépôt %s." % (agent, path),
        "Index (%s) :" % INDEX,
        index or "(vide : tu n'as encore aucun souvenir)",
        "Lis un souvenir (souvenirs/<sujet>.md) quand il concerne ta tâche.",
        "Règle d'écriture : n'écris QUE dans %s, en ajout seul : ce que tu apprends "
        "qui servira plus tard (décisions, préférences des humains, pièges, "
        "références). Jamais de secret (clé, jeton, mot de passe). ameesh le "
        "sauvegarde à la fin de chaque tour ; la consolidation en souvenirs se "
        "fait lors d'un tour de mémoire." % os.path.relpath(journal_path(path, session), path),
        "",
    ]
    return "\n".join(lines) + "\n"


def secrets_in(path: str) -> list[str]:
    """Fichiers modifiés de la copie qui contiennent un secret probable."""
    diff = _git(path, "diff", "--cached", "--name-only").stdout.split()
    found = []
    for name in diff:
        try:
            with open(os.path.join(path, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if any(p.search(text) for p in SECRET_PATTERNS):
            found.append(name)
    return found


def sync(path: str, message: str, *, cfg_raw=None) -> dict:
    """Commit de ce qui a changé et push. Rend `{"status": …}` :
    `inchangee`, `poussee`, `secret` (commit refusé), `push_refuse` (copie gardée)."""
    if not os.path.isdir(os.path.join(path, ".git")):
        raise MemoryError("pas de copie de mémoire ouverte : %s" % path)
    _git(path, "add", "-A")
    if not _git(path, "diff", "--cached", "--quiet", check=False).returncode:
        return {"status": "inchangee"}
    suspects = secrets_in(path)
    if suspects:
        _git(path, "reset", "-q")
        return {"status": "secret", "files": suspects}
    _git(path, "commit", "-q", "-m", message)
    env = _git_env(cfg_raw)
    push = _git(path, "push", "-q", env=env, check=False)
    if push.returncode != 0:
        pull = _git(path, "pull", "--rebase", "-q", env=env, check=False)
        push = _git(path, "push", "-q", env=env, check=False) if pull.returncode == 0 else pull
    if push.returncode != 0:
        return {"status": "push_refuse", "error": (push.stderr or "").strip()[-300:]}
    return {"status": "poussee", "commit": _git(path, "rev-parse", "--short", "HEAD").stdout.strip()}


def close(path: str, *, cfg_raw=None) -> dict:
    """Dernière synchronisation, puis effacement de la copie (si tout est poussé)."""
    if not os.path.isdir(path):
        return {"status": "absente"}
    r = sync(path, "mémoire : fermeture de la copie", cfg_raw=cfg_raw)
    if r["status"] in ("inchangee", "poussee"):
        ahead = _git(path, "rev-list", "--count", "@{u}..HEAD", check=False).stdout.strip()
        if ahead in ("", "0"):
            shutil.rmtree(path)
            return {"status": "effacee"}
    return {"status": "gardee", "raison": r}


CONSOLIDATION_PROMPT = (
    "TOUR DE MÉMOIRE. Consolide ta mémoire de persona dans %(path)s : relis les "
    "journaux (journal/*.md), et pour chaque élément durable crée ou mets à jour un "
    "souvenir souvenirs/<sujet>.md (frontmatter : type user|feedback|project|reference, "
    "modified) puis une ligne dans MEMORY.md (≤ 200 lignes, une par souvenir). Supprime "
    "les souvenirs devenus faux. Ne touche pas aux journaux (ils restent archivés). "
    "Aucun secret. ameesh commitera et poussera à la fin du tour."
)


def consolidation_prompt(path: str) -> str:
    return CONSOLIDATION_PROMPT % {"path": path}


# --------------------------------------------------------------------------
# ligne de commande : ameesh memory status|consolidate|close <persona>
# --------------------------------------------------------------------------

USAGE = """\
ameesh memory status <persona>        dépôt, copie de travail, changements non poussés
ameesh memory consolidate <persona>   lui demande un tour de mémoire (journal → souvenirs)
ameesh memory close <persona>         dernière poussée puis effacement de la copie locale
"""


def main(argv) -> int:
    import sys
    from . import db as db_mod, mail
    from .config import load as load_config
    if len(argv) != 2 or argv[0] not in ("status", "consolidate", "close"):
        print(USAGE, file=sys.stderr if argv and argv[0] not in ("-h", "--help") else sys.stdout)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    sub, agent = argv
    cfg = load_config()
    db = db_mod.connect(cfg)
    try:
        rows = db.query("SELECT memory_repository FROM agent_registry WHERE name = %s", (agent,))
        if not rows:
            print("ameesh memory : persona inconnue %s" % agent, file=sys.stderr)
            return 2
        repo = rows[0].get("memory_repository")
        path = copy_dir(cfg.state_dir, agent)
        if sub == "status":
            print("persona : %s\ndépôt   : %s\ncopie   : %s" % (
                agent, repo or "aucun (pas de memory.repository dans sa fiche)",
                path if os.path.isdir(path) else "aucune"))
            if os.path.isdir(os.path.join(path, ".git")):
                state = _git(path, "status", "--short", check=False).stdout.strip()
                ahead = _git(path, "rev-list", "--count", "@{u}..HEAD", check=False).stdout.strip()
                print("non commité : %s\nnon poussé  : %s commit(s)" % (
                    "rien" if not state else "%d fichier(s)" % len(state.splitlines()),
                    ahead or "?"))
            return 0
        if not repo:
            print("ameesh memory : %s n'a pas de dépôt de mémoire" % agent, file=sys.stderr)
            return 2
        if sub == "consolidate":
            if not os.path.isdir(os.path.join(path, ".git")):
                path = open_copy(repo, cfg.state_dir, agent, cfg_raw=cfg.persona_memory)
            mail.send(db, "memoire", agent, consolidation_prompt(path), kind="request")
            print("tour de mémoire demandé à %s" % agent)
            return 0
        r = close(path, cfg_raw=cfg.persona_memory)
        print("copie de %s : %s" % (agent, r["status"]))
        return 0 if r["status"] in ("effacee", "absente") else 1
    except MemoryError as exc:
        print("ameesh memory : %s" % exc, file=sys.stderr)
        return 1
    finally:
        db.close()
