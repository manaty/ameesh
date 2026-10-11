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

L159c : les deux signalent aussi une unité d'exécuteur sans plafond mémoire,
hors de `ameesh-agents.slice`, ou dont `MemoryMax` s'écarte du plafond du
canon (`policy.resources.agent_memory_max` de la fiche Host, 6 Gio par
défaut), avec la commande qui corrige.

Lecture seule : rien n'est écrit, ni en base ni dans systemd.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from . import adapters, harnesses, platform, registry

#: modèle d'unité d'exécuteur par agent (docs/BASCULE.md, deploy/systemd)
RUNNER_TEMPLATE = "ameesh-runner-agent@%s.service"
#: unité SYSTÈME d'un serveur du mesh (profil L55, utilisateur `ameesh`) :
#: `ameesh-runner@<persona>` (L146)
SYSTEM_RUNNER_TEMPLATE = "ameesh-runner@%s.service"
#: premier argument de `run` : la commande vise le gestionnaire système
SYSTEM = "--system"
#: états `systemctl is-enabled` qui démarrent l'unité au boot
ENABLED_STATES = ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias")
_AGENTS_RE = re.compile(r"--agents[= ]([^\s;]+)")


#: L159c : slice qui regroupe les exécuteurs d'agent (deploy/systemd)
AGENTS_SLICE = "ameesh-agents.slice"
#: écart toléré entre `MemoryMax` de l'unité et le plafond du canon (arrondis)
CAP_TOLERANCE = 0.02

#: `systemctl` à utiliser ; VIDE, les unités ne sont pas vérifiées (tests)
SYSTEMCTL_ENV = "AMEESH_SYSTEMCTL"


def _systemctl(*args: str) -> str | None:
    """Sortie de `systemctl --user …` (`systemctl …` si le premier argument est
    `SYSTEM`), ou None (pas de systemd, erreur)."""
    choisi = os.environ.get(SYSTEMCTL_ENV)
    if choisi == "":
        return None
    binary = choisi or shutil.which("systemctl")
    if not binary or not platform.is_linux():
        return None
    scope = ["--user"]
    if args[:1] == (SYSTEM,):
        scope, args = [], args[1:]
    try:
        proc = subprocess.run([binary, *scope, *args], capture_output=True, text=True,
                              timeout=5.0, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if scope and proc.returncode != 0 and not proc.stdout:
        # pas de gestionnaire utilisateur (session sans bus : `runuser -l
        # ameesh` sur un serveur) : rien à lire, pas « aucune unité »
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

    Couvre, unités utilisateur puis unités système (L146) :
    `ameesh-runner-agent@<agent>.service` (poste) ou
    `ameesh-runner@<agent>.service` (serveur du mesh) activée ; toute autre
    unité activée dont le nom contient `runner` et qui lance ameesh —
    `--agents a,b` la limite à ces agents ; sans `--agents`, une instance
    `x@<agent>` mène son agent, une unité simple les mène tous
    (l'`agent-runner.service` de BASCULE.md)."""
    scopes = []
    for scope in ((), (SYSTEM,)):
        listing = run(*scope, "list-unit-files", "--type=service", "--no-legend", "--plain")
        if listing is not None:
            scopes.append((scope, listing))
    if not scopes:
        return None
    covered: dict[str, str] = {}
    generic: list[str] = []
    for scope, listing in scopes:
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            unit, state = parts[0], parts[1]
            if ("runner" not in unit or unit.endswith("@.service")
                    or state not in ENABLED_STATES):
                continue
            exec_start = run(*scope, "show", unit, "-p", "ExecStart", "--value") or ""
            if "ameesh" not in exec_start and "agent-runner" not in exec_start:
                continue
            match = _AGENTS_RE.search(exec_start)
            if match:
                for name in match.group(1).split(","):
                    if name:
                        covered.setdefault(name, unit)
            elif "@" in unit:
                covered.setdefault(unit.split("@", 1)[1].rsplit(".service", 1)[0], unit)
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
        out[name] = None
        for scope, template in (((), RUNNER_TEMPLATE), ((SYSTEM,), SYSTEM_RUNNER_TEMPLATE)):
            if scope not in [s for s, _listing in scopes]:
                continue
            unit = template % name
            state = (run(*scope, "is-enabled", unit) or "").strip().splitlines()
            if state and state[0] in ENABLED_STATES:
                out[name] = unit
                break
    return out


def missing_runner_lines(cfg, db, run=_systemctl) -> list[str]:
    """Lignes de `ameesh doctor` : agents menés sans unité d'exécuteur."""
    agents = served_agents(cfg, db)
    if not agents:
        return []
    coverage = runner_coverage([row["name"] for row in agents], run=run)
    if coverage is None:
        return ["exécuteurs : systemd indisponible — unités non vérifiées"]
    manquants = sorted(name for name, unit in coverage.items() if unit is None)
    if not manquants:
        return ["exécuteurs : %d agent(s) mené(s), tous avec une unité activée"
                % len(coverage)]
    lines = ["attention  : %d agent(s) mené(s) sans unité d'exécuteur activée sur %s "
             "(ne repart pas au redémarrage ; une session attach morte n'est reprise "
             "par personne) : %s" % (len(manquants), cfg.host, ", ".join(manquants))]
    # serveur du mesh (modèle système installé) : la commande système
    serveur = (SYSTEM_RUNNER_TEMPLATE % "") in (
        run(SYSTEM, "list-unit-files", "--type=service", "--no-legend", "--plain") or "")
    for name in manquants:
        lines.append("             systemctl enable --now %s" % (SYSTEM_RUNNER_TEMPLATE % name)
                     if serveur else
                     "             systemctl --user enable --now %s" % (RUNNER_TEMPLATE % name))
    return lines


def _size(octets: int) -> str:
    """Une taille pour systemd : en M (base 1024), sans décimale."""
    return "%dM" % (octets // 1024 ** 2)


def user_manager_controllers() -> set[str] | None:
    """Contrôleurs cgroup délégués au gestionnaire systemd de l'utilisateur,
    ou None (pas de cgroup v2, autre système)."""
    if not platform.is_linux():
        return None
    uid = os.getuid()
    path = "/sys/fs/cgroup/user.slice/user-%d.slice/user@%d.service/cgroup.controllers" % (uid, uid)
    try:
        with open(path, encoding="utf-8") as fh:
            return set(fh.read().split())
    except OSError:
        return None


def memory_cap_lines(cfg, db, canons=None, run=_systemctl,
                     controllers=user_manager_controllers) -> list[str]:
    """L159c : lignes de `ameesh doctor` sur les plafonds mémoire des unités
    `ameesh-runner-agent@<agent>` des agents menés par l'hôte."""
    agents = served_agents(cfg, db)
    if not agents:
        return []
    if canons is None:
        from . import canon as canon_mod
        try:
            canons = canon_mod.load_configured(cfg)
        except Exception:
            canons = []
    from . import resources as resources_mod
    lines, ok = [], 0
    for name in sorted(row["name"] for row in agents):
        unit = RUNNER_TEMPLATE % name
        shown = run("show", unit, "-p", "LoadState", "-p", "MemoryMax", "-p", "Slice")
        if shown is None:
            return ["plafonds   : systemd utilisateur indisponible — non vérifiés"]
        props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            continue  # pas d'unité du modèle : signalé par missing_runner_lines
        cap = resources_mod.agent_memory_cap(canons, cfg.host or "", name)
        problems = []
        if props.get("Slice") != AGENTS_SLICE:
            problems.append("slice %s (attendue : %s)" % (props.get("Slice") or "?", AGENTS_SLICE))
        try:
            current = int(props.get("MemoryMax") or "")
        except ValueError:
            current = None  # « infinity » : aucun plafond
        if current is None:
            problems.append("aucun plafond mémoire")
        elif abs(current - cap["max"]) > cap["max"] * CAP_TOLERANCE:
            problems.append("MemoryMax %s, le canon dit %s (%s)"
                            % (_size(current), _size(cap["max"]), cap["origin"]))
        if not problems:
            ok += 1
            continue
        lines.append("attention  : %s : %s" % (unit, " ; ".join(problems)))
        if props.get("Slice") != AGENTS_SLICE:
            lines.append("             réinstallez deploy/systemd (unité et %s), puis "
                         "systemctl --user daemon-reload" % AGENTS_SLICE)
        if current is None or abs(current - cap["max"]) > cap["max"] * CAP_TOLERANCE:
            lines.append("             systemctl --user set-property %s MemoryMax=%s MemoryHigh=%s"
                         % (unit, _size(cap["max"]), _size(cap["high"])))
    delegated = controllers()
    if delegated is not None and "memory" not in delegated:
        # sans délégation, systemd accepte MemoryMax mais ne l'applique pas
        lines.append("attention  : contrôleur mémoire non délégué au systemd utilisateur : "
                     "les plafonds ne s'appliquent pas (docs/EXPLOITATION.md, L159c)")
    if ok and not lines:
        lines.append("plafonds   : %d unité(s) d'agent dans %s, MemoryMax conforme au canon"
                     % (ok, AGENTS_SLICE))
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
        for line in missing_runner_lines(cfg, db) + memory_cap_lines(cfg, db):
            print(line)
    print("verdict    : %s" % ("OK" if verdict == 0 else "KO"))
    return verdict
