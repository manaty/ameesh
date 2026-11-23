#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#8109 : faire suivre l'épingle canon de l'exécutant de staging au déploiement.

Exécuté en root sur un candidat par `staging.sh` (le script est passé sur
l'entrée standard, par le SSH root de la séquence : rien n'est installé sur
l'hôte). Il lit la tête du canon des agents **de cet hôte**
(`/home/ameesh/canon`, `refs/remotes/origin/main`), en fait un paquet git,
le fait entrer dans la copie du compte `ameesh-staging`, valide le commit avec
le `ameesh canon check` de ce compte, avance `refs/remotes/origin/main` en
avance rapide, puis déplace l'épingle `canon_ref` de sa configuration.

Pourquoi ici : un `canon_ref` épinglé n'est jamais déplacé par une passe
(#7053, #7750) ; le compte dédié n'a pas d'accès à `manaty/home`, et la seule
voie qui possède déjà le canon **et** un accès root aux candidats est la
séquence staging. Chaque déploiement staging remet donc l'épingle sur la tête
du canon que l'hôte vient de valider, sans geste opérateur. Une passe reste
seule juge de son propre rafraîchissement ; `staging_canon_stale` reste le
témoin.

Un refus laisse la copie et l'épingle intactes et rend un JSON nommé. Aucun
secret n'est lu, écrit ni imprimé. Rien n'arrête l'exécutant : la copie de
configuration est remplacée atomiquement, et une passe concurrente lit un
fichier complet, avant ou après le geste.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "ameesh-staging-canon/1"
ACCOUNT = "ameesh-staging"
SOURCE = "/home/ameesh/canon"
SOURCE_REF = "refs/remotes/origin/main"
CANDIDATE_REF = "refs/ameesh/staging-canon-candidate"
BACKUP = "config.json.avant-8109"
MAX_CONFIG = 1 << 20
GIT_TIMEOUT = 120
CHECK_TIMEOUT = 600
HEX = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
STATES_OK = frozenset(("installed", "current", "follows", "absent",
                       "unconfigured"))


class Refus(Exception):
    """Refus nommé : rien n'a été déplacé."""


@dataclass(frozen=True)
class Compte:
    nom: str
    uid: int
    gid: int
    home: str


def _raison(value, limite: int = 300) -> str:
    """Une raison publique : une ligne, bornée, sans secret."""
    texte = " ".join(str(value or "").split())
    return texte[:limite]


def _compte(nom: str) -> Compte | None:
    try:
        entree = pwd.getpwnam(nom)
    except KeyError:
        return None
    return Compte(entree.pw_name, entree.pw_uid, entree.pw_gid, entree.pw_dir)


def _compte_de_uid(uid: int) -> Compte:
    try:
        entree = pwd.getpwuid(uid)
    except KeyError:
        raise Refus("propriétaire uid %d inconnu" % uid) from None
    return Compte(entree.pw_name, entree.pw_uid, entree.pw_gid, entree.pw_dir)


def _commande(compte: Compte, argv) -> list[str]:
    """La commande à exécuter sous `compte`, ou pour le compte courant en test."""
    argv = [str(item) for item in argv]
    if os.geteuid() != 0:
        if compte.uid != os.geteuid():
            raise Refus("root requis pour agir sous %s" % compte.nom)
        return argv
    runuser = shutil.which("runuser") or "/usr/sbin/runuser"
    if not os.path.isfile(runuser):
        raise Refus("runuser absent")
    return [runuser, "-u", compte.nom, "--", "env",
            "HOME=" + compte.home, "USER=" + compte.nom,
            "LOGNAME=" + compte.nom,
            "XDG_RUNTIME_DIR=/run/user/%d" % compte.uid,
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus" % compte.uid,
            "GIT_CONFIG_NOSYSTEM=1", "LC_ALL=C"] + argv


def _lancer(compte: Compte, argv, *, timeout: int = GIT_TIMEOUT,
            stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _commande(compte, argv), stdin=subprocess.DEVNULL, stdout=stdout,
            stderr=subprocess.PIPE, timeout=timeout,
            text=(stdout is subprocess.PIPE))
    except subprocess.TimeoutExpired:
        raise Refus("délai dépassé (%ss)" % timeout) from None
    except OSError as exc:
        raise Refus("exécution impossible (%s)" % (exc.strerror or type(exc).__name__)) from None


def _git(compte: Compte, depot: Path, *args, timeout: int = GIT_TIMEOUT):
    return _lancer(compte, ["git", "-C", str(depot), *args], timeout=timeout)


def _git_ok(compte: Compte, depot: Path, *args, timeout: int = GIT_TIMEOUT) -> str:
    fait = _git(compte, depot, *args, timeout=timeout)
    if fait.returncode != 0:
        detail = (fait.stderr or fait.stdout or "").strip().splitlines()
        raise Refus("git %s: %s" % (args[0], _raison(detail[-1] if detail else
                                                    "code %d" % fait.returncode, 180)))
    return (fait.stdout or "").strip()


def _git_quiet(compte: Compte, depot: Path, *args) -> subprocess.CompletedProcess:
    return _git(compte, depot, *args)


def _est_depot(compte: Compte, chemin: Path) -> bool:
    try:
        return _git(compte, chemin, "rev-parse", "--git-dir").returncode == 0
    except Refus:
        return False


def _canon_agents() -> Path:
    """Le canon par défaut du compte des agents, ou le chemin conventionnel."""
    for fichier in (Path("/home/ameesh/.config/ameesh/config.json"),
                    Path("/home/ameesh/.config/agent-mesh/config.json")):
        try:
            donnees = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(donnees, dict):
            continue
        if isinstance(donnees.get("canon"), str) and donnees["canon"].strip():
            return _chemin_de(donnees["canon"], Path("/home/ameesh"))
        canons = donnees.get("canons")
        if isinstance(canons, list) and canons:
            premier = canons[0]
            if isinstance(premier, dict) and isinstance(premier.get("path"), str) \
                    and premier["path"].strip():
                return _chemin_de(premier["path"], Path("/home/ameesh"))
            if isinstance(premier, str) and premier.strip():
                return _chemin_de(premier, Path("/home/ameesh"))
    return Path(SOURCE)


def _ouvrir_dossier(home: Path, chemin: Path):
    """Ouvre le dossier de `chemin` sous `home`, sans suivre de lien.

    Rend (fd, nom) ; `fd` est le dossier parent ouvert, `nom` le fichier.
    Chaque composant est ouvert relativement au précédent avec O_NOFOLLOW :
    un lien dans le home du compte ne détourne ni la lecture ni l'écriture.
    """
    home = Path(home)
    chemin = Path(chemin)
    try:
        relatif = chemin.relative_to(home)
    except ValueError:
        raise Refus("chemin hors du home du compte : %s" % chemin) from None
    if not relatif.parts:
        raise Refus("chemin de configuration vide")
    if ".." in relatif.parts:
        raise Refus("chemin hors du home du compte : %s" % chemin)
    try:
        fd = os.open(str(home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refus("home illisible (%s)" % (exc.strerror or "?")) from None
    try:
        for partie in relatif.parts[:-1]:
            if partie in ("", "."):
                continue
            try:
                suivant = os.open(partie, os.O_RDONLY | os.O_DIRECTORY
                                 | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                return None, relatif.parts[-1]
            except OSError as exc:
                raise Refus("dossier %s illisible (%s)"
                            % (partie, exc.strerror or "?")) from None
            os.close(fd)
            fd = suivant
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd, relatif.parts[-1]


def _lire_fd(fd: int, nom: str) -> bytes | None:
    try:
        handle = os.open(nom, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Refus("configuration illisible (%s)" % (exc.strerror or "?")) from None
    try:
        infos = os.fstat(handle)
        if not stat.S_ISREG(infos.st_mode):
            raise Refus("configuration n'est pas un fichier régulier")
        with os.fdopen(handle, "rb") as flux:
            handle = None
            donnees = flux.read(MAX_CONFIG + 1)
    except OSError as exc:
        raise Refus("configuration illisible (%s)" % (exc.strerror or "?")) from None
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
    if len(donnees) > MAX_CONFIG:
        raise Refus("configuration trop grande")
    return donnees


def _ecrire_fd(fd: int, nom: str, donnees: bytes, compte: Compte,
               mode: int = 0o600) -> None:
    """Écrit `nom` dans le dossier ouvert, en remplacement atomique."""
    temporaire = ".%s.tmp-%d" % (nom, os.getpid())
    try:
        handle = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, mode, dir_fd=fd)
    except FileExistsError:
        os.unlink(temporaire, dir_fd=fd)
        handle = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, mode, dir_fd=fd)
    except OSError as exc:
        raise Refus("écriture impossible (%s)" % (exc.strerror or "?")) from None
    try:
        with os.fdopen(handle, "wb") as flux:
            os.fchmod(flux.fileno(), mode)
            os.fchown(flux.fileno(), compte.uid, compte.gid)
            flux.write(donnees)
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(temporaire, nom, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(temporaire, dir_fd=fd)
        except FileNotFoundError:
            pass
        raise


def _decoder(brut: bytes) -> dict:
    try:
        donnees = json.loads(brut.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refus("configuration JSON illisible (%s)" % type(exc).__name__) from None
    if not isinstance(donnees, dict):
        raise Refus("configuration JSON inattendue")
    return donnees


def _lire_config(fd: int, nom: str) -> tuple[dict, bytes]:
    brut = _lire_fd(fd, nom)
    if brut is None:
        raise Refus("configuration absente")
    return _decoder(brut), brut


def _epingle(config: dict):
    """(emplacement, valeur) de l'épingle canon, ou None si le canon suit un ref."""
    if "canons" in config:
        if any(cle in config for cle in ("canon", "canon_ref")):
            raise Refus("canons et canon_ref ensemble : configuration refusée")
        canons = config["canons"]
        if not isinstance(canons, list):
            raise Refus("canons n'est pas une liste")
        if not canons:
            return None
        premier = canons[0]
        if not isinstance(premier, dict):
            return None
        valeur = premier.get("ref", "")
        if not isinstance(valeur, str):
            raise Refus("canons[0].ref n'est pas une chaîne")
        return ("canons", valeur)
    if "canon_ref" in config:
        valeur = config["canon_ref"]
        if not isinstance(valeur, str):
            raise Refus("canon_ref n'est pas une chaîne")
        return ("canon_ref", valeur)
    return None


def _poser_epingle(config: dict, emplacement: str, sha: str) -> None:
    if emplacement == "canons":
        config["canons"][0]["ref"] = sha
    else:
        config["canon_ref"] = sha


def _est_sha(valeur: str) -> bool:
    return bool(HEX.fullmatch(str(valeur or "").strip().lower()))


def _bundle(compte_source: Compte, source: Path, flux, source_ref: str) -> None:
    """Écrit le bundle de `source_ref` dans le fichier déjà ouvert `flux`.

    Le descripteur est ouvert une seule fois : le chemin du bundle vit dans le
    home du compte d'exécution, et une réouverture par root y suivrait un lien
    qu'il aurait posé entre-temps.
    """
    fait = _lancer(compte_source,
                   ["git", "-C", str(source), "bundle", "create", "-", source_ref],
                   timeout=GIT_TIMEOUT, stdout=flux)
    if fait.returncode != 0:
        detail = (fait.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise Refus("bundle: %s" % _raison(detail[-1] if detail else
                                          "code %d" % fait.returncode, 180))
    if os.fstat(flux.fileno()).st_size == 0:
        raise Refus("bundle vide")


def _canon_check(compte: Compte, copie: Path, venv: Path, sha: str) -> None:
    binaire = venv / "bin" / "ameesh"
    if not binaire.is_file() or not os.access(str(binaire), os.X_OK):
        raise Refus("venv de l'exécutant illisible")
    fait = _lancer(compte, [str(binaire), "canon", "check", "--canon", str(copie),
                            "--ref", sha], timeout=CHECK_TIMEOUT)
    if fait.returncode != 0:
        detail = (fait.stderr or fait.stdout or "").strip().splitlines()
        raise Refus("canon check: %s" % _raison(
            detail[-1] if detail else "code %d" % fait.returncode, 220))


def _chemin_de(valeur: str, maison: Path) -> Path:
    """Un chemin de configuration : absolu, ou relatif au home du compte.

    `~/…` désigne le home du compte d'exécution (la configuration est la
    sienne), jamais celui de root qui lit le fichier.
    """
    texte = str(valeur or "").strip()
    if texte == "~":
        return maison
    if texte.startswith("~/"):
        return maison / texte[2:]
    if texte.startswith("~"):
        return Path(os.path.expanduser(texte))
    chemin = Path(texte)
    return chemin if chemin.is_absolute() else maison / chemin


def _copie_config(donnees: dict, maison: Path) -> Path | None:
    """La racine du canon par défaut de la configuration, ou None.

    Même règle que `Config` : `canon` pour la forme historique, sinon le
    `path` de la première entrée de `canons` (le canon par défaut).
    """
    if isinstance(donnees.get("canon"), str) and donnees["canon"].strip():
        return _chemin_de(donnees["canon"], maison)
    canons = donnees.get("canons")
    if isinstance(canons, list) and canons and isinstance(canons[0], dict):
        chemin = canons[0].get("path")
        if isinstance(chemin, str) and chemin.strip():
            return _chemin_de(chemin, maison)
    return None


def _lire_config_ferme(compte: Compte, maison: Path, config: Path) -> dict | None:
    """Lit la configuration sans garder de descripteur ouvert (déduction)."""
    dossier, nom = _ouvrir_dossier(maison, config)
    if dossier is None:
        return None
    try:
        brut = _lire_fd(dossier, nom)
        return _decoder(brut) if brut is not None else None
    finally:
        os.close(dossier)


def _avancer(compte: Compte, maison: Path, source: Path, source_ref: str,
             config: Path, copie: Path, venv: Path) -> dict:
    try:
        source_uid = source.stat().st_uid
    except OSError:
        return {"state": "no-source", "reason": "canon des agents absent"}
    if not source.is_dir() or not _est_depot(_compte_de_uid(source_uid), source):
        return {"state": "no-source", "reason": "canon des agents illisible"}
    compte_source = _compte_de_uid(source_uid)
    try:
        head = _git_ok(compte_source, source, "rev-parse", "--verify",
                       source_ref + "^{commit}")
    except Refus as exc:
        return {"state": "no-source", "reason": str(exc)}
    if not _est_sha(head):
        return {"state": "no-source", "reason": "tête de canon illisible"}
    head = head.lower()

    dossier, nom = _ouvrir_dossier(maison, config)
    if dossier is None:
        return {"state": "unconfigured", "reason": "configuration absente"}
    try:
        ancien_brut = _lire_fd(dossier, nom)
        if ancien_brut is None:
            # Pas de configuration : pas d'exécutant sur cet hôte. Ce n'est pas
            # un refus (rien à avancer), mais l'état est nommé pour la trace.
            return {"state": "unconfigured", "reason": "configuration absente"}
        donnees = _decoder(ancien_brut)
        epingle = _epingle(donnees)
        if epingle is None:
            return {"state": "follows", "reason": "aucune épingle canon_ref"}
        emplacement, ancien = epingle
        if not _est_sha(ancien):
            # Un ref (origin/main) suit déjà le distant : #7053 s'en charge.
            return {"state": "follows", "ref": ancien}
        ancien = ancien.strip().lower()
        if not copie.is_dir() or not _est_depot(compte, copie):
            # Une configuration existe mais sa copie manque : l'exécutant est
            # cassé, pas absent ; le témoin doit le dire.
            return {"state": "refused",
                    "reason": "copie canon de l'exécutant illisible"}
        if ancien == head:
            if _git_quiet(compte, copie, "cat-file", "-e",
                          head + "^{commit}").returncode != 0:
                return {"state": "refused",
                        "reason": "tête absente de la copie de l'exécutant"}
            return {"state": "current", "from": ancien, "to": head}
        # Le compte peut changer sa copie ; on ne déplace qu'après la preuve
        # d'ancêtre et le canon check.
        resultat = {"from": ancien, "to": head, "source_ref": source_ref}
        bundle = None
        try:
            cache = maison / ".cache" / "ameesh"
            fait = _lancer(compte, ["mkdir", "-p", str(cache)], timeout=30)
            if fait.returncode != 0:
                raise Refus("dossier de travail de l'exécutant illisible")
            handle, bundle_nom = tempfile.mkstemp(
                prefix="canon-8109-", suffix=".bundle", dir=str(cache))
            bundle = Path(bundle_nom)
            with os.fdopen(handle, "wb") as flux:
                _bundle(compte_source, source, flux, source_ref)
                # Le bundle appartient au compte, pas à root : c'est lui qui
                # le lit ensuite par `git fetch`.
                os.fchown(flux.fileno(), compte.uid, compte.gid)
                os.fchmod(flux.fileno(), 0o600)
            fetch = _git(compte, copie, "fetch", "--quiet", "--no-tags",
                         "--refmap=", "--no-write-fetch-head", str(bundle),
                         "+%s:%s" % (source_ref, CANDIDATE_REF))
            if fetch.returncode != 0:
                detail = (fetch.stderr or fetch.stdout or "").strip().splitlines()
                raise Refus("fetch: %s" % _raison(
                    detail[-1] if detail else "code %d" % fetch.returncode, 180))
            candidate = _git_ok(compte, copie, "rev-parse", "--verify",
                                CANDIDATE_REF + "^{commit}")
            if candidate.lower() != head:
                raise Refus("le candidat relu n'est pas la tête demandée")
            if _git_quiet(compte, copie, "cat-file", "-e",
                          ancien + "^{commit}").returncode != 0:
                raise Refus("ancienne épingle absente de la copie")
            if _git_quiet(compte, copie, "merge-base", "--is-ancestor",
                          ancien, head).returncode != 0:
                raise Refus("le canon a divergé (pas une avance rapide)")
            _canon_check(compte, copie, venv, head)
            suivi = _git_quiet(compte, copie, "rev-parse", "--verify", "--quiet",
                               "refs/remotes/origin/main")
            suivi_ancien = (suivi.stdout or "").strip() if suivi.returncode == 0 else ""
            if suivi_ancien and suivi_ancien != head:
                if _git_quiet(compte, copie, "merge-base", "--is-ancestor",
                              suivi_ancien, head).returncode == 0:
                    monte = _git_quiet(compte, copie, "update-ref", "-m",
                                       "ameesh staging canon pin (#8109)",
                                       "refs/remotes/origin/main", head, suivi_ancien)
                    if monte.returncode != 0:
                        resultat["tracking_note"] = "ref de suivi non déplacée"
                else:
                    resultat["tracking_note"] = "ref de suivi divergente, laissée"
            elif not suivi_ancien:
                _git_quiet(compte, copie, "update-ref", "-m",
                           "ameesh staging canon pin (#8109)",
                           "refs/remotes/origin/main", head)
            # Relecture sous le même dossier ouvert : une autre écriture
            # pendant le geste est refusée, pas écrasée.
            a_jour, avant = _lire_config(dossier, nom)
            epingle_a_jour = _epingle(a_jour)
            if epingle_a_jour is None or epingle_a_jour[1].strip().lower() != ancien:
                raise Refus("l'épingle a changé pendant le geste")
            nouveau = dict(a_jour)
            _poser_epingle(nouveau, emplacement, head)
            nouvelle_brut = (json.dumps(nouveau, indent=2, ensure_ascii=False)
                             + "\n").encode("utf-8")
            try:
                _ecrire_fd(dossier, BACKUP, avant, compte)
                _ecrire_fd(dossier, nom, nouvelle_brut, compte)
            except Exception:
                try:
                    _ecrire_fd(dossier, nom, avant, compte)
                except Exception:
                    pass
                raise
            relu, _ = _lire_config(dossier, nom)
            epingle_relue = _epingle(relu)
            if epingle_relue is None or epingle_relue[1].strip().lower() != head:
                try:
                    _ecrire_fd(dossier, nom, avant, compte)
                except Exception:
                    pass
                raise Refus("configuration relue sans la nouvelle épingle")
            resultat["state"] = "installed"
            resultat["backup"] = BACKUP
            return resultat
        finally:
            if bundle is not None:
                try:
                    os.unlink(bundle)
                except FileNotFoundError:
                    pass
    finally:
        os.close(dossier)


def avancer(*, account_name: str = ACCOUNT, home: str | None = None,
            config: str | None = None, repo: str | None = None,
            venv: str | None = None, source: str | None = None,
            source_ref: str = SOURCE_REF) -> dict:
    """Le geste complet, sans jamais lever : un état JSON nommé.

    `home`, `config`, `repo` et `venv` sont déduits du compte quand ils sont
    absents ; `source` est le canon des agents sur l'hôte.
    """
    resultat = {"schema": SCHEMA, "host": os.uname().nodename,
                "account": account_name}
    compte = _compte(account_name)
    if compte is None:
        resultat.update(state="absent", reason="compte %s absent" % account_name)
        return resultat
    maison = Path(home or compte.home)
    chemin_config = Path(config or maison / ".config" / "ameesh" / "config.json")
    if repo:
        chemin_copie = Path(repo)
    else:
        # La racine du canon vient de la configuration du compte (forme
        # historique `canon` ou première entrée `canons`) ; à défaut,
        # l'emplacement conventionnel. Une configuration illisible est
        # laissée à `_avancer`, qui rend le refus nommé.
        try:
            donnees = _lire_config_ferme(compte, maison, chemin_config)
        except Refus:
            donnees = None
        chemin_copie = ((_copie_config(donnees, maison) if donnees else None)
                        or maison / "canon")
    chemin_venv = Path(venv or maison / ".local" / "share" / "ameesh" / "venv")
    chemin_source = Path(source) if source else _canon_agents()
    resultat.update(config=str(chemin_config), repo=str(chemin_copie),
                    source=str(chemin_source))
    try:
        resultat.update(_avancer(
            compte, maison, chemin_source, source_ref, chemin_config,
            chemin_copie, chemin_venv))
    except Refus as exc:
        resultat.update(state="refused", reason=str(exc)[:400])
    except Exception as exc:  # noqa: BLE001 - le déploiement ne doit pas tomber
        resultat.update(state="refused",
                        reason=("interne: %s: %s"
                                % (type(exc).__name__, _raison(exc, 240))))
    return resultat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", default=ACCOUNT,
                        help="compte de l'exécutant (défaut : %(default)s)")
    parser.add_argument("--home", help="home du compte (déduit du compte)")
    parser.add_argument("--config", help="configuration de l'exécutant")
    parser.add_argument("--repo", help="copie canon de l'exécutant")
    parser.add_argument("--venv", help="venv de l'exécutant")
    parser.add_argument("--source", help="canon des agents sur cet hôte")
    parser.add_argument("--source-ref", default=SOURCE_REF,
                        help="révision du canon des agents (défaut : %(default)s)")
    args = parser.parse_args(argv)
    resultat = avancer(account_name=args.account, home=args.home,
                       config=args.config, repo=args.repo, venv=args.venv,
                       source=args.source, source_ref=args.source_ref)
    print(json.dumps(resultat, sort_keys=True, ensure_ascii=False))
    return 0 if resultat.get("state") in STATES_OK else 2


if __name__ == "__main__":
    raise SystemExit(main())
