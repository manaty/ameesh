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

import json
import os
import re
import shlex
import shutil
import subprocess

from . import adapters, harness_versions, harnesses, platform, registry

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

#: L498 : monture de la hiérarchie cgroup v2 unifiée
CGROUP_ROOT = "/sys/fs/cgroup"
#: L498 : sous-groupe de l'exécuteur dans son unité déléguée
#: (`DelegateSubgroup=`), la racine déléguée restant libre de processus
TURN_CGROUP_SUBGROUP = "runner"

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


# --------------------------------------------------------------------------
# L514 : les hooks de courrier des harnais
# --------------------------------------------------------------------------

def mail_hooks_of(descriptor) -> dict:
    """L514 : la section `ameesh.hooks.mail` d'un descripteur, ou {}."""
    hooks = getattr(descriptor, "hooks", None)
    mail = hooks.get("mail") if isinstance(hooks, dict) else None
    return mail if isinstance(mail, dict) else {}


def hook_file_of(descriptor) -> str:
    """L514 : nom du fichier de hooks du harnais (`ameesh.hooks.mail.file`) ;
    `hooks.json` sinon."""
    name = mail_hooks_of(descriptor).get("file")
    return name if isinstance(name, str) and name else "hooks.json"


def hook_events_of(descriptor) -> list[str]:
    """L514 : les événements que le harnais doit servir (`hooks.mail.events`)."""
    events = mail_hooks_of(descriptor).get("events")
    return [str(e) for e in events] if isinstance(events, list) else []


def hook_dirs(cfg, harness: str, environ=None) -> list[str]:
    """L514 : dossiers de configuration à regarder pour ce harnais : celui de
    l'environnement (`DSH_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`), sinon son
    défaut, puis les comptes déclarés (L30/L95) — jamais deux fois le même."""
    from . import accounts
    environ = os.environ if environ is None else environ
    variable = accounts.config_env_of(harness)
    primary = (environ.get(variable) if variable else "") or accounts.default_home_of(harness)
    out: list[str] = []
    for home in [primary, *accounts.homes(cfg, harness)]:
        chemin = os.path.abspath(os.path.expanduser(home))
        if chemin not in out:
            out.append(chemin)
    return out


def hook_commands(document) -> list[tuple[str, str]]:
    """L514 : les (événement, commande) d'un fichier de hooks de harnais.

    Format Claude Code, repris par dsh et Codex :
    `{"hooks": {"<événement>": [{"hooks": [{"type": "command", "command": "…"}]}]}}`.
    Un document d'une autre forme ne rend rien (jamais une erreur)."""
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return []
    out: list[tuple[str, str]] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if isinstance(command, str) and command.strip():
                    out.append((str(event), command.strip()))
    return out


def _hook_binary(command: str) -> str:
    """Le premier mot d'une commande de hook (guillemets respectés)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


def _binary_missing(binary: str, *, path: str | None = None) -> bool:
    """Le binaire d'une commande de hook est-il introuvable ? Un chemin
    (absolu ou avec un séparateur) est contrôlé sur place ; un nom seul passe
    par `path` s'il est fourni, sinon par le PATH ambiant — c'est le cas de
    l'incident du 2026-10-12 (`/usr/local/bin/agent-mail` absent, tous les
    hooks en 127)."""
    chemin = os.path.expanduser(binary)
    if os.sep in chemin:
        return not (os.path.isfile(chemin) and os.access(chemin, os.X_OK))
    return shutil.which(chemin, path=path or None) is None


def _binary_missing_where(binary: str, manager_path: str) -> str:
    """Où le binaire d'un hook est introuvable : la raison lisible, sinon "".

    Un nom seul est cherché dans **deux** contextes : le PATH de la session
    du doctor (le contrôle d'origine) et, quand il est lisible, le PATH du
    gestionnaire systemd (`adapters.systemd_manager_path`). Le PATH réel du
    service est celui de son unité (`Environment=PATH`, qu'un EnvironmentFile
    peut encore remplacer) : aucun des deux ne le prouve à lui seul, et la
    présence dans l'un ne doit pas masquer l'absence dans l'autre — un
    binaire absent d'un contexte peut y sortir en 127 (#1395, avis bloquant
    cx2 : la résolution « gestionnaire seul » rendait ok un hook introuvable
    sous le PATH réel). Un chemin est contrôlé sur place, sans contexte."""
    if os.sep in os.path.expanduser(binary):
        return "not found" if _binary_missing(binary) else ""
    missing = []
    if _binary_missing(binary):
        missing.append("this session's PATH")
    if manager_path and _binary_missing(binary, path=manager_path):
        missing.append("the systemd manager PATH")
    return "not found under %s" % " or ".join(missing) if missing else ""


def hook_lines(cfg, ids, environ=None) -> dict:
    """L514 : lignes de `ameesh doctor --harness` sur les hooks de courrier
    installés pour les harnais `ids` ; `{"lines": [...], "ok": bool}`.

    `ok` est faux seulement pour un défaut certain : un fichier de hooks
    illisible, ou une commande de hook dont le binaire est introuvable (le
    hook sort alors en 127 : ni courrier en cours de tour, ni Stop). Un
    fichier absent ou un événement non configuré est un avertissement, pas un
    KO — l'agent travaille, mais sans livraison entre deux outils.

    #514 N2 : un nom de binaire seul est contrôlé dans les deux contextes qui
    # peuvent lancer le hook — la session du doctor et le PATH du gestionnaire
    # systemd (`adapters.systemd_manager_path`) ; le PATH réel du service
    # reste celui de son unité, donc un binaire absent de l'un des deux est
    # signalé : le gestionnaire seul ne prouve pas que le hook tournera
    # (#1395, avis bloquant cx2). Le doctor reste en lecture seule."""
    environ = os.environ if environ is None else environ
    lines: list[str] = []
    ok = True
    manager_path = adapters.systemd_manager_path(environ)
    for harness in ids:
        descriptor = harnesses.get(harness)
        name = hook_file_of(descriptor)
        expected = hook_events_of(descriptor)
        directories = hook_dirs(cfg, harness, environ)
        found = [os.path.join(directory, name) for directory in directories
                 if os.path.isfile(os.path.join(directory, name))]
        if not found:
            lines.append("hooks      : %s — no %s (%s): mail read at the next turn, "
                         "without mid-turn delivery"
                         % (harness, name, ", ".join(directories)))
            continue
        for path in found:
            try:
                with open(path, encoding="utf-8") as fh:
                    document = json.load(fh)
            except (OSError, ValueError) as exc:
                ok = False
                lines.append("hooks      : %s KO — %s unreadable (%s)"
                             % (harness, path, " ".join(str(exc).split())[:120]))
                continue
            commands = [(event, command) for event, command in hook_commands(document)
                        if "hook" in command and ("agent-mail" in command
                                                  or "agent-mesh" in command
                                                  or "ameesh" in command)]
            if not commands:
                lines.append("hooks      : %s — no mail hook in %s: mail read at the "
                             "next turn, without mid-turn delivery" % (harness, path))
                continue
            events = sorted({event for event, _command in commands})
            binaries = []
            for _event, command in commands:
                binary = _hook_binary(command)
                if binary and binary not in binaries:
                    binaries.append(binary)
                if not binary:
                    continue
                where = _binary_missing_where(binary, manager_path)
                if where:
                    ok = False
                    lines.append("hooks      : %s KO — %s calls %s: %s (the hook "
                                 "exits 127: no delivery between two tool calls, "
                                 "no Stop)" % (harness, path, command, where))
            if binaries and all(not _binary_missing_where(binary, manager_path)
                                for binary in binaries):
                lines.append("hooks      : %s → %d event(s) (%s), %s"
                             % (harness, len(events), ", ".join(events),
                                ", ".join(binaries)))
            missing = [event for event in expected if event not in events]
            if missing:
                why = []
                if "PostToolUse" in missing:
                    why.append("no delivery between tool calls")
                if "Stop" in missing:
                    why.append("no end-of-turn delivery")
                lines.append("warning    : hooks %s: %s not configured in %s — %s"
                             % (harness, ", ".join(missing), path,
                                "; ".join(why) or "events missing"))
    return {"lines": lines, "ok": ok}


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
        return ["runners    : systemd unavailable — units not checked"]
    manquants = sorted(name for name, unit in coverage.items() if unit is None)
    if not manquants:
        return ["runners    : %d agent(s) run here, all with an enabled unit"
                % len(coverage)]
    lines = ["warning    : %d agent(s) run without an enabled runner unit on %s "
             "(not restarted after a reboot; nobody resumes a dead attach "
             "session): %s" % (len(manquants), cfg.host, ", ".join(manquants))]
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
            return ["memory caps: user systemd unavailable — not checked"]
        props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            continue  # pas d'unité du modèle : signalé par missing_runner_lines
        cap = resources_mod.agent_memory_cap(canons, cfg.host or "", name)
        problems = []
        if props.get("Slice") != AGENTS_SLICE:
            problems.append("slice %s (expected: %s)" % (props.get("Slice") or "?", AGENTS_SLICE))
        try:
            current = int(props.get("MemoryMax") or "")
        except ValueError:
            current = None  # « infinity » : aucun plafond
        if current is None:
            problems.append("no memory cap")
        elif abs(current - cap["max"]) > cap["max"] * CAP_TOLERANCE:
            problems.append("MemoryMax %s, the canon says %s (%s)"
                            % (_size(current), _size(cap["max"]), cap["origin"]))
        if not problems:
            ok += 1
            continue
        lines.append("warning    : %s: %s" % (unit, "; ".join(problems)))
        if props.get("Slice") != AGENTS_SLICE:
            lines.append("             reinstall deploy/systemd (unit and %s), then "
                         "systemctl --user daemon-reload" % AGENTS_SLICE)
        if current is None or abs(current - cap["max"]) > cap["max"] * CAP_TOLERANCE:
            lines.append("             systemctl --user set-property %s MemoryMax=%s MemoryHigh=%s"
                         % (unit, _size(cap["max"]), _size(cap["high"])))
    delegated = controllers()
    if delegated is not None and "memory" not in delegated:
        # sans délégation, systemd accepte MemoryMax mais ne l'applique pas
        lines.append("warning    : memory controller not delegated to user systemd: "
                     "caps are not enforced (docs/EXPLOITATION.md, L159c)")
    if ok and not lines:
        lines.append("memory caps: %d agent unit(s) in %s, MemoryMax matches the canon"
                     % (ok, AGENTS_SLICE))
    return lines


def turn_cgroup_lines(cfg, db, run=_systemctl) -> list[str]:
    """L498 : lignes de `ameesh doctor` sur le cgroup délégué par tour.

    Lecture seule : pour chaque unité d'agent menée par l'hôte, l'unité
    délègue-t-elle (`Delegate=yes` + `DelegateSubgroup=runner`), et sa racine
    déléguée est-elle utilisable — `cgroup.kill` et `cgroup.events` présents,
    répertoire inscriptible, **aucun processus** dedans (contrainte
    « no internal process » : sans elle, les enfants du tour ne peuvent pas
    être créés proprement) ? Là où ce n'est pas posé, le repli PID/PGID reste
    en vigueur : signalé, sans changer le verdict."""
    if not platform.is_linux():
        return []
    agents = served_agents(cfg, db)
    if not agents:
        return []
    lines, ok, pending = [], 0, 0
    for name in sorted(row["name"] for row in agents):
        unit = RUNNER_TEMPLATE % name
        shown = run("show", unit, "-p", "LoadState", "-p", "Delegate",
                    "-p", "DelegateSubgroup", "-p", "ControlGroup")
        if shown is None:
            return ["turn cgroups: user systemd unavailable — not checked"]
        props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            continue  # pas d'unité du modèle : signalé par missing_runner_lines
        delegated = (props.get("Delegate") == "yes"
                     and props.get("DelegateSubgroup") == TURN_CGROUP_SUBGROUP)
        if not delegated:
            pending += 1
            continue
        problems = []
        control = (props.get("ControlGroup") or "").rstrip("/")
        if not control:
            problems.append("unit not running (no ControlGroup)")
        else:
            root = CGROUP_ROOT + control
            if not os.path.isdir(root):
                problems.append("delegated root %s missing" % root)
            else:
                for name_file in ("cgroup.kill", "cgroup.events"):
                    if not os.path.exists(os.path.join(root, name_file)):
                        problems.append("%s missing in %s" % (name_file, root))
                if not os.access(root, os.W_OK | os.X_OK):
                    problems.append("delegated root not writable (uid %d)" % os.getuid())
                try:
                    with open(os.path.join(root, "cgroup.procs"), encoding="utf-8") as fh:
                        members = fh.read().split()
                except OSError:
                    members = None
                if members is None:
                    problems.append("%s/cgroup.procs unreadable" % root)
                elif members:
                    problems.append("%d own process(es) in the delegated root "
                                    "(DelegateSubgroup=%s not effective)"
                                    % (len(members), TURN_CGROUP_SUBGROUP))
        if not problems:
            ok += 1
            continue
        lines.append("warning    : %s: per-turn cgroup unusable (%s); keeps "
                     "PID/PGID ownership (L498)" % (unit, "; ".join(problems)))
    if ok and not lines:
        lines.append("turn cgroups: %d runner unit(s) delegated, root free and "
                     "writable (L498)" % ok)
    if pending and not lines:
        lines.append("turn cgroups: %d runner unit(s) not delegated — PID/PGID "
                     "ownership (L498 canary pending)" % pending)
    elif pending and lines:
        lines.append("turn cgroups: %d further unit(s) not delegated — PID/PGID "
                     "ownership (L498 canary pending)" % pending)
    return lines


def cmd_harness(cfg, db=None) -> int:
    """`ameesh doctor --harness` : 0 si chaque harnais servi est trouvé."""
    adapters.configure(cfg)
    agents = None
    if db is not None:
        try:
            agents = served_agents(cfg, db)
        except Exception as exc:  # diagnostic : on continue sans la base
            print("agents     : unreadable (%s) — checking all descriptors"
                  % type(exc).__name__)
    print("host       : %s" % cfg.host)
    print("PATH       : %s" % (os.environ.get("PATH") or "(empty)"))
    systemd_path = adapters.systemd_manager_path()
    if systemd_path and systemd_path != os.environ.get("PATH"):
        print("PATH systemd : %s" % systemd_path)
    verdict = 0
    items = harness_report(agents)
    for item in items:
        qui = " [%s]" % ", ".join(item["agents"]) if item["agents"] else ""
        if item["ok"]:
            print("harness    : %s%s → %s" % (item["harness"], qui, item["detail"]))
            # L120: the version, read now (doctor records nothing)
            version = harness_versions.probe(item["harness"])
            print("             version %s" % (version["version"] or "unknown (%s)"
                                               % (version["error"] or "?")))
            if item.get("source") in ("systemd manager PATH", "known location"):
                print("             hint: found outside PATH — set PATH in the unit "
                      "(deploy/systemd) or `harness_bins` in the config")
        else:
            verdict = 1
            print("harness    : %s%s KO — %s" % (item["harness"], qui, item["detail"]))
    # L514 : un hook de courrier dont le binaire est introuvable sort en 127 —
    # aucune remise entre deux outils, aucun Stop ; le découvrir ici, pas dans
    # une session d'agent.
    hooks = hook_lines(cfg, [item["harness"] for item in items])
    for line in hooks["lines"]:
        print(line)
    if not hooks["ok"]:
        verdict = 1
    if db is not None and agents is not None:
        for line in missing_runner_lines(cfg, db) + memory_cap_lines(cfg, db):
            print(line)
    print("verdict    : %s" % ("OK" if verdict == 0 else "KO"))
    return verdict
