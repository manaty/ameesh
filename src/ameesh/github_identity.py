# SPDX-License-Identifier: AGPL-3.0-only
"""Identité GitHub d'un agent, et journal de ses gestes `gh` (#1645).

Constat du 15/10/2026 : la CI du paquet de 11 h a été annulée à 13:46 sans
auteur identifiable. Tous les tours héritaient de l'environnement de
l'exécuteur, donc du jeton de l'owner (`GH_TOKEN` de `~/.config/ameesh/env`,
sinon `~/.config/gh/hosts.yml`), et aucun geste `gh` lancé par un agent
n'était consigné. Ce module ferme les deux trous, de façon réversible :

1. **Journal** (actif par défaut) : le tour reçoit en tête de `PATH` un `gh`
   généré qui écrit une ligne JSON (agent, tour, lot, rôle, commande) dans
   `<state>/github/gestes-AAAA-MM.jsonl` **avant** d'exécuter le vrai `gh`
   (`execv` : code de sortie, signaux et entrées-sorties inchangés). Une
   écriture impossible n'empêche jamais la commande. `ameesh github gestes`
   répond à « qui a annulé ce run ? ». C'est une attribution, pas une
   contrainte : un appel du vrai `gh` par son chemin absolu, ou de l'API par
   `curl`, échappe au journal.
2. **Identité par rôle** (inerte sans configuration) : le fichier
   `<config>/github-identities.json` associe des agents à un rôle et un rôle
   à un fichier de jeton. Si le jeton du rôle est lisible, le tour porte ce
   jeton (`GH_TOKEN`), un `GH_CONFIG_DIR` vide (le `hosts.yml` de l'owner
   n'est plus lu) et un assistant d'identification git pour
   `https://github.com` ; `GITHUB_TOKEN` hérité est retiré. Sans fichier, ou
   sans jeton lisible, rien ne change : le tour garde l'identité héritée, et
   le journal le dit (`role` vide, `identity: inherited`).

Format de `github-identities.json` (aucun secret dedans, seulement des
chemins vers des fichiers 0600) ::

    {"journal": true,
     "default_role": "agent",
     "roles": {
       "agent":  {"token_file": "~/.config/ameesh/github/agent.token"},
       "pilote": {"token_file": "~/.config/ameesh/github/pilote.token",
                  "agents": ["mesh-design", "orchestrateur"]}}}

Les droits minimaux par rôle et ce que l'owner doit créer avant
d'activer sont dans `docs/design/etudes/github-identites.md`.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import shutil
import stat
import sys

CONFIG_NAME = "github-identities.json"
#: variables lues par le `gh` généré
ENV_REAL = "AMEESH_GH_REAL"
ENV_JOURNAL = "AMEESH_GH_JOURNAL"
ENV_ROLE = "AMEESH_GH_ROLE"
#: un argument plus long est tronqué dans le journal (corps de PR, requêtes)
ARG_MAX = 200
#: jetons GitHub reconnaissables, jamais écrits dans le journal
TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")

#: le `gh` généré. Volontairement autonome (bibliothèque standard seule) : il
#: tourne sous l'interpréteur de l'exécuteur, sans importer ameesh.
SHIM = r'''#!{python}
# ameesh #1645 : journal des gestes gh. Fichier généré par l'exécuteur, ne pas
# éditer ; il est réécrit à chaque tour.
import json, os, re, sys, time
real = os.environ.get("{real}", "")
if not real or not os.access(real, os.X_OK):
    sys.stderr.write("ameesh: vrai gh introuvable ({real}=%r)\n" % real)
    sys.exit(127)
journal = os.environ.get("{journal}", "")
if journal:
    try:
        tok = re.compile(r"{token_re}")
        args = [tok.sub("[jeton]", a if len(a) <= {arg_max} else a[:{arg_max}] + "…")
                for a in sys.argv[1:]]
        line = json.dumps({{
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent": os.environ.get("AGENT_MAIL_NAME", ""),
            "turn": os.environ.get("AMEESH_TURN_ID", ""),
            "lot": os.environ.get("AMEESH_LOT", ""),
            "role": os.environ.get("{role}", ""),
            "host": os.environ.get("AMEESH_HOST", ""),
            "pid": os.getpid(), "ppid": os.getppid(),
            "cwd": os.getcwd(), "argv": args}}, ensure_ascii=False)
        path = time.strftime(journal)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:  # le journal ne bloque jamais la commande
        sys.stderr.write("ameesh: journal gh non écrit (%s)\n" % exc)
os.execv(real, [real] + sys.argv[1:])
'''


def config_path(config_dir: str) -> str:
    return os.path.join(config_dir, CONFIG_NAME)


def load_config(config_dir: str) -> tuple[dict, str]:
    """(configuration, problème). Absente : ({}, ""). Illisible : ({}, raison),
    et l'appelant garde l'identité héritée."""
    path = config_path(config_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, "%s illisible (%s)" % (path, exc)
    if not isinstance(data, dict):
        return {}, "%s : objet JSON attendu" % path
    return data, ""


def role_of(config: dict, agent: str) -> str:
    """Le rôle de l'agent : celui qui le nomme, sinon `default_role`."""
    roles = config.get("roles") or {}
    for nom, role in sorted(roles.items()):
        if isinstance(role, dict) and agent in (role.get("agents") or ()):
            return nom
    defaut = config.get("default_role") or ""
    return defaut if defaut in roles else ""


def _read_token(path: str) -> tuple[str, str]:
    """(jeton, problème). Le fichier doit être privé (aucun droit groupe ou
    autres) : un jeton lisible par d'autres comptes n'est pas utilisé."""
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return "", "%s : droits %o trop larges (0600 attendu)" % (
                path, stat.S_IMODE(st.st_mode))
        with open(path, encoding="utf-8") as fh:
            jeton = fh.read().strip()
    except OSError as exc:
        return "", "%s illisible (%s)" % (path, exc)
    if not jeton or any(c.isspace() for c in jeton):
        return "", "%s : jeton vide ou mal formé" % path
    return jeton, ""


def resolve(config_dir: str, agent: str) -> dict:
    """L'identité GitHub d'un agent, sans jamais rendre le jeton lui-même :
    {role, identity: role|inherited, token_file, problem, journal}."""
    config, probleme = load_config(config_dir)
    role = role_of(config, agent) if config else ""
    out = {"agent": agent, "role": role, "identity": "inherited",
           "token_file": "", "problem": probleme,
           "journal": bool(config.get("journal", True))}
    if role:
        fichier = (config["roles"][role] or {}).get("token_file") or ""
        out["token_file"] = fichier
        if not fichier:
            out["problem"] = "rôle %s sans token_file" % role
        else:
            jeton, probleme = _read_token(fichier)
            if jeton:
                out["identity"] = "role"
            else:
                out["problem"] = probleme
    return out


def real_gh(path_env: str, shim_dir: str) -> str:
    """Le vrai `gh` du PATH, le dossier du `gh` généré exclu (pas de boucle)."""
    shim_dir = os.path.realpath(shim_dir)
    dossiers = [d for d in path_env.split(os.pathsep)
                if d and os.path.realpath(d) != shim_dir]
    return shutil.which("gh", path=os.pathsep.join(dossiers)) or ""


def write_shim(shim_dir: str, python: str | None = None) -> str:
    """Écrit le `gh` généré (idempotent, remplacement atomique)."""
    os.makedirs(shim_dir, mode=0o700, exist_ok=True)
    contenu = SHIM.format(python=python or sys.executable, real=ENV_REAL,
                          journal=ENV_JOURNAL, role=ENV_ROLE,
                          token_re=TOKEN_RE.pattern, arg_max=ARG_MAX)
    cible = os.path.join(shim_dir, "gh")
    try:
        with open(cible, encoding="utf-8") as fh:
            if fh.read() == contenu:
                return cible
    except OSError:
        pass
    tmp = "%s.%d.tmp" % (cible, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(contenu)
    os.chmod(tmp, 0o755)
    os.replace(tmp, cible)
    return cible


def journal_pattern(state_dir: str) -> str:
    """Motif `strftime` du journal : un fichier par mois."""
    return os.path.join(state_dir, "github", "gestes-%Y-%m.jsonl")


def apply(env: dict, agent: str, config_dir: str, state_dir: str,
          lot: str | None = None) -> dict:
    """Pose sur `env` (celui du tour) le journal et, s'il est configuré, le
    jeton du rôle. Rend la résolution (sans jeton), pour le journal de
    l'exécuteur. Jamais bloquant : une erreur laisse l'environnement hérité."""
    info = resolve(config_dir, agent)
    if lot:
        env["AMEESH_LOT"] = str(lot)
    if info["identity"] == "role":
        jeton, _ = _read_token(info["token_file"])
        env["GH_TOKEN"] = jeton
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_ENTERPRISE_TOKEN", None)
        # le hosts.yml de l'owner n'est plus lu : un dossier vide, par rôle
        vide = os.path.join(state_dir, "github", "gh-config", info["role"])
        os.makedirs(vide, mode=0o700, exist_ok=True)
        env["GH_CONFIG_DIR"] = vide
        env[ENV_ROLE] = info["role"]
        _git_credential(env)
    else:
        env.pop(ENV_ROLE, None)
    if info["journal"]:
        try:
            shim_dir = os.path.join(state_dir, "github", "bin")
            reel = real_gh(env.get("PATH", os.defpath), shim_dir)
            if reel:
                write_shim(shim_dir)
                env[ENV_REAL] = reel
                env[ENV_JOURNAL] = journal_pattern(state_dir)
                env["PATH"] = shim_dir + os.pathsep + env.get("PATH", os.defpath)
            else:
                info["problem"] = (info["problem"] + " ; " if info["problem"] else "") \
                    + "gh absent du PATH : journal sans objet"
        except OSError as exc:
            info["problem"] = (info["problem"] + " ; " if info["problem"] else "") \
                + "journal gh non installé (%s)" % exc
    return info


def _git_credential(env: dict) -> None:
    """git sur `https://github.com` répond avec le jeton du rôle. Une valeur
    vide d'abord vide la liste des assistants hérités (celui de l'owner,
    `gh auth git-credential`) pour cette URL seulement. Les remotes SSH ne
    sont pas concernés (voir l'étude)."""
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        n = 0
    cle = "credential.https://github.com.helper"
    for valeur in ("", '!f() { echo username=x-access-token; '
                       'echo "password=$GH_TOKEN"; }; f'):
        env["GIT_CONFIG_KEY_%d" % n] = cle
        env["GIT_CONFIG_VALUE_%d" % n] = valeur
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)


# ----------------------------------------------------------------------------
# lecture du journal

def _since(texte: str) -> datetime.timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhd])", texte.strip())
    if not m:
        raise argparse.ArgumentTypeError("durée attendue : 30m, 24h, 7d")
    n, u = int(m.group(1)), m.group(2)
    unite = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[u]
    return datetime.timedelta(**{unite: n})


def read_journal(state_dir: str, since: datetime.timedelta | None = None,
                 agent: str = "", match: str = "",
                 now: datetime.datetime | None = None) -> list[dict]:
    """Les gestes consignés, du plus ancien au plus récent. `match` : sous-chaîne
    de la commande (`run cancel`, `pr merge`, un numéro de run)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    limite = now - since if since else None
    motif = journal_pattern(state_dir).replace("%Y-%m", "*")
    out = []
    for chemin in sorted(glob.glob(motif)):
        try:
            with open(chemin, encoding="utf-8") as fh:
                lignes = fh.readlines()
        except OSError:
            continue
        for ligne in lignes:
            try:
                geste = json.loads(ligne)
            except ValueError:
                continue  # ligne coupée
            if agent and geste.get("agent") != agent:
                continue
            if match and match not in " ".join(geste.get("argv") or ()):
                continue
            if limite:
                try:
                    quand = datetime.datetime.strptime(geste.get("ts", ""),
                                                       "%Y-%m-%dT%H:%M:%S%z")
                except ValueError:
                    continue
                if quand < limite:
                    continue
            out.append(geste)
    out.sort(key=lambda g: g.get("ts", ""))
    return out


def main(argv: list[str] | None = None) -> int:
    from .config import load as load_config_mesh
    p = argparse.ArgumentParser(prog="ameesh github", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gestes", help="gestes gh consignés (qui a annulé, fusionné, étiqueté)")
    g.add_argument("--since", type=_since, default=_since("24h"))
    g.add_argument("--agent", default="")
    g.add_argument("--match", default="", help="sous-chaîne de la commande, ex. 'run cancel'")
    g.add_argument("--json", action="store_true")
    i = sub.add_parser("identity", help="identité GitHub résolue d'un agent (jamais le jeton)")
    i.add_argument("agent")
    i.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config_mesh()
    if args.cmd == "identity":
        info = resolve(cfg.config_dir, args.agent)
        if args.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            print("%s : identité %s%s%s" % (
                info["agent"], info["identity"],
                " (rôle %s)" % info["role"] if info["role"] else "",
                " — %s" % info["problem"] if info["problem"] else ""))
        return 0
    gestes = read_journal(cfg.state_dir, args.since, args.agent, args.match)
    if args.json:
        for geste in gestes:
            print(json.dumps(geste, ensure_ascii=False))
        return 0
    for geste in gestes:
        print("%s %-16s lot %-6s %-8s gh %s" % (
            geste.get("ts", "?"), geste.get("agent") or "?", geste.get("lot") or "—",
            geste.get("role") or "hérité", " ".join(geste.get("argv") or ())))
    if not gestes:
        print("aucun geste gh consigné sur la période")
    return 0
