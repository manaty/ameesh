# SPDX-License-Identifier: AGPL-3.0-only
"""L'hôte est-il prêt à mener ses agents ? (lot L106)

  ameesh doctor --harness
        chaque harnais servi par l'hôte : binaire résolu (chemin absolu et
        provenance : variable, `harness_bins`, PATH, PATH du gestionnaire
        systemd, emplacement connu), interpréteur d'un script (`node` pour
        dsh) ; puis les agents menés sans unité d'exécuteur activée.

`ameesh doctor` (diagnostic complet) signale lui aussi un agent mené (mode
`execute`, cet hôte, non arrêté) sans unité d'exécuteur activée : un tel agent
ne repart pas après un redémarrage de l'hôte, et une session `attach` qui
meurt n'est reprise par personne.

Lecture seule : rien n'est écrit, ni en base ni dans systemd.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from . import adapters, harnesses, registry

#: modèle d'unité d'exécuteur par agent (docs/BASCULE.md, deploy/systemd)
RUNNER_TEMPLATE = "ameesh-runner-agent@%s.service"
#: états `systemctl is-enabled` qui démarrent l'unité au boot
ENABLED_STATES = ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias")
_AGENTS_RE = re.compile(r"--agents[= ]([^\s;]+)")


#: `systemctl` à utiliser ; VIDE, les unités ne sont pas vérifiées (tests)
SYSTEMCTL_ENV = "AMEESH_SYSTEMCTL"


def _systemctl(*args: str) -> str | None:
    """Sortie de `systemctl --user …`, ou None (pas de systemd, erreur)."""
    choisi = os.environ.get(SYSTEMCTL_ENV)
    if choisi == "":
        return None
    binary = choisi or shutil.which("systemctl")
    if not binary or not sys.platform.startswith("linux"):
        return None
    try:
        proc = subprocess.run([binary, "--user", *args], capture_output=True, text=True,
                              timeout=5.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout


def served_agents(cfg, db) -> list[dict]:
    """Les agents que l'hôte doit mener : cet hôte, mode `execute`, non arrêtés."""
    return [row for row in registry.overview(db)
            if (row.get("host") or "") == (cfg.host or "")
            and (row.get("mode") or "execute") == "execute"
            and row.get("status") != "stopped"]


def harness_report(agents: list[dict] | None = None) -> list[dict]:
    """Un constat par harnais : `harness`, `agents`, `ok`, `detail`.

    Sans agents (base injoignable, aucun agent), tous les descripteurs connus
    sont vérifiés."""
    par_harnais: dict[str, list[str]] = {}
    if agents:
        for row in agents:
            par_harnais.setdefault(row.get("harness") or "other", []).append(row["name"])
    else:
        for harness in harnesses.known_ids():
            par_harnais[harness] = []
    out = []
    for harness, noms in sorted(par_harnais.items()):
        try:
            adapter = adapters.adapter_for(harness)
            resolution = adapter.resolution
            out.append({"harness": harness, "agents": sorted(noms), "ok": True,
                        "detail": resolution.describe() if resolution else adapter.binary,
                        "path": resolution.path if resolution else adapter.binary,
                        "source": resolution.source if resolution else "",
                        "interpreter": resolution.interpreter_path if resolution else ""})
        except adapters.HarnessMissing as exc:
            out.append({"harness": harness, "agents": sorted(noms), "ok": False,
                        "detail": str(exc)})
    return out


def runner_coverage(names: list[str], run=_systemctl) -> dict | None:
    """L'unité d'exécuteur ACTIVÉE (démarrée au boot) qui mène chaque agent,
    ou None pour un agent qu'aucune ne mène. None en entier sans systemd.

    Couvre : `ameesh-runner-agent@<agent>.service` activée ; toute autre unité
    utilisateur activée dont le nom contient `runner` et qui lance ameesh —
    `--agents a,b` la limite à ces agents, sans `--agents` elle les mène tous
    (l'`agent-runner.service` de BASCULE.md)."""
    listing = run("list-unit-files", "--type=service", "--no-legend", "--plain")
    if listing is None:
        return None
    covered: dict[str, str] = {}
    generic: list[str] = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        unit, state = parts[0], parts[1]
        if "runner" not in unit or unit.endswith("@.service") or state not in ENABLED_STATES:
            continue
        exec_start = run("show", unit, "-p", "ExecStart", "--value") or ""
        if "ameesh" not in exec_start and "agent-runner" not in exec_start:
            continue
        match = _AGENTS_RE.search(exec_start)
        if match:
            for name in match.group(1).split(","):
                if name:
                    covered.setdefault(name, unit)
        else:
            generic.append(unit)
    out: dict[str, str | None] = {}
    for name in names:
        if name in covered:
            out[name] = covered[name]
            continue
        if generic:
            out[name] = generic[0]
            continue
        unit = RUNNER_TEMPLATE % name
        state = (run("is-enabled", unit) or "").strip().splitlines()
        out[name] = unit if state and state[0] in ENABLED_STATES else None
    return out


def missing_runner_lines(cfg, db, run=_systemctl) -> list[str]:
    """Lignes de `ameesh doctor` : agents menés sans unité d'exécuteur."""
    agents = served_agents(cfg, db)
    if not agents:
        return []
    coverage = runner_coverage([row["name"] for row in agents], run=run)
    if coverage is None:
        return ["exécuteurs : systemd utilisateur indisponible — unités non vérifiées"]
    manquants = sorted(name for name, unit in coverage.items() if unit is None)
    if not manquants:
        return ["exécuteurs : %d agent(s) mené(s), tous avec une unité activée"
                % len(coverage)]
    lines = ["attention  : %d agent(s) mené(s) sans unité d'exécuteur activée sur %s "
             "(ne repart pas au redémarrage ; une session attach morte n'est reprise "
             "par personne) : %s" % (len(manquants), cfg.host, ", ".join(manquants))]
    for name in manquants:
        lines.append("             systemctl --user enable --now %s" % (RUNNER_TEMPLATE % name))
    return lines


def cmd_harness(cfg, db=None) -> int:
    """`ameesh doctor --harness` : 0 si chaque harnais servi est trouvé."""
    adapters.configure(cfg)
    agents = None
    if db is not None:
        try:
            agents = served_agents(cfg, db)
        except Exception as exc:  # diagnostic : on continue sans la base
            print("agents     : illisibles (%s) — tous les descripteurs vérifiés"
                  % type(exc).__name__)
    print("hôte       : %s" % cfg.host)
    print("PATH       : %s" % (os.environ.get("PATH") or "(vide)"))
    systemd_path = adapters.systemd_manager_path()
    if systemd_path and systemd_path != os.environ.get("PATH"):
        print("PATH systemd : %s" % systemd_path)
    verdict = 0
    for item in harness_report(agents):
        qui = " [%s]" % ", ".join(item["agents"]) if item["agents"] else ""
        if item["ok"]:
            print("harnais    : %s%s → %s" % (item["harness"], qui, item["detail"]))
            if item.get("source") in ("PATH du gestionnaire systemd", "emplacement connu"):
                print("             conseil : trouvé hors du PATH — posez PATH dans l'unité "
                      "(deploy/systemd) ou `harness_bins` dans la configuration")
        else:
            verdict = 1
            print("harnais    : %s%s KO — %s" % (item["harness"], qui, item["detail"]))
    if db is not None and agents is not None:
        for line in missing_runner_lines(cfg, db):
            print(line)
    print("verdict    : %s" % ("OK" if verdict == 0 else "KO"))
    return verdict
