# SPDX-License-Identifier: AGPL-3.0-only
"""L1651 (#1650) — les parts qu'un change set gelé rend dues.

La porte de file lit la CI du commit gelé (`radar.commit_ci`) : chaque run,
check et statut que GitHub rend pour ce SHA exact doit être terminé et
passant. Une part que le change set rend **due mais qui n'a jamais été
dispatchée** ne laisse aucun statut : un ensemble non vide de statuts verts se
lit vert, et la part manquante est invisible. C'est le trou mesuré sur #1470
(tête gelée antérieure à #1368, sans job `lot-plan`) : cinq parts dues, une
seule annoncée, `radar.commit_ci` vert (#1650, constat première main
mty-nxl-impl-01 du 15/10/2026).

Ce module ferme ce trou : les parts dues ne se déduisent plus des statuts
présents, elles sont **recalculées** par le sélecteur du dépôt, lu à une
référence **versionnée** (`ci_parts.rev`, la cible par défaut) résolue en
identifiant de commit, et alimenté du change set de la tête gelée
(merge-base avec cette référence..tête). Une branche ne peut donc pas
supprimer une part en éditant ses propres workflows, et une tête ancienne qui
ne porte pas le job de plan est jugée contre le contrat que sa cible porte
aujourd'hui — le contrat n'est jamais déduit des seuls statuts présents.

Contrat (`radar.projects.<nom>.ci_parts`, configuration de l'hôte) :

    "ci_parts": {
      "selector": "scripts/ci/lot-plan.mjs",   # relatif au dépôt, lu à `rev`
      "rev": "develop",                        # défaut : la cible du projet
      "argv": ["node", "{selector}", "--files", "{files}", "--out", "{out}"],
      "schema": "nexlink-lot-plan/1",          # facultatif, vérifié
      "due": "due"                             # champ JSON de la liste due
    }

`{selector}`, `{root}` (l'arbre extrait), `{files}` (les noms changés,
séparés par NUL, tels que `git diff --name-only -z` les écrit) et `{out}` (le
plan JSON) sont substitués dans `argv` ; la commande tourne dans l'arbre
extrait, **jamais** par un shell. Le plan doit porter un `schema` non vide et
le champ `due` : une liste de noms de parts non vides, sans doublon.

Rien n'est coché ni écrit dans le dépôt du projet : l'arbre du commit épinglé
est extrait (`git archive`) dans un répertoire temporaire, la commande y
tourne, le répertoire est supprimé. Toute incertitude — configuration
illisible, sélecteur absent de l'arbre épinglé, erreur de git, commande qui
échoue ou dépasse son délai, plan absent, illisible, d'un autre schéma ou
mal formé — lève `PartsError` : un contrat inconnu n'est pas un vert.

N2 de #1666 : la preuve rendue par `due` est mise en cache (facultatif,
`cache_dir(cfg)`) par couple (commit de base, tête gelée) et par contrat ;
l'arbre de la cible n'est extrait qu'une fois par couple, plus à chaque
contrôle d'atterrissage, et une entrée relue est revalidée avant d'être
servie. N4 de #1666 : le sélecteur tourne dans sa propre session et son
groupe de processus entier est tué quand il dépasse `PARTS_TIMEOUT`.

N3 de #1666 : le sélecteur est du code de la cible, relu mais jamais de
confiance : il tourne dans un bac à sable `bwrap` (voie (a) du pilote,
mesh-design, 16/10/2026) — espaces de noms séparés, **réseau coupé**,
environnement vidé, racine vide où seuls le runtime nécessaire, l'arbre de la
cible (lecture seule), les entrées (lecture seule) et deux répertoires de
travail et de sortie (inscriptibles, propres à la preuve) sont montés : ni
`/`, ni `/home`, ni jeton, ni DSN de l'hôte. **Aucun repli** : sans `bwrap`,
ou si le bac à sable ne démarre pas (espaces de noms refusés par le noyau),
la preuve est refusée par `PartsError` — un contrat inconnu n'est pas un vert.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time

from . import plan_git
from .canon import GIT_TIMEOUT, GitError, _git, _git_env

#: plafond d'exécution du sélecteur : un sélecteur bloqué ne retient pas la
#: passe du radar des heures (canon §3, plafonds de la CI)
PARTS_TIMEOUT = 300
#: N4 de #1666 : délai de moisson après la mort du groupe — un petit-fils
#: échappé de la session retiendrait les tubes plus longtemps sans effet utile
PARTS_REAP_TIMEOUT = 10
#: plafond du plan écrit par le sélecteur, lu par l'hôte (`_plan_file`)
PLAN_MAX_BYTES = 1 << 20
#: N2 de #1666 : durée de vie (s) d'une preuve mise en cache. La preuve est
#: une fonction pure du couple (commit de base, tête gelée) et du contrat ;
#: au-delà du délai elle est élaguée et recalculée, sans effet sur la porte.
PARTS_CACHE_TTL = 24 * 3600
#: version du format de cache : une entrée d'une autre version est ignorée
PARTS_CACHE_VERSION = 1
#: N3 de #1666 : répertoires du runtime montés en lecture seule dans le bac à
#: sable — de quoi lancer l'interpréteur du sélecteur, rien de l'hôte : ni `/`,
#: ni `/home`, ni les jetons ou DSN de la session qui exécute la preuve
SANDBOX_RUNTIME = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
#: N3 de #1666 : configuration du lieur dynamique, montée en lecture seule
#: quand elle existe (fichiers étroits) : sans elle, un binaire lié
#: dynamiquement peut refuser de démarrer dans la racine vide du bac à sable
SANDBOX_LINKER = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d")
#: N3 de #1666 : délai du témoin de démarrage du bac à sable — un bwrap
#: bloqué ne retient pas la passe du radar
PARTS_PROBE_TIMEOUT = 20
#: les espaces de noms substituables dans `argv` : `radar.parse_ci_parts` les
#: refuse à la déclaration, `due` les substitue
PLACEHOLDERS = ("selector", "root", "files", "out")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
#: un identifiant de commit (SHA-1 ou SHA-256), pour valider une entrée de cache
_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class PartsError(RuntimeError):
    """Les parts dues d'une tête gelée ne peuvent pas être prouvées : message
    actionnable, jamais un vert."""


def _fail(what: str, proc) -> str:
    return "%s: %s" % (what, (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:])


def change_set(repo: str, base: str, head: str) -> bytes:
    """Les noms changés de `head` sur sa base versionnée `base`, séparés par
    NUL (`git diff --no-renames --name-only -z`), la seule lecture sans perte
    d'un nom (B1 de la R1 #1368, auditeur)."""
    proc = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, head)
    if proc.returncode != 0:
        raise PartsError(_fail("cannot read the change set %s..%s" % (base[:12], head[:12]),
                               proc))
    return proc.stdout


def merge_base(repo: str, base: str, head: str) -> str:
    """Le merge-base de la tête gelée et de la référence versionnée : base du
    change set, comme le job de plan (`origin/develop`)."""
    proc = _git(repo, "merge-base", base, head, timeout=GIT_TIMEOUT)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise PartsError(_fail("no merge base between %s and the frozen head %s"
                               % (base[:12], head[:12]), proc))
    return proc.stdout.decode("ascii", "replace").strip()


def _member_path(dest: str, name: str) -> str:
    """Le chemin absolu d'une entrée d'archive sous `dest`, ou `PartsError` :
    aucun nom absolu, aucun `..`, rien hors de l'arbre extrait."""
    clean = name.rstrip("/")
    if not clean or clean.startswith("/") or os.path.isabs(clean) \
            or any(part in ("", ".", "..") for part in clean.split("/")):
        raise PartsError("archive entry escapes the extracted tree: %r" % name)
    path = os.path.realpath(os.path.join(dest, *clean.split("/")))
    if path != dest and not path.startswith(dest + os.sep):
        raise PartsError("archive entry escapes the extracted tree: %r" % name)
    return path


def extract(repo: str, commit: str, dest: str) -> None:
    """L'arbre de `commit`, extrait dans `dest` depuis les objets Git
    (`git archive`), sans toucher au dépôt ni à un checkout. Seuls les
    fichiers, répertoires et liens symboliques internes entrent ; une entrée
    d'un autre type, ou un lien qui sort de l'arbre, refuse."""
    if not os.path.isdir(dest):
        os.makedirs(dest)
    argv = ["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
            "-c", "core.fsmonitor=false", "-c", "core.hooksPath=%s" % os.devnull,
            "archive", "--format=tar", commit]
    try:
        proc = subprocess.Popen(argv, cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_git_env())
    except OSError as exc:
        raise PartsError("git archive %s: %s" % (commit[:12], exc)) from exc
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                path = _member_path(dest, member.name)
                if member.isdir():
                    os.makedirs(path, exist_ok=True)
                    continue
                if member.issym():
                    target = os.path.realpath(os.path.join(os.path.dirname(path),
                                                           member.linkname))
                    if not target.startswith(dest + os.sep):
                        raise PartsError("archive entry escapes the extracted tree: %r"
                                         % member.name)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    if os.path.lexists(path):
                        os.remove(path)
                    os.symlink(member.linkname, path)
                    continue
                if not member.isfile():
                    raise PartsError("unsupported archive entry %r (only files, directories "
                                     "and internal symlinks)" % member.name)
                source = tar.extractfile(member)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(path, member.mode & 0o777)
        # le lecteur d'archive s'arrête à la fin d'archive : vider le reste du
        # flux avant de fermer, sinon git peut mourir sur SIGPIPE et un
        # archivage pourtant complet se lit comme un échec (témoin instable)
        proc.stdout.read()
    except (tarfile.TarError, OSError) as exc:
        raise PartsError("git archive %s: unreadable archive (%s)" % (commit[:12], exc)) from exc
    finally:
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        code = proc.wait()
    if code != 0:
        raise PartsError("git archive %s: %s" % (commit[:12],
                                                 err.decode("utf-8", "replace").strip()[-300:]))


def selector_env(root: str, home: str) -> dict:
    """L'environnement du sélecteur : minimal, jamais celui de l'hôte.

    Le sélecteur vient de l'arbre du dépôt (commit épinglé de la cible), pas
    de la configuration de l'hôte : il ne voit ni les jetons, ni les DSN, ni
    les variables d'outillage de la session qui l'exécute. `PATH` (lancer
    `node`), `HOME` et `TMPDIR` (un répertoire temporaire supprimé avec
    l'arbre) et `GITHUB_WORKSPACE` (la racine extraite, comme le job de plan)
    suffisent au contrat."""
    return {"PATH": os.environ.get("PATH") or os.defpath, "HOME": home,
            "TMPDIR": home, "GITHUB_WORKSPACE": root, "LC_ALL": "C"}


def sandbox_binary() -> str | None:
    """N3 de #1666 : le binaire d'isolation de l'hôte (`bwrap`), ou None.

    Aucun repli n'existe : sans ce binaire, `due` refuse de prouver (le
    sélecteur est du code de la cible, jamais exécuté sur l'hôte)."""
    return shutil.which("bwrap")


def sandbox_prefix(bwrap: str) -> list:
    """N3 de #1666 : les options d'isolation du bac à sable — espaces de noms
    séparés (`--unshare-all`, dont le réseau), environnement vidé (`--clearenv`,
    seuls les `--setenv` du contrat entrent), racine vide (`bwrap` n'expose que
    ce qui est monté), `/proc`, `/dev` et `/tmp` neufs."""
    return [bwrap, "--die-with-parent", "--unshare-all", "--unshare-net",
            "--clearenv", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]


def _readonly_binds(paths) -> list:
    """Les montages en lecture seule des chemins existants : `--ro-bind`
    strict (un chemin déclaré mais absent fait échouer le bac à sable, donc
    refuser la preuve, jamais l'exécuter à moitié isolé)."""
    argv = []
    for path in paths:
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    return argv


def sandbox_argv(bwrap: str, argv, *, root: str, files: str, writable, env: dict) -> list:
    """N3 de #1666 : la commande du sélecteur, enveloppée dans le bac à sable.

    Le binaire du sélecteur est résolu **avant** l'entrée (l'environnement du
    bac à sable est vide). Hors du runtime système, seuls son fichier et les
    répertoires `lib`/`lib64` de son préfixe sont montés en lecture seule :
    monter le préfixe entier exposerait le HOME pour un binaire de `~/bin`.
    L'arbre de la cible et les entrées sont montés en lecture
    seule ; les seuls chemins inscriptibles sont les répertoires de travail et
    de sortie, propres à cette preuve. L'environnement du bac à sable est vidé
    puis reconstruit avec **toutes** les variables de `selector_env` — aucune
    liste figée, pour que la composition avec le correctif #1938
    (`LD_LIBRARY_PATH` d'un runtime relocalisé) ne perde rien. Rien de l'hôte
    n'est monté en entier : pas de `/`, pas de `/home`."""
    program = shutil.which(argv[0], path=env.get("PATH"))
    if program is None:
        raise PartsError("the selector %s cannot start (%s is not in PATH)"
                         % (argv[0], argv[0]))
    real = os.path.realpath(program)
    bound = list(SANDBOX_RUNTIME)
    extra = os.path.dirname(real) or "/"
    if os.path.basename(extra) == "bin":
        extra = os.path.dirname(extra) or "/"
    if extra == "/":
        raise PartsError("the selector %s is installed at the host root: it cannot be "
                         "isolated" % argv[0])
    if not any(extra == path or extra.startswith(path.rstrip("/") + "/")
               for path in SANDBOX_RUNTIME):
        bound.append(real)
        bound.extend(os.path.join(extra, name) for name in ("lib", "lib64"))
    cmd = sandbox_prefix(bwrap)
    cmd += _readonly_binds(bound + list(SANDBOX_LINKER))
    cmd += ["--ro-bind", root, root, "--ro-bind", files, files]
    for path in writable:
        cmd += ["--bind", path, path]
    cmd += ["--chdir", root]
    for key, value in env.items():
        cmd += ["--setenv", key, value]
    return cmd + ["--", real] + list(argv[1:])


def sandbox_ready(bwrap: str) -> str | None:
    """N3 de #1666 : None si le bac à sable démarre réellement sur cet hôte,
    sinon la raison du refus (message actionnable).

    Un `bwrap` installé mais dont le noyau refuse les espaces de noms (ou un
    montage) n'isole rien : le témoin démarre `true` dans le bac à sable, avec
    le runtime réel, avant toute extraction."""
    probe = shutil.which("true")
    if probe is None:
        # pas de témoin disponible : le bac à sable se prononcera sur le
        # sélecteur lui-même (un échec de bwrap reste un refus, jamais un vert)
        return None
    argv = sandbox_prefix(bwrap) + _readonly_binds(
        list(SANDBOX_RUNTIME) + list(SANDBOX_LINKER))
    argv += ["--chdir", "/", os.path.realpath(probe)]
    proc = None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={"PATH": os.defpath, "LC_ALL": "C"},
                                start_new_session=True)
        _out, err = proc.communicate(timeout=PARTS_PROBE_TIMEOUT)
    except OSError as exc:
        return "the sandbox %s cannot start (%s)" % (bwrap, exc)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return "the sandbox %s did not answer in %d s" % (bwrap, PARTS_PROBE_TIMEOUT)
    if proc.returncode != 0:
        reason = (err or b"").decode("utf-8", "replace").strip()[-200:]
        return "the sandbox %s cannot isolate this host (exit %d): %s" % (
            bwrap, proc.returncode, reason)
    return None


def _plan_file(path: str, plan) -> tuple:
    """Le champ `due` du plan écrit à `path`, vérifié (schéma, liste de noms
    non vides, sans doublon).

    `path` est dans un répertoire inscriptible du bac à sable : le sélecteur
    (code de la cible) choisit ce qui s'y trouve, mais c'est l'hôte qui le lit.
    Seul un fichier ordinaire est lu, sans suivre de lien (un lien vers un
    fichier de l'hôte serait lu hors du bac à sable), sans bloquer (une FIFO
    figerait l'admission) et dans la limite de `PLAN_MAX_BYTES`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise PartsError("the selector wrote no readable plan at %s: %s" % (path, exc)) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PartsError("the selector's plan at %s is not a regular file" % path)
        if info.st_size > PLAN_MAX_BYTES:
            raise PartsError("the selector's plan at %s exceeds %d bytes" % (path, PLAN_MAX_BYTES))
        with os.fdopen(fd, "rb") as handle:
            fd = None
            raw = handle.read(PLAN_MAX_BYTES + 1)
        if len(raw) > PLAN_MAX_BYTES:
            raise PartsError("the selector's plan at %s exceeds %d bytes" % (path, PLAN_MAX_BYTES))
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise PartsError("the selector wrote no readable plan at %s: %s" % (path, exc)) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(data, dict):
        raise PartsError("the selector's plan is not a JSON object")
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema.strip():
        raise PartsError("the selector's plan declares no schema: it cannot be a proof")
    if plan.schema and schema != plan.schema:
        raise PartsError("the selector's plan schema is %r, expected %r" % (schema, plan.schema))
    names = data.get(plan.due)
    if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names):
        raise PartsError("the selector's plan field %r is not a list of part names" % plan.due)
    clean = [name.strip() for name in names]
    if len(set(clean)) != len(clean):
        raise PartsError("the selector's plan field %r repeats a part" % plan.due)
    return schema, tuple(clean)


def cache_dir(cfg) -> str | None:
    """Le répertoire du cache de preuves d'un hôte (`<état>/radar/ci-parts`),
    ou None sans état : l'admission et les atterrissages du même hôte
    partagent ainsi la même preuve (N2 de #1666)."""
    state = getattr(cfg, "state_dir", None)
    return os.path.join(state, "radar", "ci-parts") if state else None


def _cache_key(plan, base: str, frozen: str) -> str:
    """La clé d'une preuve : le couple (commit de base, tête gelée) et le
    contrat qui la produit. Deux contrats différents ne partagent jamais une
    entrée."""
    payload = json.dumps({"version": PARTS_CACHE_VERSION, "base": base, "frozen": frozen,
                          "selector": plan.selector, "argv": list(plan.argv),
                          "schema": plan.schema, "due": plan.due},
                         sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_read(cache: str, key: str, plan, base: str, frozen: str) -> dict | None:
    """La preuve mise en cache, relue et **revalidée** comme un plan frais ;
    toute entrée illisible, incomplète, d'un autre schéma, d'un autre format
    ou d'un autre couple (base, tête gelée) est ignorée — la preuve est
    recalculée, jamais servie."""
    try:
        with open(os.path.join(cache, key + ".json"), "r", encoding="utf-8") as handle:
            if time.time() - os.fstat(handle.fileno()).st_mtime > PARTS_CACHE_TTL:
                return None
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != PARTS_CACHE_VERSION:
        return None
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema.strip():
        return None
    if plan.schema and schema != plan.schema:
        return None
    names = data.get("due")
    if not isinstance(names, list) or not all(
            isinstance(name, str) and name.strip() for name in names):
        return None
    clean = tuple(name.strip() for name in names)
    if len(set(clean)) != len(clean):
        return None
    if data.get("base") != base or data.get("frozen") != frozen:
        return None
    if not isinstance(data.get("merge_base"), str) \
            or not _COMMIT_RE.fullmatch(data["merge_base"]):
        return None
    return {"schema": schema, "due": clean, "selector": plan.selector,
            "base": base, "merge_base": data["merge_base"], "frozen": frozen}


def _cache_write(cache: str, key: str, proof: dict) -> None:
    """Écrit la preuve en cache, atomiquement ; un échec d'écriture ne fait
    jamais échouer la preuve — le cache est un coût, pas une garantie."""
    path = os.path.join(cache, key + ".json")
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        os.makedirs(cache, exist_ok=True)
        data = {"version": PARTS_CACHE_VERSION, "schema": proof["schema"],
                "due": list(proof["due"]), "base": proof["base"],
                "merge_base": proof["merge_base"], "frozen": proof["frozen"]}
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _cache_prune(cache: str, now: float) -> None:
    """Élague les preuves plus vieilles que `PARTS_CACHE_TTL` : le cache ne
    grossit pas sans borne (N2 de #1666)."""
    try:
        entries = list(os.scandir(cache))
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            if now - entry.stat().st_mtime > PARTS_CACHE_TTL:
                os.remove(entry.path)
        except OSError:
            continue


def _kill_group(proc) -> None:
    """N4 de #1666 : `subprocess.run(timeout=…)` ne tue que l'enfant direct ;
    les petits-enfants du sélecteur (node, sh…) lui survivraient. Le sélecteur
    tourne dans sa propre session (`start_new_session=True`) : on tue le
    groupe entier, sans quoi un sélecteur bloqué laisse des processus
    derrière lui."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def due(project, sha: str, base_sha: str, repo: str, *, cache: str | None = None) -> dict:
    """Les parts que le change set de la tête gelée `sha` rend dues, calculées
    par le sélecteur versionné de `project` (`ci_parts`), lu au commit
    `base_sha` de `repo` et alimenté du diff `merge-base(base_sha, sha)..sha`.

    Rend `{schema, due, selector, base, merge_base, frozen}` ; lève
    `PartsError` dès que la preuve manque (sélecteur absent de l'arbre
    épinglé, change set inconnu, commande en échec, plan mal formé).

    N2 de #1666 : `cache` (un répertoire, `cache_dir(cfg)`) garde la preuve
    par couple (base, tête gelée) et par contrat. L'arbre entier de la cible
    (~71 Mo pour Nexlink) n'est plus extrait aux trois contrôles
    d'atterrissage : une preuve en cache est relue et revalidée, jamais
    recalculée pour un autre couple. Sans `cache`, le calcul reste intact.

    N3 de #1666 : le sélecteur ne tourne que dans le bac à sable `bwrap`
    (réseau coupé, racine vide, arbre et entrées en lecture seule). Il n'y a
    **aucun repli** : sans `bwrap` sur l'hôte, ou si le bac à sable ne démarre
    pas, `PartsError` — la preuve est inconnue, jamais un vert."""
    plan = getattr(project, "ci_parts", None)
    if plan is None:
        raise PartsError("project %s declares no ci_parts contract" % project.name)
    if plan.selector.startswith("/") or any(part in ("", ".", "..")
                                            for part in plan.selector.split("/")):
        # la déclaration est validée à la lecture de la configuration ; le
        # contrat construit à la main ne contourne pas la règle (défense en
        # profondeur : rien n'est lu hors de l'arbre extrait)
        raise PartsError("the selection source %r is not a path inside the repository"
                         % plan.selector)
    try:
        frozen = plan_git._commit(repo, sha)
        base = plan_git._commit(repo, base_sha)
    except GitError as exc:
        raise PartsError("cannot read %s in %s: %s" % ((sha or "?")[:12], repo, exc)) from exc
    if not frozen:
        raise PartsError("the frozen commit %s is not in %s" % ((sha or "?")[:12], repo))
    if not base:
        raise PartsError("the versioned selection source %r is not a commit of %s: the due "
                         "parts cannot be proven" % (base_sha, repo))
    #: N2 de #1666 : la preuve est une fonction pure du couple (base, tête
    #: gelée) et du contrat ; l'extraction de l'arbre (~71 Mo pour Nexlink)
    #: n'est refaite que sur un défaut de cache, jamais aux contrôles
    #: répétés d'un même couple. Entrée revalidée : jamais servie telle quelle.
    key = _cache_key(plan, base, frozen) if cache else None
    if cache:
        _cache_prune(cache, time.time())
        hit = _cache_read(cache, key, plan, base, frozen)
        if hit is not None:
            return hit
    #: N3 de #1666 : le sélecteur est du code de la cible ; il ne tourne que
    #: dans le bac à sable. Sans bwrap, ou si le bac à sable ne démarre pas
    #: (espaces de noms refusés par le noyau), la preuve est refusée avant
    #: toute extraction — aucun repli vers l'exécution sur l'hôte.
    bwrap = sandbox_binary()
    if bwrap is None:
        raise PartsError("the selector %s has no sandbox on this host (bwrap is not "
                         "installed): the due parts cannot be proven without isolation "
                         "(N3 of #1666)" % plan.selector)
    why = sandbox_ready(bwrap)
    if why is not None:
        raise PartsError("the selector %s cannot be isolated: %s (N3 of #1666)"
                         % (plan.selector, why))
    try:
        parent = merge_base(repo, base, frozen)
        changed = change_set(repo, parent, frozen)
    except GitError as exc:
        raise PartsError("cannot read the change set of %s in %s: %s"
                         % (frozen[:12], repo, exc)) from exc
    tmp = tempfile.mkdtemp(prefix="ameesh-ci-parts-")
    try:
        root = os.path.join(tmp, "tree")
        extract(repo, base, root)
        selector = os.path.join(root, *plan.selector.split("/"))
        if not os.path.isfile(selector):
            raise PartsError("the selection source %s is not in the tree of %s (%s): the due "
                             "parts cannot be proven" % (plan.selector, base_sha[:12], base[:12]))
        files = os.path.join(tmp, "changed.nul")
        with open(files, "wb") as out:
            out.write(changed)
        #: N3 de #1666 : seuls ces deux répertoires sont inscriptibles dans le
        #: bac à sable ; l'arbre et `changed.nul` y sont montés en lecture seule
        work = os.path.join(tmp, "work")
        outdir = os.path.join(tmp, "out")
        os.makedirs(work)
        os.makedirs(outdir)
        result = os.path.join(outdir, "plan.json")
        values = {"selector": selector, "root": root, "files": files, "out": result}
        try:
            argv = [part.format(**values) for part in plan.argv]
        except (KeyError, IndexError, ValueError) as exc:
            raise PartsError("the ci_parts command uses an unknown placeholder: %s" % exc) from exc
        env = selector_env(root, work)
        argv = sandbox_argv(bwrap, argv, root=root, files=files,
                            writable=(work, outdir), env=env)
        try:
            proc = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env,
                                    start_new_session=True)
        except OSError as exc:
            raise PartsError("the selector %s cannot start in its sandbox (%s: %s)"
                             % (plan.selector, argv[0], exc)) from exc
        try:
            out, err = proc.communicate(timeout=PARTS_TIMEOUT)
        except subprocess.TimeoutExpired:
            #: N4 de #1666 : le sélecteur et ses petits-enfants, pas seulement
            #: l'enfant direct.
            _kill_group(proc)
            try:
                proc.wait(timeout=PARTS_REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            raise PartsError("the selector %s timed out after %d s"
                             % (plan.selector, PARTS_TIMEOUT))
        if proc.returncode != 0:
            tail = (err or out or b"").decode("utf-8", "replace").strip()[-300:]
            raise PartsError("the selector %s failed (exit %d): %s"
                             % (plan.selector, proc.returncode, tail))
        schema, names = _plan_file(result, plan)
        proof = {"schema": schema, "due": names, "selector": plan.selector,
                 "base": base, "merge_base": parent, "frozen": frozen}
        if cache:
            _cache_write(cache, key, proof)
        return proof
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
