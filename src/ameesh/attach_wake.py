# SPDX-License-Identifier: AGPL-3.0-only
"""#7863 : réveil natif d'une session `attach` idle.

Claude Code exécute un hook déclaré `asyncRewake` en tâche de fond et rouvre
un tour quand ce hook sort en **code 2** ; son stderr est remis au modèle
comme rappel système (qualifié sur 2.1.304 et 2.1.309, PTY, API simulée).
`agent-mail wait` est une veille en **lecture seule** : elle sort 2 à
l'arrivée d'un courrier non passif (`mail.passive_reason`), sans jamais
réserver ni marquer lu. Le hook agent-mail ordinaire reste donc le seul
chemin d'acquittement, et aucune double remise n'est possible.

La veille ne réveille que le courrier **arrivé après son démarrage** : un
message encore non lu au démarrage est déjà à la charge du hook ordinaire ;
le réveiller encore ferait une boucle sans fin (réveil → Stop → veille →
même message). C'est aussi pourquoi le Stop pose la veille avec un court
délai : le hook ordinaire livre d'abord le courrier du tour.

`asyncRewake` n'existe pas avant 2.1.304 et codex n'a pas d'équivalent :
l'attach lit la version réellement lancée et, sans preuve, ne pose rien
(repli #7861).

Le document est passé par `--settings`, qui **ajoute** des réglages à ceux du
harnais (vérifié : un hook utilisateur et le nôtre tournent tous les deux) :
le hook agent-mail de livraison reste chargé, et notre veille ne fait que
réveiller.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import shutil
import subprocess

from . import harness_versions

#: première version du harnais où le réveil `asyncRewake` est qualifié
MIN_VERSION = "2.1.304"
#: marge entre la fin de la veille et le timeout du hook
HOOK_TIMEOUT_MARGIN_S = 30
#: au Stop, laisse le hook agent-mail ordinaire livrer le courrier du tour
STOP_DELAY_S = 2.0
#: `claude --version` répond en moins d'une seconde ; un nœud chargé peut
#: mettre quelques secondes (même borne que `harness_versions`)
PROBE_TIMEOUT_S = 20.0


@dataclasses.dataclass
class Wake:
    """Réveil posé pour un attach, ou la raison de ne pas en poser.

    `path` est le fichier `--settings` écrit (None : rien de posé) ; `version`
    est la version réellement lancée, lue par `--version` (None : illisible).
    """
    path: str | None = None
    version: str | None = None
    reason: str = ""
    command: str = ""


def watcher_command(executable: str, name: str, seconds: float,
                    delay: float = 0.0) -> list[str]:
    """La commande de veille : lecture seule, sortie 2 à l'arrivée."""
    argv = [executable, "wait", "--agent", name,
            "--timeout", str(max(1, int(seconds)))]
    if delay > 0:
        argv += ["--delay", str(max(1, int(delay)))]
    return argv


def document(name: str, seconds: float, executable: str,
             stop_delay: float = STOP_DELAY_S) -> dict:
    """Le document `--settings` : SessionStart et Stop, tous deux asyncRewake.

    Le timeout du hook dépasse celui de la veille : c'est la veille qui rend
    la main (0 au délai), jamais une kill du harnais. `asyncRewake` implique
    l'asynchronisme : le tour initial et la TUI ne l'attendent pas.
    """
    timeout = max(1, int(seconds)) + HOOK_TIMEOUT_MARGIN_S

    def entry(delay: float) -> dict:
        return {"hooks": [{
            "type": "command",
            "command": shlex.join(
                watcher_command(executable, name, seconds, delay)),
            "timeout": timeout,
            "asyncRewake": True,
        }]}

    return {"hooks": {"SessionStart": [entry(0.0)],
                      "Stop": [entry(stop_delay)]}}


def probe_version(binary: str, env: dict) -> str | None:
    """La version du binaire qui va être lancé (`claude --version`)."""
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return harness_versions.parse_version(proc.stdout or proc.stderr)


def _write_settings(path: str, document_: dict) -> None:
    """Écrit le document en 0600, atomiquement (jamais un fichier partiel)."""
    temporary = "%s.tmp-%d" % (path, os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document_, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def prepare(cfg, name: str, *, harness: str, binary: str, env: dict,
            seconds: float, tag: str) -> Wake:
    """Prépare le réveil d'un attach, ou explique pourquoi il est absent.

    `tag` distingue les fichiers de réglages d'attach successifs (bail,
    processus) ; le fichier est retiré par `cleanup` au détachement.
    """
    if harness != "claude":
        return Wake(reason="harness %s: no asyncRewake channel (fallback #7861)"
                    % (harness or "?"))
    version = probe_version(binary, env)
    if version is None:
        return Wake(reason="claude version unreadable: no wake hook (fallback #7861)")
    if harness_versions.version_key(version) < harness_versions.version_key(MIN_VERSION):
        return Wake(version=version,
                    reason="claude %s < %s: no asyncRewake (fallback #7861)"
                    % (version, MIN_VERSION))
    executable = shutil.which("agent-mail", path=env.get("PATH")) or ""
    if not executable:
        return Wake(version=version,
                    reason="agent-mail not in PATH: no wake hook (fallback #7861)")
    seconds = max(5.0, float(seconds))
    path = os.path.join(cfg.agent_dir(name),
                        "attach-wake-%s-%d.json" % (tag, os.getpid()))
    try:
        _write_settings(path, document(name, seconds, executable))
    except OSError as exc:
        return Wake(version=version,
                    reason="settings not written (%s): no wake hook (fallback #7861)"
                    % exc)
    return Wake(path=path, version=version,
                command=shlex.join(watcher_command(executable, name, seconds)))


def cleanup(wake: Wake | None) -> None:
    """Retire le fichier de réglages d'un attach (idempotent)."""
    if wake is None or not wake.path:
        return
    try:
        os.unlink(wake.path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
