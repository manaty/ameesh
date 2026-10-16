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
#: le filtre du journal, partagé par le `gh` généré (qui l'embarque) et par
#: ce module (`sanitize_argv`). Liste blanche sémantique : ne sont consignés
#: que la commande et la sous-commande connues, les noms d'options connues,
#: la valeur d'une option d'identifiant (`--repo`, `--match-head-commit`…) et,
#: pour les seules commandes dont les positions sont des identifiants (`pr`,
#: `run`, `api`…), leurs arguments de forme identifiant. Tout le reste devient
#: `[valeur]`, un jeton reconnaissable `[jeton]`. Dès que l'alignement des
#: options et de leurs valeurs n'est plus certain — option inconnue, option
#: courte à valeur collée, ou option à valeur suivie d'un argument qui
#: commence par un tiret — plus rien n'est consigné. Rien n'est tronqué.
SANITIZE = r"""
_JETON = re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#:@+-]{0,99}")
#: commandes et sous-commandes consignées en clair
_COMMANDES = {
    "pr": {"list", "view", "create", "merge", "close", "reopen", "edit", "checks",
           "comment", "review", "ready", "diff", "checkout", "status", "update-branch",
           "lock", "unlock"},
    "issue": {"list", "view", "create", "close", "reopen", "edit", "comment",
              "status", "lock", "unlock", "delete", "transfer", "pin", "unpin"},
    "run": {"list", "view", "cancel", "rerun", "watch", "delete", "download"},
    "workflow": {"list", "view", "run", "enable", "disable"},
    "release": {"list", "view", "create", "edit", "delete", "upload", "download"},
    "label": {"list", "create", "edit", "delete", "clone"},
    "repo": {"view", "list", "clone", "create", "edit", "fork", "sync",
             "set-default"},
    "cache": {"list", "delete"},
    "secret": {"list", "set", "delete"},
    "variable": {"list", "set", "get", "delete"},
    "search": {"code", "commits", "issues", "prs", "repos"},
    "auth": {"status", "token", "login", "logout", "setup-git", "refresh",
             "switch"},
    "api": None, "status": None, "browse": None,
}
#: commandes dont les arguments positionnels sont des identifiants
_POS_IDENT = frozenset(("pr", "issue", "run", "workflow", "release", "label",
                        "repo", "cache", "api"))
#: options d'identifiant : leur valeur est consignée si elle en a la forme,
#: pour les commandes de `_POS_IDENT` seulement
_SURES = frozenset((
    "--repo", "-R", "--branch", "--head", "--base", "-B", "--match-head-commit",
    "--label", "--add-label", "--remove-label", "--workflow",
    "--method", "-X", "--state", "--limit", "-L", "--status", "--event",
    "--user", "--commit", "--ref", "--job", "--attempt", "--author",
    "--assignee", "--milestone", "--hostname", "--org", "--env", "--name"))
#: options à valeur libre : leur valeur n'est jamais consignée
_LIBRES = frozenset((
    "--body", "-b", "--json", "--body-file", "-F", "--title", "-t", "--field", "-f",
    "--raw-field", "--header", "-H", "--input", "--jq", "-q", "--template",
    "--message", "-m", "--notes", "--notes-file", "-n", "--search", "-S",
    "--subject", "--env-file", "--app", "-a", "--reviewer", "-r",
    "--add-reviewer", "--remove-reviewer", "--add-assignee",
    "--remove-assignee", "--visibility", "--description", "-d", "--key",
    "--cache", "--target", "--tag", "--discussion-category", "--comment", "-c"))
#: options sans valeur **dans toutes les sous-commandes** de gh : l'argument
#: suivant n'est pas leur valeur. Une option booléenne ici, à valeur ailleurs
#: (`--comment`/`-c` : booléen de `pr review`, texte de `pr close`), n'y est
#: pas : traitée comme option à valeur, elle masque au pire un argument de
#: trop, jamais l'inverse. Vérifié contre l'aide de gh 2.100 (224 commandes) :
#: `-h`, `-i`, `-w` ont une valeur dans certaines sous-commandes, ils n'y sont pas.
_BOOLS = frozenset((
    "--auto", "--squash", "--merge", "--rebase", "--delete-branch",
    "--admin", "--disable-auto", "--draft", "--web", "--yes", "-y",
    "--force", "--paginate", "--silent", "--include", "--verbose",
    "--exit-status", "--failed", "--log", "--log-failed", "--watch", "--fill",
    "--fill-first", "--fill-verbose", "--help", "--no-maintainer-edit",
    "--approve", "--request-changes", "--slurp", "--prerelease",
    "--generate-notes", "--verify-tag", "--clobber", "--undo"))


def _valeur(v):
    if _JETON.search(v):
        return "[jeton]"
    return v if _IDENT.fullmatch(v) else "[valeur]"


def _positionnel(a):
    chemin, q, _ = a.partition("?")
    tete = "/" if chemin.startswith("/") else ""
    v = tete + _valeur(chemin[len(tete):])
    if v != chemin:
        return "[jeton]" if _JETON.search(chemin) else "[valeur]"
    return v + "?[valeur]" if q else v


def _option(a):
    # (nom, valeur collée ou None) ; nom None si l'option est inconnue
    if a.startswith("--"):
        nom, egal, v = a.partition("=")
        v = v if egal else None
    else:
        nom, v = a[:2], (a[2:] or None)
    if nom in _SURES or nom in _LIBRES or (nom in _BOOLS and v is None):
        return nom, v
    return None, v


def sanitize_argv(argv):
    # Les positionnels ne sont consignés qu'avant la première option : après,
    # une erreur d'arité (booléen pris pour option à valeur ou l'inverse) ne
    # peut que masquer davantage, jamais faire passer une valeur pour un
    # identifiant.
    out, mots, attente, option_vue = [], [], None, False

    def sure(nom):
        # une option d'identifiant ne l'est que pour les commandes connues
        # (`--user` est booléen de `secret set`, `--status` de `codespace`)
        return nom in _SURES and bool(mots) and mots[0] in _POS_IDENT \
            and mots[-1] != "[commande]"

    for i, a in enumerate(argv):
        if attente is not None:
            if a.startswith("-") and a != "-":
                # la « valeur » ressemble à une option : si gh tient l'option
                # pour un booléen (`pr review -a`), c'est elle qu'il lit comme
                # option (`--body`), et l'argument d'après est un corps libre
                # qu'on prendrait pour une option sûre. L'alignement est
                # perdu : plus rien n'est consigné.
                out.extend("[valeur]" for _ in argv[i:])
                break
            out.append(_valeur(a) if sure(attente) else "[valeur]")
            attente = None
            continue
        if a == "--":
            out.append("--")
            out.extend("[valeur]" for _ in argv[i + 1:])
            break
        if a.startswith("-") and a != "-":
            option_vue = True
            nom, v = _option(a)
            if nom is None or (v is not None and not a.startswith("--")):
                # inconnue, ou courte à valeur collée (`-ab` est aussi un
                # groupe de booléens pour gh, dont le dernier peut prendre
                # l'argument suivant) : booléen ou non, on ne sait pas, plus
                # rien n'est consigné
                # booléen ou non, on ne sait pas : plus rien n'est consigné
                out.append("[option]")
                out.extend("[valeur]" for _ in argv[i + 1:])
                break
            if v is not None:
                out.append(nom + "=" + (_valeur(v) if sure(nom) else "[valeur]"))
            else:
                out.append(nom)
                attente = None if nom in _BOOLS else nom
            continue
        if option_vue:
            out.append("[jeton]" if _JETON.search(a) else "[valeur]")
        elif not mots:
            mots.append(a if a in _COMMANDES else "[commande]")
            out.append(mots[-1])
        elif len(mots) == 1 and _COMMANDES.get(mots[0]):
            mots.append(a if a in _COMMANDES[mots[0]] else "[commande]")
            out.append(mots[-1])
        elif mots[0] in _POS_IDENT and mots[-1] != "[commande]":
            out.append(_positionnel(a))
        else:
            out.append("[valeur]")
    return out
"""
_ns: dict = {"re": re}
exec(SANITIZE, _ns)  # noqa: S102 — source constante de ce module
sanitize_argv = _ns["sanitize_argv"]

#: le `gh` généré. Volontairement autonome (bibliothèque standard seule) : il
#: tourne sous l'interpréteur de l'exécuteur, sans importer ameesh.
SHIM = r'''#!{python}
# ameesh #1645 : journal des gestes gh. Fichier généré par l'exécuteur, ne pas
# éditer ; il est réécrit à chaque tour.
import json, os, re, sys, time
{sanitize}
real = os.environ.get("{real}", "")
if not real or not os.access(real, os.X_OK):
    sys.stderr.write("ameesh: vrai gh introuvable ({real}=%r)\n" % real)
    sys.exit(127)
journal = os.environ.get("{journal}", "")
if journal:
    try:
        line = json.dumps({{
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent": os.environ.get("AGENT_MAIL_NAME", ""),
            "turn": os.environ.get("AMEESH_TURN_ID", ""),
            "lot": os.environ.get("AMEESH_LOT", ""),
            "role": os.environ.get("{role}", ""),
            "host": os.environ.get("AMEESH_HOST", ""),
            "pid": os.getpid(), "ppid": os.getppid(),
            "cwd": os.getcwd(), "argv": sanitize_argv(sys.argv[1:])}}, ensure_ascii=False)
        path = time.strftime(journal)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as exc:  # le journal ne bloque jamais la commande
        try:  # stderr peut être fermé : l'avertissement non plus
            sys.stderr.write("ameesh: journal gh non écrit (%s)\n" % exc)
            sys.stderr.flush()
        except Exception:
            pass
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


#: un nom de rôle sert de nom de dossier (`gh-config/<rôle>`)
ROLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def role_of(config: dict, agent: str) -> str:
    """Le rôle de l'agent : celui qui le nomme, sinon `default_role`. Un rôle
    mal formé (nom, type) est ignoré."""
    roles = config.get("roles")
    if not isinstance(roles, dict):
        return ""
    valides = {nom: role for nom, role in roles.items()
               if isinstance(nom, str) and ROLE_RE.fullmatch(nom)
               and isinstance(role, dict)}
    for nom, role in sorted(valides.items()):
        agents = role.get("agents")
        if isinstance(agents, list) and agent in agents:
            return nom
    defaut = config.get("default_role")
    return defaut if isinstance(defaut, str) and defaut in valides else ""


def _read_token(path: str) -> tuple[str, str]:
    """(jeton, problème). Le fichier doit être un fichier ordinaire et privé
    (aucun droit groupe ou autres) : un jeton lisible par d'autres comptes
    n'est pas utilisé, un FIFO ne bloque pas le tour."""
    path = os.path.expanduser(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return "", "%s : fichier ordinaire attendu" % path
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                return "", "%s : droits %o trop larges (0600 attendu)" % (
                    path, stat.S_IMODE(st.st_mode))
            with os.fdopen(os.dup(fd), encoding="utf-8") as fh:
                jeton = fh.read().strip()
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        return "", "%s illisible (%s)" % (path, exc)
    if not jeton or any(c.isspace() for c in jeton):
        return "", "%s : jeton vide ou mal formé" % path
    return jeton, ""


def _resolve(config_dir: str, agent: str) -> tuple[dict, str]:
    """(résolution sans secret, jeton). Le jeton n'est lu qu'ici, une fois :
    `identity: role` implique un jeton non vide."""
    config, probleme = load_config(config_dir)
    role = role_of(config, agent) if config else ""
    out = {"agent": agent, "role": role, "identity": "inherited",
           "token_file": "", "problem": probleme,
           "journal": config.get("journal", True) is not False}
    jeton = ""
    if role:
        fichier = config["roles"][role].get("token_file")
        if not isinstance(fichier, str) or not fichier:
            out["problem"] = "rôle %s sans token_file" % role
        else:
            out["token_file"] = fichier
            jeton, probleme = _read_token(fichier)
            if jeton:
                out["identity"] = "role"
            else:
                out["problem"] = probleme
    return out, jeton


def resolve(config_dir: str, agent: str) -> dict:
    """L'identité GitHub d'un agent, sans jamais rendre le jeton lui-même :
    {role, identity: role|inherited, token_file, problem, journal}."""
    return _resolve(config_dir, agent)[0]


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
                          sanitize=SANITIZE)
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


def _problem(info: dict, texte: str) -> None:
    info["problem"] = (info["problem"] + " ; " if info["problem"] else "") + texte


def apply(env: dict, agent: str, config_dir: str, state_dir: str,
          lot: str | None = None) -> dict:
    """Pose sur `env` (celui du tour) le journal et, s'il est configuré, le
    jeton du rôle. Rend la résolution (sans jeton), pour le journal de
    l'exécuteur. Jamais bloquant : chaque étape se prépare sur une copie et
    n'est publiée qu'entière ; un échec laisse l'environnement hérité."""
    info, jeton = _resolve(config_dir, agent)
    nouveau = dict(env)
    if lot:
        nouveau["AMEESH_LOT"] = str(lot)
    nouveau.pop(ENV_ROLE, None)
    if jeton:
        identite = dict(nouveau)
        try:
            # le hosts.yml de l'owner n'est plus lu : un dossier vide, par rôle
            vide = os.path.join(state_dir, "github", "gh-config", info["role"])
            os.makedirs(vide, mode=0o700, exist_ok=True)
        except OSError as exc:
            info["identity"] = "inherited"
            _problem(info, "identité du rôle non posée (%s)" % exc)
        else:
            identite["GH_TOKEN"] = jeton
            identite.pop("GITHUB_TOKEN", None)
            identite.pop("GH_ENTERPRISE_TOKEN", None)
            identite["GH_CONFIG_DIR"] = vide
            identite[ENV_ROLE] = info["role"]
            _git_credential(identite)
            nouveau = identite
    if info["journal"]:
        try:
            shim_dir = os.path.join(state_dir, "github", "bin")
            reel = real_gh(nouveau.get("PATH", os.defpath), shim_dir)
            if reel:
                write_shim(shim_dir)
                nouveau[ENV_REAL] = reel
                nouveau[ENV_JOURNAL] = journal_pattern(state_dir)
                nouveau["PATH"] = shim_dir + os.pathsep + nouveau.get("PATH", os.defpath)
            else:
                _problem(info, "gh absent du PATH : journal sans objet")
        except OSError as exc:
            _problem(info, "journal gh non installé (%s)" % exc)
    env.clear()
    env.update(nouveau)
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
