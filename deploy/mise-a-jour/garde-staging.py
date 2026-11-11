#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pose la garde de demarrage du staging (ENV_SHA) sur une VM d'agents (#6806).

La garde versionnee `staging-startup-guard.py` (sha256 4bde802e...) est celle
qui protege l'executeur elu : elle n'admet que l'uid 980, les nodenames
`ameesh-agents-1` et `ameesh-agents-2`, l'acteur `ameesh:staging` et le
contenu public exact de `~ameesh-staging/.config/ameesh/bascule.env`
(ENV_SHA = 1223707f...). Le drop-in
`systemd/ameesh-staging-10-native-actor.conf` (sha256 ce3ed09c...) pose
`AMEESH_OPERATEUR=ameesh:staging` et l'`ExecStartPre` de la garde.

`installer` :
  * verifie le compte `ameesh-staging`, son home 0700 et les parents de
    configuration (memes controles que la garde) ;
  * lit `bascule.env` et refuse, par un refus nomme, s'il differe du contenu
    versionne : une edition a la main ne passe pas par l'installateur ;
  * pose la garde (980:0755) et le drop-in (980:0644), recharge le
    gestionnaire utilisateur et fait tourner la garde sous 980 (doit rendre 0) ;
  * `--accepter-bascule` est le chemin de mise a jour du hash : apres revue
    du changement, il reecrit PUBLIC_ENV (donc ENV_SHA) dans la copie
    installee. `pin` fait la meme chose sans reposer le drop-in.
  * un hote sans compte `ameesh-staging` (AG3) sort en 3 : rien a poser.

Aucun service n'est demarre ni arrete. `--a-blanc` ne modifie rien. Un echec
de verification restaure les octets precedents. Les acces au home du compte
passent par des descripteurs de repertoire ancres (chaque composant ouvert
sans suivre de lien, ecritures et remplacements relatifs au parent ouvert) :
la substitution d'un parent apres controle ne detourne ni une lecture ni une
ecriture.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

COMPTE = 'ameesh-staging'
GARDE_REL = Path('.local/libexec/staging-startup-guard.py')
DROPIN_REL = Path('.config/systemd/user/ameesh-staging.service.d/10-native-actor.conf')
BASCULE_REL = Path('.config/ameesh/bascule.env')
ACTEUR = b'AMEESH_OPERATEUR=ameesh:staging'
UID_GARDE = 980
TAILLE_MAX = 65536


class Refus(ValueError):
    """Refus nomme : rien n'est pose."""


def empreinte(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assignation(source: bytes, nom: str):
    """Noeud AST de l'assignation `nom = ...` du module."""
    try:
        arbre = ast.parse(source)
    except SyntaxError as exc:
        raise Refus('garde versionnee illisible : %s' % exc) from None
    for noeud in arbre.body:
        if not isinstance(noeud, ast.Assign) or len(noeud.targets) != 1:
            continue
        cible = noeud.targets[0]
        if isinstance(cible, ast.Name) and cible.id == nom:
            return noeud
    raise Refus('garde versionnee : %s introuvable' % nom)


def _valeur_litterale(expression, nom: str):
    try:
        return ast.literal_eval(expression)
    except (ValueError, SyntaxError, TypeError):
        raise Refus('garde versionnee : %s non litteral' % nom) from None


def _home_versionnee(source: bytes) -> tuple[object, str]:
    """Noeud et valeur de `HOME = Path('...')` dans la garde versionnee."""
    noeud = _assignation(source, 'HOME')
    appel = noeud.value
    if not (isinstance(appel, ast.Call) and isinstance(appel.func, ast.Name)
            and appel.func.id == 'Path' and len(appel.args) == 1):
        raise Refus('garde versionnee : HOME = Path(...) attendu')
    valeur = _valeur_litterale(appel.args[0], 'HOME')
    if not isinstance(valeur, str):
        raise Refus('garde versionnee : HOME doit etre un chemin')
    return noeud, valeur


def _hotes_versionnes(source: bytes) -> tuple[str, ...]:
    """Nodenames admis par la garde versionnee."""
    trouve = re.search(rb'os\.uname\(\)\.nodename not in \(([^)]*)\)', source)
    if not trouve:
        raise Refus('garde versionnee : liste des nodenames introuvable')
    try:
        valeur = ast.literal_eval('(' + trouve.group(1).decode('ascii') + ')')
    except (ValueError, SyntaxError):
        raise Refus('garde versionnee : liste des nodenames illisible') from None
    if not isinstance(valeur, tuple) or not all(isinstance(v, str) for v in valeur):
        raise Refus('garde versionnee : liste des nodenames invalide')
    return valeur


def _remplacer(source: bytes, noeud, ligne: bytes) -> bytes:
    if not source.endswith(b'\n'):
        raise Refus('garde versionnee : fin de ligne attendue')
    lignes = source.splitlines(keepends=True)
    debut = noeud.lineno - 1
    fin = noeud.end_lineno
    if debut < 0 or fin > len(lignes) or fin <= debut:
        raise Refus('garde versionnee : assignation hors du fichier')
    lignes[debut:fin] = [ligne + b'\n']
    return b''.join(lignes)


def rendre(source: bytes, home: Path, env: bytes) -> bytes:
    """Copie installable : HOME et PUBLIC_ENV rendus, reste inchange."""
    noeud_home, home_actuel = _home_versionnee(source)
    noeud_env = _assignation(source, 'PUBLIC_ENV')
    env_actuel = _valeur_litterale(noeud_env.value, 'PUBLIC_ENV')
    if not isinstance(env_actuel, bytes):
        raise Refus('garde versionnee : PUBLIC_ENV doit etre un bytes')
    neuf = source
    if home_actuel != str(home):
        neuf = _remplacer(neuf, noeud_home,
                          b'HOME = Path(' + repr(str(home)).encode('utf-8') + b')')
    if env_actuel != env:
        # relire le noeud apres un eventuel premier remplacement
        noeud_env = _assignation(neuf, 'PUBLIC_ENV')
        neuf = _remplacer(neuf, noeud_env, b'PUBLIC_ENV = ' + repr(env).encode('utf-8'))
    try:
        compile(neuf, '<garde-staging>', 'exec')
    except SyntaxError as exc:
        raise Refus('garde rendue invalide : %s' % exc) from None
    return neuf


def _ouvrir_home(home: Path, uid: int) -> int:
    """Descripteur du home du compte, sans suivre de lien, controle 0700."""
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refus('home du compte illisible : %s' % exc) from None
    meta = os.fstat(fd)
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid
            or stat.S_IMODE(meta.st_mode) != 0o700):
        os.close(fd)
        raise Refus('home dedie attendu en 0700 et proprietaire du compte')
    return fd


def _ouvrir_dossier(parent_fd: int, composants, uid: int, gid: int,
                    creer: bool) -> int:
    """Ouvre les composants sous parent_fd, sans suivre aucun lien.

    Chaque composant est resolu relativement au descripteur precedent : une
    substitution du parent apres controle ne detourne pas l'ecriture. Les
    niveaux crees sont poses 0755 et appartiennent au compte.
    """
    fd = os.dup(parent_fd)
    try:
        for nom in composants:
            cree = False
            for _ in range(3):
                try:
                    suivant = os.open(nom, os.O_RDONLY | os.O_DIRECTORY
                                      | os.O_NOFOLLOW, dir_fd=fd)
                    break
                except FileNotFoundError:
                    if not creer:
                        raise Refus('dossier de configuration introuvable : %s'
                                    % nom) from None
                    try:
                        os.mkdir(nom, 0o755, dir_fd=fd)
                        cree = True
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise Refus('dossier non creable (%s) : %s'
                                    % (nom, exc)) from None
                except OSError as exc:
                    raise Refus('dossier non sur (%s) : %s' % (nom, exc)) from None
            else:
                raise Refus('dossier instable : %s' % nom)
            try:
                if cree:
                    os.fchown(suivant, uid, gid)
                    os.fchmod(suivant, 0o755)
                meta = os.fstat(suivant)
            except BaseException:
                os.close(suivant)
                raise
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != uid
                    or stat.S_IMODE(meta.st_mode) & 0o022
                    or (cree and stat.S_IMODE(meta.st_mode) != 0o755)):
                os.close(suivant)
                raise Refus('dossier non sur : %s' % nom)
            os.close(fd)
            fd = suivant
        return fd
    except BaseException:
        os.close(fd)
        raise


def lire_bascule(home: Path, uid: int) -> bytes:
    """Memes controles que la garde, sur des descripteurs ancres au home."""
    home_fd = _ouvrir_home(home, uid)
    try:
        config_fd = _ouvrir_dossier(home_fd, ('.config', 'ameesh'),
                                    uid, 0, False)
    finally:
        os.close(home_fd)
    try:
        try:
            fd = os.open('bascule.env',
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=config_fd)
        except OSError as exc:
            raise Refus('bascule.env illisible : %s' % exc) from None
        with os.fdopen(fd, 'rb') as flux:
            meta = os.fstat(flux.fileno())
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != uid
                    or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_nlink != 1):
                raise Refus('bascule.env : fichier regulier 0600, un seul lien attendu')
            if meta.st_size > TAILLE_MAX:
                raise Refus('bascule.env : taille maximale depassee')
            data = flux.read(TAILLE_MAX + 1)
    finally:
        os.close(config_fd)
    if len(data) != meta.st_size or len(data) > TAILLE_MAX:
        raise Refus('bascule.env : lecture incomplete')
    return data


def verifier_acteur(env: bytes) -> None:
    lignes = [ligne for ligne in env.splitlines()
              if ligne.startswith(b'AMEESH_OPERATEUR=')]
    if lignes != [ACTEUR]:
        raise Refus("bascule.env : une seule ligne AMEESH_OPERATEUR=ameesh:staging attendue")


def _lire_ancien(dir_fd: int, nom: str) -> bytes | None:
    """Octets de la cible sous dir_fd, ou None si absente ; jamais un lien."""
    try:
        meta = os.stat(nom, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(meta.st_mode):
        raise Refus('cible non reguliere : %s' % nom)
    if meta.st_size > TAILLE_MAX:
        raise Refus('cible surdimensionnee : %s' % nom)
    try:
        fd = os.open(nom, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise Refus('cible illisible (%s) : %s' % (nom, exc)) from None
    with os.fdopen(fd, 'rb') as flux:
        return flux.read(TAILLE_MAX + 1)


def _ecrire_atomique(dir_fd: int, nom: str, data: bytes, mode: int,
                     uid: int, gid: int) -> None:
    """Ecrit et remplace `nom` relativement au parent ouvert."""
    temporaire = '.%s.tmp-%d' % (nom, os.getpid())
    try:
        fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode,
                     dir_fd=dir_fd)
    except FileExistsError:
        os.unlink(temporaire, dir_fd=dir_fd)
        fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode,
                     dir_fd=dir_fd)
    try:
        with os.fdopen(fd, 'wb') as flux:
            os.fchmod(flux.fileno(), mode)
            os.fchown(flux.fileno(), uid, gid)
            flux.write(data)
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(temporaire, nom, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(temporaire, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def _executer(args: list[str], executer) -> subprocess.CompletedProcess:
    return executer(args, check=False, capture_output=True, text=True, timeout=120)


def _recharger(compte: str, home: Path, uid: int, executer) -> None:
    env = ['HOME=%s' % home, 'XDG_RUNTIME_DIR=/run/user/%d' % uid,
           'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus' % uid]
    resultat = _executer(['runuser', '-u', compte, '--', 'env', *env,
                          'systemctl', '--user', 'daemon-reload'], executer)
    if resultat.returncode != 0:
        raise Refus('daemon-reload du gestionnaire utilisateur en echec : %s'
                    % (resultat.stderr or '').strip())


def _verifier(compte: str, garde: Path, executer) -> subprocess.CompletedProcess:
    return _executer(['runuser', '-u', compte, '--', 'env', '-i',
                      'AMEESH_OPERATEUR=ameesh:staging', '/usr/bin/python3.12',
                      '-I', '-B', str(garde)], executer)


def _restaurer(dir_fd: int, nom: str, ancien: bytes | None, mode: int,
               uid: int, gid: int) -> None:
    if ancien is None:
        try:
            os.unlink(nom, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    else:
        _ecrire_atomique(dir_fd, nom, ancien, mode, uid, gid)


def installer(home: Path, uid: int, gid: int, compte: str, source: Path,
              dropin: Path | None = None, accepter: bool = False,
              appliquer: bool = True, executer=subprocess.run,
              uid_attendu: int = UID_GARDE) -> dict:
    """Pose la garde et, si fourni, le drop-in ; rend un compte rendu."""
    try:
        source = Path(source).read_bytes()
    except OSError as exc:
        raise Refus('garde versionnee illisible : %s' % exc) from None
    _, home_versionne = _home_versionnee(source)
    if Path(home_versionne) != home:
        raise Refus('garde versionnee pour %s, compte sur %s'
                    % (home_versionne, home))
    if uid != uid_attendu:
        raise Refus('garde versionnee pour l\'uid %d, compte %s a l\'uid %d'
                    % (uid_attendu, compte, uid))
    env = lire_bascule(home, uid)
    verifier_acteur(env)
    env_versionne = _valeur_litterale(_assignation(source, 'PUBLIC_ENV').value, 'PUBLIC_ENV')
    if not isinstance(env_versionne, bytes):
        raise Refus('garde versionnee : PUBLIC_ENV doit etre un bytes')
    change = env != env_versionne
    if change and not accepter:
        raise Refus(
            "bascule.env differe de l'empreinte versionnee (ENV_SHA %s attendu, %s lu) : "
            "edition hors installateur ; revoir le changement puis relancer avec "
            "--accepter-bascule" % (empreinte(env_versionne)[:16], empreinte(env)[:16]))
    garde = rendre(source, home, env)
    garde_cible = home / GARDE_REL
    contenu_dropin = None
    dropin_cible = home / DROPIN_REL
    if dropin is not None:
        try:
            contenu_dropin = Path(dropin).read_bytes()
        except OSError as exc:
            raise Refus('drop-in versionne illisible : %s' % exc) from None
        attendu = (b'[Service]\nEnvironment=AMEESH_OPERATEUR=ameesh:staging\n'
                   b'ExecStartPre=/usr/bin/python3.12 -I -B '
                   + str(garde_cible).encode('utf-8') + b'\n')
        if contenu_dropin != attendu:
            raise Refus('drop-in versionne incoherent avec %s' % garde_cible)
    compte_rendu = {
        'compte': compte, 'home': str(home), 'change': change,
        'garde': str(garde_cible), 'dropin': str(dropin_cible) if contenu_dropin else None,
        'env_sha': empreinte(env), 'garde_sha': empreinte(garde),
        'dropin_sha': empreinte(contenu_dropin) if contenu_dropin else None,
        'candidat': os.uname().nodename in _hotes_versionnes(source),
        'verifie': False,
    }
    if not appliquer:
        return compte_rendu
    home_fd = _ouvrir_home(home, uid)
    garde_dir = None
    dropin_dir = None
    try:
        garde_dir = _ouvrir_dossier(home_fd, ('.local', 'libexec'),
                                    uid, gid, True)
        if contenu_dropin is not None:
            dropin_dir = _ouvrir_dossier(
                home_fd, ('.config', 'systemd', 'user',
                          'ameesh-staging.service.d'), uid, gid, True)
        ancienne_garde = _lire_ancien(garde_dir, garde_cible.name)
        ancien_dropin = (_lire_ancien(dropin_dir, dropin_cible.name)
                         if dropin_dir is not None else None)
        garde_change = ancienne_garde != garde
        dropin_change = contenu_dropin is not None and ancien_dropin != contenu_dropin
        try:
            if garde_change:
                _ecrire_atomique(garde_dir, garde_cible.name, garde,
                                 0o755, uid, gid)
            if dropin_change:
                _ecrire_atomique(dropin_dir, dropin_cible.name, contenu_dropin,
                                 0o644, uid, gid)
            if dropin_change:
                _recharger(compte, home, uid, executer)
            if compte_rendu['candidat']:
                resultat = _verifier(compte, garde_cible, executer)
                if resultat.returncode != 0:
                    raise Refus('la garde refuse apres installation (rc %d) : %s'
                                % (resultat.returncode,
                                   (resultat.stderr or '').strip()))
                compte_rendu['verifie'] = True
        except BaseException:
            if garde_change:
                _restaurer(garde_dir, garde_cible.name, ancienne_garde,
                           0o755, uid, gid)
            if dropin_change:
                _restaurer(dropin_dir, dropin_cible.name, ancien_dropin,
                           0o644, uid, gid)
                try:
                    _recharger(compte, home, uid, executer)
                except Refus:
                    pass
            raise
    finally:
        if dropin_dir is not None:
            os.close(dropin_dir)
        if garde_dir is not None:
            os.close(garde_dir)
        os.close(home_fd)
    return compte_rendu


def _compte(nom: str):
    try:
        entree = pwd.getpwnam(nom)
    except KeyError:
        return None
    return Path(entree.pw_dir), entree.pw_uid, entree.pw_gid


def principal(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--compte', default=COMPTE,
                        help='compte de l\'executeur staging (defaut : %(default)s)')
    parser.add_argument('--source', type=Path,
                        default=Path(__file__).with_name('staging-startup-guard.py'))
    parser.add_argument('--a-blanc', action='store_true',
                        help='ne rien modifier, imprimer ce qui serait fait')
    sous = parser.add_subparsers(dest='action', required=True)
    p_installer = sous.add_parser('installer', help='poser la garde et le drop-in')
    p_installer.add_argument('--dropin', type=Path,
                             default=Path(__file__).with_name('systemd')
                             / 'ameesh-staging-10-native-actor.conf')
    p_installer.add_argument('--accepter-bascule', action='store_true',
                             help='adopter bascule.env et mettre a jour ENV_SHA')
    p_pin = sous.add_parser('pin', help='mettre a jour ENV_SHA seulement')
    p_pin.add_argument('--accepter-bascule', action='store_true', default=True,
                       help=argparse.SUPPRESS)
    sous.add_parser('verifier', help='faire tourner la garde installee')
    args = parser.parse_args(argv)
    compte = _compte(args.compte)
    if compte is None:
        print('hote sans compte %s : aucune garde de staging a poser' % args.compte)
        return 3
    home, uid, gid = compte
    try:
        if args.action == 'verifier':
            garde = home / GARDE_REL
            home_fd = _ouvrir_home(home, uid)
            try:
                garde_dir = _ouvrir_dossier(home_fd, ('.local', 'libexec'),
                                            uid, gid, False)
            finally:
                os.close(home_fd)
            try:
                contenu = _lire_ancien(garde_dir, GARDE_REL.name)
            finally:
                os.close(garde_dir)
            if contenu is None:
                raise Refus('garde absente : %s' % garde)
            resultat = _verifier(args.compte, garde, subprocess.run)
            if resultat.returncode != 0:
                print('garde refusee (rc %d) : %s'
                      % (resultat.returncode, (resultat.stderr or '').strip()),
                      file=sys.stderr)
                return 1
            print('garde acceptee par %s (%s)'
                  % (args.compte, empreinte(contenu)[:16]))
            return 0
        dropin = args.dropin if args.action == 'installer' else None
        rapport = installer(home, uid, gid, args.compte, args.source, dropin,
                            accepter=args.accepter_bascule,
                            appliquer=not args.a_blanc)
    except Refus as exc:
        print('refus : %s' % exc, file=sys.stderr)
        return 2
    print('garde staging : %s ; bascule.env sha %s%s ; garde sha %s%s'
          % (rapport['garde'], rapport['env_sha'][:16],
             ' (mise a jour)' if rapport['change'] else '',
             rapport['garde_sha'][:16],
             ' (verifiee)' if rapport['verifie'] else
             (' (hote hors candidats)' if not rapport['candidat'] else
              (' (a blanc)' if args.a_blanc else ''))))
    return 0


if __name__ == '__main__':
    raise SystemExit(principal())
