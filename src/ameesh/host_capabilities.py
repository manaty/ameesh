# SPDX-License-Identifier: AGPL-3.0-only
"""#7926: capacités déclarées d'un hôte, provisioning et vérification.

Une fiche `Host` peut déclarer, au même niveau que `tags` :

    capabilities: [android-emulator]

Chaque capacité connue nomme les groupes Unix que le compte des exécuteurs
doit avoir sur l'hôte. `android-emulator` exige le groupe `kvm` (accès à
`/dev/kvm`) ; rien d'autre n'est élargi : ni `chmod`, ni ACL, ni règle udev
`0666`.

Trois lecteurs, sans autorité propre :

* l'installation (`deploy/guest/install.sh`, le `install.sh` que la fiche
  d'AG3 nomme) n'ajoute un groupe que si la fiche le déclare (`apply`,
  `--dry-run`, `--check`) ;
* `ameesh doctor` compare la déclaration à l'état réel, dans les deux sens :
  une capacité déclarée non appliquée est un KO, un groupe accordé sans
  déclaration est une dérive signalée sans bloquer l'hôte ; quand le compte
  a le groupe mais que le processus courant (l'exécuteur) ne l'a pas encore,
  le docteur le dit : les exécuteurs lancés avant l'application n'ont
  `/dev/kvm` qu'après redémarrage ;
* le placement et `ameesh agent observe` lisent la déclaration sans rien
  décider (`hosts_with`, `card_for`).

La fiche est lue à la **tête relue** du canon (objets git de `origin/main`,
ou de `AMEESH_CANON_REF`), jamais dans un arbre de travail que le compte
`ameesh` peut réécrire, et le parcours est borné à `hotes/` : un worktree
imbriqué (`w7248/`) ne déclare rien. Seul un canon explicitement
`untrusted` (essai, prototype) est lu dans ses fichiers.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import stat as stat_mod
import subprocess
import sys

from . import canon

#: la capacité d'émulateur Android (e2e Maestro, lot Nexlink #7918)
ANDROID_EMULATOR = "android-emulator"

#: capacité -> groupes Unix requis sur l'hôte, dans l'ordre d'application.
#: Une capacité inconnue n'accorde rien : le schéma du canon l'ignore.
CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    ANDROID_EMULATOR: ("kvm",),
}

#: capacités connues ; le schéma du canon refuse (avertit sur) les autres
KNOWN = frozenset(CAPABILITY_GROUPS)

#: le périphérique de virtualisation de l'émulateur Android
KVM_DEVICE = "/dev/kvm"

#: compte des exécuteurs sur un hôte d'agents
DEFAULT_USER = "ameesh"

#: variable qui nomme le compte des exécuteurs quand la fiche ne le fait pas
USER_ENV = "AMEESH_HOST_USER"

#: dossier des fiches Host dans un canon OKF
HOTES = "hotes"

#: branche canonique lue quand rien ne nomme la révision relue
DEFAULT_CANON_REF = "origin/main"

#: variable qui nomme la révision relue (`origin/main`, un SHA, `HEAD`…)
CANON_REF_ENV = "AMEESH_CANON_REF"

#: toutes les variables de groupe connues, pour la dérive inverse
ALL_GROUPS = tuple(sorted({group for groups in CAPABILITY_GROUPS.values()
                           for group in groups}))


def parse(value) -> tuple[list[str], list[str], list[str]]:
    """`(connues, problèmes, inconnues)` d'un champ `capabilities`.

    Une valeur absente ne déclare rien. Une valeur illisible ne déclare rien
    non plus (fail closed) et nomme le problème ; un nom inconnu est rendu à
    part pour que `canon check` l'avertisse sans l'appliquer.
    """
    if value is None:
        return [], [], []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or not all(
            isinstance(item, (str, int, float)) and not isinstance(item, bool)
            for item in value):
        return [], ["`capabilities`: list of strings expected"], []
    names: list[str] = []
    problems: list[str] = []
    for item in value:
        name = str(item).strip()
        if not name:
            problems.append("`capabilities`: empty capability name")
        elif name not in names:
            names.append(name)
    return ([name for name in names if name in KNOWN],
            problems,
            [name for name in names if name not in KNOWN])


def from_card(data: dict) -> tuple[list[str], list[str], list[str]]:
    """Capacités connues, problèmes et inconnues d'un frontmatter de Host."""
    return parse((data or {}).get("capabilities"))


def groups_of(capabilities) -> list[str]:
    """Groupes Unix requis par ces capacités, sans doublon et dans l'ordre."""
    out: list[str] = []
    for capability in capabilities or ():
        for group in CAPABILITY_GROUPS.get(str(capability), ()):
            if group not in out:
                out.append(group)
    return out


def user_name() -> str:
    """Le compte dont `doctor` vérifie l'appartenance aux groupes.

    `AMEESH_HOST_USER` d'abord (provisioning explicite), sinon l'utilisateur
    effectif ; lancé en root, le compte des exécuteurs (`ameesh`) quand il
    existe, pour ne pas déclarer vert un root qui n'exécute aucun agent.
    """
    named = (os.environ.get(USER_ENV) or "").strip()
    if named:
        return named
    try:
        effective = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:  # compte sans entrée passwd : rien de prouvable
        return DEFAULT_USER
    if effective == "root":
        try:
            pwd.getpwnam(DEFAULT_USER)
        except KeyError:
            return effective
        return DEFAULT_USER
    return effective


def _lookup(table, name):
    try:
        return table(name)
    except (KeyError, TypeError):
        return None


def _memberships(pw, grnam, grgid, grouplist) -> set[str]:
    """Groupes du compte lus dans la base des utilisateurs, pas au processus.

    Une appartenance ajoutée par `usermod` ne figure pas dans les groupes du
    processus déjà lancé ; c'est pourtant l'état durable de l'hôte que le
    doctor doit dire. On relit donc `/etc/group` (via `os.getgrouplist`) ;
    `probe` compare ensuite cette appartenance aux groupes du processus
    courant pour dire qu'un redémarrage des exécuteurs reste nécessaire.
    """
    try:
        gids = set(grouplist(pw.pw_name, pw.pw_gid))
    except (KeyError, OSError):
        gids = {pw.pw_gid}
    names: set[str] = set()
    for gid in gids:
        try:
            names.add(grgid(gid).gr_name)
        except KeyError:
            continue
    return names


def _process_group_names(grgid, gids=None) -> set[str]:
    """Groupes effectifs du processus courant, pas ceux de la base des comptes.

    `os.getgroups()` ne rend que les groupes supplémentaires : le GID effectif
    est ajouté. Une appartenance accordée par `usermod` n'y apparaît qu'après
    le redémarrage du processus qui la porte (l'exécuteur, puis ses enfants).
    """
    if gids is None:
        try:
            gids = set(os.getgroups()) | {os.getgid()}
        except OSError:
            gids = set()
    names: set[str] = set()
    for gid in gids:
        try:
            names.add(grgid(gid).gr_name)
        except KeyError:
            continue
    return names


def probe(capabilities, *, user: str = DEFAULT_USER, device: str = KVM_DEVICE,
          pwnam=pwd.getpwnam, grnam=grp.getgrnam, grgid=grp.getgrgid,
          grouplist=os.getgrouplist, stat=os.stat,
          euid=None, process_groups=None) -> dict:
    """État réel d'un hôte pour ces capacités, sans écrire ni élargir.

    Rend `{declared, user, groups, missing_groups, user_missing,
    group_missing, process_missing, device, problems, warnings, ok}`.
    `problems` bloque la capacité (déclarée mais inutilisable) ; `warnings`
    signale une dérive sans bloquer l'hôte (groupe accordé sans déclaration,
    /dev/kvm 0666, groupe dans le compte mais pas encore dans le processus).
    """
    declared = list(dict.fromkeys(str(c).strip() for c in (capabilities or ())
                                  if str(c).strip()))
    groups = groups_of(declared)
    out = {
        "declared": declared,
        "user": user,
        "groups": groups,
        "missing_groups": [],
        "user_missing": False,
        "group_missing": [],
        "process_missing": [],
        "device": None,
        "problems": [],
        "warnings": [],
        "ok": True,
    }
    pw = _lookup(pwnam, user)
    out["user_missing"] = pw is None
    if pw is None and declared:
        out["problems"].append("user %s does not exist" % user)
    memberships = (_memberships(pw, grnam, grgid, grouplist)
                   if pw is not None else set())
    kvm = _lookup(grnam, "kvm")
    for group in groups:
        found = _lookup(grnam, group)
        if found is None:
            out["group_missing"].append(group)
            if declared:
                out["problems"].append("group %s does not exist" % group)
            continue
        if pw is not None and group not in memberships:
            out["missing_groups"].append(group)
            out["problems"].append("user %s is not in group %s" % (user, group))
    # Le compte a le groupe, mais le processus courant ne l'a pas encore :
    # il a été lancé avant l'application (ou en descend). Les exécuteurs ne
    # peuvent utiliser /dev/kvm qu'après un redémarrage ; on le dit dans la
    # ligne du doctor, sans rendre l'hôte rouge (l'état durable est correct).
    current_uid = os.geteuid() if euid is None else euid
    if (pw is not None and current_uid == pw.pw_uid
            and declared and memberships):
        current = (_process_group_names(grgid) if process_groups is None
                   else set(process_groups))
        out["process_missing"] = [group for group in groups
                                  if group in memberships
                                  and group not in current]
        if out["process_missing"]:
            out["warnings"].append(
                "group %s is granted to %s but not yet in this executor "
                "process: restart it (ameesh-runner-agent@<agent>.service) "
                "to use /dev/kvm" % (", ".join(out["process_missing"]), user))
    if not declared:
        # Dérive inverse : un groupe de capacité accordé sans déclaration.
        # Aucun KO : tant que la fiche n'a pas la capacité (#7927), AG3 doit
        # rester un hôte sain ; le signal suffit à faire corriger la fiche.
        for group in ALL_GROUPS:
            if pw is not None and group in memberships:
                out["warnings"].append(
                    "group %s is granted to %s but no capability is declared"
                    % (group, user))
    if ANDROID_EMULATOR in declared:
        try:
            info = stat(device)
        except OSError:
            out["problems"].append("%s is missing or unreadable" % device)
        else:
            device_block = {
                "path": device,
                "character": stat_mod.S_ISCHR(info.st_mode),
                "mode": stat_mod.S_IMODE(info.st_mode),
                "gid": info.st_gid,
            }
            out["device"] = device_block
            if not device_block["character"]:
                out["problems"].append("%s is not a character device" % device)
            else:
                if info.st_mode & 0o002:
                    out["warnings"].append(
                        "%s is world-writable (%04o): access is wider than the "
                        "kvm group" % (device, stat_mod.S_IMODE(info.st_mode)))
                if kvm is not None and info.st_gid != kvm.gr_gid:
                    try:
                        owner = grgid(info.st_gid).gr_name
                    except KeyError:
                        owner = str(info.st_gid)
                    out["warnings"].append(
                        "%s belongs to group %s, not kvm" % (device, owner))
    out["ok"] = not out["problems"]
    return out


def _one_line(exc) -> str:
    return " ".join(str(exc).split())[:160]


def doctor_lines(cfg, *, canons=None, host: str | None = None,
                 user: str | None = None, probe_fn=probe) -> tuple[bool, list[str]]:
    """`(KO, lignes)` de `ameesh doctor` pour la fiche Host de cet hôte.

    Sans fiche, ou sans capacité déclarée et sans dérive, rien n'est imprimé :
    le doctor ne parle pas pour un hôte qui ne déclare rien. Une capacité
    déclarée mais inutilisable est un KO ; une dérive (groupe accordé sans
    déclaration) reste un avertissement, pour qu'une fiche en retard ne rende
    pas l'hôte rouge.
    """
    name = (host or getattr(cfg, "host", "") or "").strip()
    if not name:
        return False, []
    if canons is None:
        try:
            canons = canon.load_configured(cfg)
        except Exception as exc:  # une lecture ne casse pas le doctor
            return False, ["capabilities: not checked (%s)" % _one_line(exc)]
    cards = [fiche for item in (canons or ()) for fiche in item.hosts
             if fiche.title == name]
    if not cards:
        return False, []
    declared = sorted({cap for fiche in cards for cap in (fiche.capabilities or ())})
    result = probe_fn(declared, user=user or user_name())
    if not declared:
        if result["warnings"]:
            return False, ["capabilities: warning — %s" % "; ".join(result["warnings"])]
        return False, []
    line = "capabilities: %s (%s)" % (
        ", ".join(declared), ", ".join("group " + group for group in result["groups"]))
    if result["problems"]:
        return True, ["capabilities: KO — %s declares %s but %s" % (
            name, ", ".join(declared), "; ".join(result["problems"]))]
    if result["warnings"]:
        line += " — warning: " + "; ".join(result["warnings"])
    return False, [line]


def apply(capabilities, *, user: str = DEFAULT_USER,
          run=subprocess.run, dry_run: bool = False,
          probe_fn=probe,
          pwnam=pwd.getpwnam, grnam=grp.getgrnam) -> tuple[int, list[str]]:
    """Accorde les groupes déclarés au compte, et rien d'autre.

    Idempotent : un groupe déjà porté par le compte n'est pas réécrit. Un
    `--dry-run` imprime la commande sans lire ni modifier l'état de l'hôte
    (l'installeur doit rester déterministe sur une machine neuve). Rend
    `(code, lignes)` : 2 quand la déclaration ne peut pas être appliquée.
    """
    groups = groups_of(capabilities)
    if not groups:
        return 0, []
    if dry_run:
        return 0, ["+ usermod -aG %s %s" % (group, user) for group in groups]
    result = probe_fn(capabilities, user=user)
    if result["user_missing"]:
        return 2, ["capabilities: KO — user %s does not exist; no group applied"
                   % user]
    if result["group_missing"]:
        return 2, ["capabilities: KO — group %s does not exist; no group applied"
                   % ", ".join(result["group_missing"])]
    lines = []
    for group in result["missing_groups"]:
        command = ["usermod", "-aG", group, user]
        try:
            run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            return 2, ["capabilities: KO — usermod -aG %s %s failed: %s"
                       % (group, user, _one_line(exc))]
        lines.append("applied: usermod -aG %s %s" % (group, user))
    if not lines:
        lines.append("capabilities: %s already in group %s"
                     % (user, ", ".join(groups)))
    return 0, lines


def hosts_with(item, capability: str) -> list[str]:
    """Hôtes du canon qui déclarent `capability`, dans l'ordre du canon."""
    return [host.title for host in item.hosts
            if capability in (host.capabilities or ())]


def _git(root: str, *args: str) -> subprocess.CompletedProcess | None:
    """git en lecture seule dans `root`, sans crochet ni état local.

    Le canon appartient au compte `ameesh`, l'installation tourne en root :
    `safe.directory` est déclaré pour ce chemin exact (jamais `*`), donné par
    l'appelant. Seul du plumbing en lecture est lancé, et `GIT_*` hérité est
    écarté pour qu'un environnement de tour ne change pas le dépôt lu.
    """
    root = os.path.abspath(root)
    command = [
        "git", "--no-pager", "--no-replace-objects",
        "-c", "core.hooksPath=%s" % os.devnull,
        "-c", "safe.directory=%s" % os.path.realpath(root),
        "-C", root,
    ]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    try:
        # Octets : la sortie d'un blob ne doit pas dépendre de la locale du
        # python3 système ; l'appelant décode en UTF-8 (les fiches le sont).
        return subprocess.run(command + list(args), capture_output=True,
                              env=env, check=False)
    except OSError:
        return None


def _stdout(proc: subprocess.CompletedProcess | None) -> str:
    """Sortie d'une commande git, décodée en UTF-8, ou "" si absente."""
    if proc is None or not proc.stdout:
        return ""
    return proc.stdout.decode("utf-8", "replace")


def reviewed_head(root: str, ref: str = "") -> tuple[str | None, str]:
    """`(commit, explication)` de la tête relue du canon.

    `ref` explicite (ou `AMEESH_CANON_REF`) est résolu tel quel ; sans lui,
    `origin/main` puis `origin/HEAD`. Jamais `HEAD` par défaut : un commit
    local non poussé, comme un arbre de travail modifié, ne doit pas accorder
    un droit d'hôte.
    """
    wanted = (ref or os.environ.get(CANON_REF_ENV) or "").strip()
    names = [wanted] if wanted else [DEFAULT_CANON_REF, "origin/HEAD"]
    for name in names:
        proc = _git(root, "rev-parse", "--verify", "--quiet", name + "^{commit}")
        commit = _stdout(proc).strip()
        if proc is not None and proc.returncode == 0 and commit:
            return commit, name
    return None, ("no readable reviewed ref (%s) in %s"
                  % (", ".join(names), root))


def _candidate_files(root: str):
    """Fiches `.md` sous `hotes/`, et rien ailleurs.

    Sans `hotes/`, aucun parcours : un canon peut contenir des worktrees
    entiers (`w7248/`) et une fiche homonyme n'y vaut pas déclaration.
    """
    hotes = os.path.join(root, HOTES)
    if not os.path.isdir(hotes):
        return
    for directory, dirnames, filenames in os.walk(hotes):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield os.path.join(directory, name)


def _candidate_git_files(root: str, commit: str) -> list[str]:
    """Chemins `.md` de `hotes/` à `commit`, jamais ceux de l'arbre de travail."""
    proc = _git(root, "ls-tree", "-r", "-z", "--name-only", commit, "--", HOTES)
    if proc is None or proc.returncode != 0:
        return []
    return sorted(path for path in _stdout(proc).split("\0")
                  if path.endswith(".md"))


def _card_data(text: str, host: str) -> dict | None:
    """Frontmatter d'une fiche `Host` de ce titre, ou None."""
    try:
        block = canon.split_frontmatter(text)
        if block is None:
            return None
        data = canon.load_yaml(block)
    except canon.YamlError:
        return None
    if (isinstance(data, dict)
            and str(data.get("type", "")).strip().lower() == "host"
            and str(data.get("title", "")).strip() == host):
        return data
    return None


def card(root: str, host: str, *, ref: str = "",
         untrusted: bool = False) -> tuple[dict | None, str | None]:
    """Frontmatter de la fiche `Host` de `host` dans `root`.

    Deux lectures, jamais l'arbre de travail par défaut :

    * `untrusted=False` (défaut) : les objets git de la tête relue
      (`reviewed_head`) ; un arbre modifié ou un commit local non poussé de
      `/home/ameesh/canon` ne peut donc pas s'accorder un groupe à la
      prochaine installation en root ;
    * `untrusted=True` : les fichiers de `root/hotes/`, pour un canon d'essai
      (même règle que `canon.load`).

    Dans les deux cas le parcours est borné à `hotes/` : un canon imbriqué ou
    un worktree (`w7248/`) n'est jamais lu.
    """
    if not host:
        return None, None
    if untrusted:
        for path in _candidate_files(root):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            data = _card_data(text, host)
            if data is not None:
                return data, path
        return None, None
    commit, _why = reviewed_head(root, ref)
    if commit is None:
        return None, None
    return _card_git(root, host, commit)


def _card_git(root: str, host: str, commit: str) -> tuple[dict | None, str | None]:
    """Fiche `host` lue dans les objets de `commit`, sous `hotes/` seulement."""
    for path in _candidate_git_files(root, commit):
        proc = _git(root, "cat-file", "blob", "%s:%s" % (commit, path))
        if proc is None or proc.returncode != 0:
            continue
        data = _card_data(_stdout(proc), host)
        if data is not None:
            return data, path
    return None, None


def card_for(cfg, host: str) -> tuple[dict | None, str | None]:
    """Fiche Host de `host` dans les canons configurés, à leur tête relue.

    Une tête relue illisible n'est pas « l'hôte n'est pas dans le canon » :
    elle lève `canon.CanonError`, que l'observation rend en fait nommé
    (`canon_unreadable`) plutôt qu'en absence de fiche.
    """
    unreadable: list[str] = []
    for entry in canon.configured(cfg):
        root = getattr(entry, "path", "")
        ref = getattr(entry, "ref", "") or ""
        untrusted = bool(getattr(entry, "untrusted", False))
        if untrusted:
            data, path = card(root, host, untrusted=True)
        else:
            commit, _why = reviewed_head(root, ref)
            if commit is None:
                unreadable.append(root)
                continue
            data, path = _card_git(root, host, commit)
        if data is not None:
            return data, path
    if unreadable:
        raise canon.CanonError("no readable reviewed head: %s"
                               % ", ".join(unreadable))
    return None, None


def _print_result(host: str, declared: list[str], result: dict) -> None:
    if result["problems"]:
        print("capabilities: KO — %s declares %s but %s"
              % (host, ", ".join(declared), "; ".join(result["problems"])))
        return
    print("capabilities: %s (%s)" % (
        ", ".join(declared), ", ".join("group " + group for group in result["groups"])))
    for warning in result["warnings"]:
        print("capabilities: warning — %s" % warning)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="host capabilities declared in a Host card: apply or check "
                    "their Unix groups (never chmod, ACL or udev)")
    parser.add_argument("--host", required=True, help="host title in the canon")
    parser.add_argument("--canon", default=os.environ.get("AMEESH_CANON")
                        or "/home/ameesh/canon",
                        help="canon checkout holding the Host card")
    parser.add_argument("--ref", default=os.environ.get(CANON_REF_ENV) or "",
                        help="reviewed revision to read (default: "
                             "AMEESH_CANON_REF, else origin/main)")
    parser.add_argument("--untrusted", action="store_true",
                        help="read the working files under hotes/ (test canons "
                             "only; never trusted by the installer)")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help="account holding the groups (default ameesh)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="print the commands, change nothing")
    mode.add_argument("--apply", action="store_true",
                      help="apply the declared groups (idempotent)")
    mode.add_argument("--check", action="store_true",
                      help="compare the declaration with the host state")
    args = parser.parse_args(argv)
    if not args.untrusted:
        commit, why = reviewed_head(args.canon, args.ref)
        if commit is None:
            # Ni l'arbre de travail ni un commit local ne remplacent la tête
            # relue : sans elle, l'installation n'accorde rien.
            print("capabilities: warning — %s; no Host card read, nothing "
                  "applied" % why)
            return 0
    data, path = card(args.canon, args.host, ref=args.ref,
                      untrusted=args.untrusted)
    if data is None:
        # En `--check`, l'absence de fiche n'est pas un fait à corriger : un
        # invité sans capacité n'a rien à vérifier et le check reste « ok ».
        if not args.check:
            print("capabilities: no Host card for %s in %s — nothing applied"
                  % (args.host, args.canon))
        return 0
    declared, problems, unknown = from_card(data)
    for problem in problems:
        print("capabilities: warning — %s" % problem)
    for capability in unknown:
        print("capabilities: warning — unknown capability %r ignored" % capability)
    if args.check:
        result = probe(declared, user=args.user)
        _print_result(args.host, declared, result)
        return 0 if result["ok"] and not problems else 1
    if args.apply or args.dry_run:
        code, lines = apply(declared, user=args.user, dry_run=args.dry_run)
        for line in lines:
            print(line)
        return code
    if not declared:
        print("capabilities: %s declares none (%s)" % (args.host, path))
        return 0
    print("capabilities: %s declares %s -> %s"
          % (args.host, ", ".join(declared), ", ".join(groups_of(declared))))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through install.sh
    sys.exit(main())
