#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bascule atomique du venv d'ameesh (#1969).

    bascule.py preparer --venv <venv> --src <code> --version <sha7>
    bascule.py activer  --venv <venv> --version <sha7> [--precedent <sha7>]
    bascule.py retour   --venv <venv>
    bascule.py etat     --venv <venv> [--json]

Le venv vivant ne reçoit plus jamais de `pip`, et aucun processus ne dépend
plus d'un chemin qu'une bascule changerait sous lui :

* chaque version vit dans un venv COMPLET et immuable (`venv-<sha7>`), préparé
  à l'écart : pip dans son coin, puis contrôles d'imports, de point d'entrée et
  de shebang AVANT toute mise en service ;
* `<venv>` reste un répertoire stable qui ne porte que des LANCEURS
  (`bin/python` et ses alias, `bin/ameesh`, et un lanceur par script console de
  la version vivante — #2155) : ils lisent le lien `venv-courant` au
  démarrage et exécutent le binaire du venv versionné. L'interpréteur voit
  donc un chemin CONCRET (`venv-<sha7>/lib/pythonX/site-packages`) : un
  processus déjà lancé garde ce chemin, et une bascule suivante ne peut plus
  lui servir un mélange ancien/nouveau (défaut B1 de la revue du 16/10) ;
* `activer` ne fait qu'un rename : le lien `venv-courant` change d'un coup,
  un appel concurrent voit l'ancien ou le nouveau venv entier, jamais un état
  incomplet (incident du 16/10 : pip dans le venv actif,
  `ModuleNotFoundError ameesh.platform.process`, `ameesh` absent/code 127) ;
* la première conversion d'un venv RÉEL ne le déplace pas et ne touche pas son
  `lib/` : ce contenu est réutilisé (lien) comme version précédente
  `venv-<sha7>`, seuls les interpréteurs de `bin/` et `bin/ameesh` sont remplacés par les
  lanceurs — un processus en cours n'importe pas depuis `bin/`. Le retour se
  fait en rebasculant le lien, sans pip.

`retour` repointe vers la version précédente enregistrée, sans pip ; il est
idempotent. Aucun octet d'un venv versionné n'est modifié après préparation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

MARKER = ".pret"                 # venv-<version> complet et contrôlé
CURRENT = "venv-courant"         # lien : version vivante
REPORT = "venv.precedent"        # fichier : version précédente enregistrée
LAUNCHERS = ".bascule-lanceurs"  # marqueur : <venv> porte les lanceurs
SHEBANG_MAX_LINE = 8192          # borne en octets, même pour un script malformé

_LAUNCHER = """#!/bin/sh
# #1969 : lanceur stable du venv d'ameesh. Il résout le venv versionné vivant
# (lien {lien}) et exécute son binaire : le processus garde ensuite un chemin
# concret, insensible aux bascules suivantes.
set -e
moi=$(readlink -f -- "$0" 2>/dev/null || echo "$0")
base=$(CDPATH= cd -- "$(dirname -- "$moi")/../.." && pwd)
courant=$(readlink -- "$base/{lien}" 2>/dev/null) || {{
  echo "bascule: aucun venv vivant ($base/{lien})" >&2; exit 127; }}
case "$courant" in /*) dir=$courant;; *) dir=$base/$courant;; esac
cible="$dir/bin/{binaire}"
[ -x "$cible" ] || {{ echo "bascule: venv vivant incomplet ($cible)" >&2; exit 127; }}
VIRTUAL_ENV=$dir; export VIRTUAL_ENV
PATH=$dir/bin:$PATH; export PATH
exec "$cible" "$@"
"""

_PIP_REFUS = """#!/bin/sh
echo "bascule: pip est interdit dans le venv vivant ; préparez un venv versionné (bascule.py preparer)" >&2
exit 1
"""


def _die(message, code=1):
    print("bascule: %s" % message, file=sys.stderr)
    sys.exit(code)


def _base(venv):
    return os.path.dirname(os.path.abspath(venv))


def _version_dir(venv, version):
    return os.path.join(_base(venv), "venv-%s" % version)


def _current(venv):
    return os.path.join(_base(venv), CURRENT)


def _report_path(venv):
    return os.path.join(_base(venv), REPORT)


def _version_name(value):
    version = (value or "").strip()
    if not version or "/" in version or version.startswith("."):
        _die("--version doit être un nom simple (sha court)")
    return version


def _write(path, data, mode=0o755):
    """Écrit un fichier en une fois (temp + rename) : jamais lu à moitié."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass  # déjà renommé, ou nettoyage impossible : garder l'erreur initiale


def _live_target(venv):
    """Version vivante (cible du lien `venv-courant`), ou None."""
    lien = _current(venv)
    if os.path.islink(lien):
        nom = os.path.basename(os.readlink(lien))
        return nom[len("venv-"):] if nom.startswith("venv-") else nom
    return None


def _pointer(venv, name):
    """Fait pointer `venv-courant` sur `venv-<name>` en un seul rename."""
    lien = _current(venv)
    tmp = "%s.suivant.%d" % (lien, os.getpid())
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink("venv-%s" % name, tmp)
    os.replace(tmp, lien)


def _read_report(venv):
    try:
        with open(_report_path(venv)) as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _write_report(venv, name):
    _write(_report_path(venv), name + "\n", mode=0o644)


# --------------------------------------------------------------------------
# préparation d'un venv versionné (pip à l'écart, contrôles avant service)
# --------------------------------------------------------------------------

def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def _creer_venv(directory, python):
    _run([python, "-m", "venv", directory])


def _pip_install(directory, src):
    pip = os.path.join(directory, "bin", "pip")
    _run([pip, "install", "-q", "--force-reinstall", "--no-deps", src])
    _run([pip, "install", "-q", src])


def _verifier_shebang(ameesh, python):
    """Refuse un préambule qui ne désigne pas l'interpréteur de CE venv.

    pip écrit `#!<venv>/bin/python3` quand le shebang tient dans 127 octets.
    Au-delà (ou si le chemin contient une espace), distlib écrit un préambule
    /bin/sh qui ré-exécute ce même interpréteur (#2156) :

        #!/bin/sh
        '''exec' <venv>/bin/python3 "$0" "$@"
        ' '''

    La troisième ligne ferme la chaîne triple : le fichier reste un script
    Python valide, lancé par sh puis par l'interpréteur cible.
    """
    def refuser(message):
        raise RuntimeError("préambule inattendu dans %s : %s" % (ameesh, message))

    def lire_ligne(handle, numero):
        ligne = handle.readline(SHEBANG_MAX_LINE + 1)
        if len(ligne) > SHEBANG_MAX_LINE:
            refuser("ligne %d : dépasse %d octets" % (numero, SHEBANG_MAX_LINE))
        return ligne.decode("utf-8", "replace").rstrip("\r\n")

    # pip écrit le shebang de l'interpréteur qui l'exécute : `bin/python3`
    # (ou `bin/python3.X`) dans un venv réel, jamais `bin/python` seul ; on
    # exige seulement l'interpréteur de CE venv.
    motif = re.escape(python) + r"(?:[0-9]+(?:\.[0-9]+)*)?"
    with open(ameesh, "rb") as handle:
        premiere = lire_ligne(handle, 1).strip()
        if re.fullmatch("#!" + motif, premiere):
            return  # aucune lecture du corps Python derrière un shebang direct
        if premiere != "#!/bin/sh":
            refuser("ligne 1 %r ; interpréteur attendu %r" % (premiere, python))
        execution = lire_ligne(handle, 2)
        fermeture = lire_ligne(handle, 3)
    prefixe = "'''exec' "
    if not execution.startswith(prefixe):
        refuser("ligne 2 %r : instruction exec de pip attendue" % execution)
    if fermeture != "' '''":
        refuser("ligne 3 %r : fermeture du préambule pip attendue" % fermeture)
    try:
        mots = shlex.split(execution[len(prefixe):])
    except ValueError as exc:
        refuser("ligne 2 %r : quotage shell invalide (%s)" % (execution, exc))
    if len(mots) != 3 or mots[1:] != ["$0", "$@"]:
        refuser('ligne 2 %r : arguments "$0" "$@" attendus' % execution)
    if re.fullmatch(motif, mots[0]) is None:
        refuser("ligne 2 %r : interpréteur attendu %r" % (execution, python))


def _controles(directory):
    """Imports, point d'entrée et shebang : tout doit répondre AVANT la bascule."""
    python = os.path.join(directory, "bin", "python")
    ameesh = os.path.join(directory, "bin", "ameesh")
    _verifier_shebang(ameesh, python)
    _run([python, "-c", "import psycopg, ameesh; print('imports ok')"],
         stdout=subprocess.DEVNULL)
    _run([ameesh, "--version"], stdout=subprocess.DEVNULL)


def preparer(args):
    version = _version_name(args.version)
    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        _die("--src introuvable : %s" % src)
    cible = _version_dir(args.venv, version)
    etat = etat_de(args.venv)
    if os.path.isdir(cible) and not os.path.islink(cible):
        if etat.get("versions", {}).get(version) == "complete":
            print("bascule: venv-%s déjà préparé" % version)
            return 0
        shutil.rmtree(cible)
    print("bascule: préparation de venv-%s depuis %s" % (version, src))
    try:
        _creer_venv(cible, args.python)
        _pip_install(cible, src)
        _controles(cible)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        shutil.rmtree(cible, ignore_errors=True)   # rien de partiel ne reste
        print("bascule: préparation de venv-%s ÉCHOUÉE, version active intacte : %s"
              % (version, exc), file=sys.stderr)
        return 1
    with open(os.path.join(cible, MARKER), "w") as handle:
        json.dump({"version": version, "src": src, "at": time.time()}, handle)
    print("bascule: venv-%s préparé et contrôlé" % version)
    return 0


# --------------------------------------------------------------------------
# lanceurs stables : le processus voit un chemin concret, pas le lien
# --------------------------------------------------------------------------

def _installer_lanceurs(venv, version):
    bin_dir = os.path.join(venv, "bin")
    try:
        os.makedirs(bin_dir, exist_ok=True)
    except OSError as exc:
        _die("bin/ inscriptible requis dans %s : %s" % (venv, exc))
    # Les scripts console installés par pip pointent souvent sur python3,
    # qui peut viser directement le binaire de base et contourner python.
    interpreteurs = {"python"} | {nom for nom in os.listdir(bin_dir)
                                  if re.fullmatch(r"python[0-9]+(?:\.[0-9]+)*", nom)}
    for nom in sorted(interpreteurs):
        _write(os.path.join(bin_dir, nom), _LAUNCHER.format(lien=CURRENT, binaire="python"))
    _write(os.path.join(bin_dir, "ameesh"), _LAUNCHER.format(lien=CURRENT, binaire="ameesh"))
    _write(os.path.join(bin_dir, "pip"), _PIP_REFUS)
    for nom in ("pip3", "pip3.14", "pip3.12"):
        if os.path.exists(os.path.join(bin_dir, nom)):
            _write(os.path.join(bin_dir, nom), _PIP_REFUS)
    # #2155 : les scripts console de la version qui s'active (agent-mail,
    # ameesh-approve, futur outil…) n'existaient dans le venv stable que s'ils
    # étaient là à l'adoption. On leur rend un lanceur qui suit `venv-courant`,
    # comme python et ameesh. Un script présent dans le stable mais absent de
    # la version visée n'est PAS touché : il garde son shebang vers le python
    # stable, qui résout la version vivante (témoin #1969 des alias).
    cible_bin = os.path.join(_version_dir(venv, version), "bin")
    scripts = _scripts_console(cible_bin)
    anciens = set(_read_lanceurs(venv).get("console_scripts") or ())
    for nom in scripts:
        _write(os.path.join(bin_dir, nom), _LAUNCHER.format(lien=CURRENT, binaire=nom))
    _write(os.path.join(venv, LAUNCHERS),
           json.dumps({"format": 3, "at": time.time(), "console_scripts": scripts}),
           mode=0o644)
    # Les lanceurs d'une version quittée ne sont retirés qu'APRÈS la bascule
    # (`_retirer_lanceurs`) : une activation qui échoue ne retire rien.
    return sorted(anciens - set(scripts))


def _retirer_lanceurs(venv, noms):
    """Retire les lanceurs de scripts console absents de la version vivante."""
    for nom in noms:
        chemin = os.path.join(venv, "bin", nom)
        if _est_lanceur(chemin):
            try:
                os.remove(chemin)
            except OSError:
                pass


def _scripts_console(directory):
    """Les points d'entrée pip de `directory/bin`, hors python/pip/activate.

    Un script console est un fichier exécutable qui commence par `#!` : on ne
    prend ni les activate (sourcés, sans shebang), ni les interpréteurs, ni pip
    (remplacé par le refus), ni `ameesh` (lanceur dédié)."""
    try:
        noms = sorted(os.listdir(directory))
    except OSError:
        return []
    scripts = []
    for nom in noms:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", nom):
            continue
        if re.fullmatch(r"(?:ameesh|python[0-9.]*|pip[0-9.]*|activate(?:\.csh|\.fish|\.ps1)?)", nom):
            continue
        chemin = os.path.join(directory, nom)
        if not os.path.isfile(chemin):
            continue
        try:
            with open(chemin, "rb") as handle:
                if handle.read(2) != b"#!":
                    continue
        except OSError:
            continue
        scripts.append(nom)
    return scripts


def _read_lanceurs(venv):
    try:
        with open(os.path.join(venv, LAUNCHERS)) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _est_lanceur(path):
    """Ce fichier est-il un lanceur écrit par nous (et non un script adopté) ?"""
    try:
        with open(path, "rb") as handle:
            return handle.read(64).startswith(b"#!/bin/sh\n# #1969 : lanceur stable du venv d'ameesh.")
    except OSError:
        return False


def _interpreteur_de_base(venv, python):
    """L'interpréteur à donner à la version adoptée, jamais le fichier stable.

    `bin/python` d'un venv est normalement un lien vers l'interpréteur de base
    (`pyvenv.cfg` : `executable`) ; un venv créé avec `--copies` porte un vrai
    binaire. Pointer sur le fichier du venv stable créerait une boucle : ce
    fichier devient le lanceur. On rend un chemin à lier, ou None s'il faut
    copier le binaire."""
    try:
        with open(os.path.join(venv, "pyvenv.cfg")) as handle:
            for ligne in handle:
                cle, _, valeur = ligne.partition("=")
                if cle.strip() == "executable" and valeur.strip():
                    cible = valeur.strip()
                    if os.path.isfile(cible) and os.access(cible, os.X_OK):
                        return cible
    except OSError:
        pass
    reel = os.path.realpath(python)
    stable = os.path.abspath(venv)
    if os.path.dirname(reel) != os.path.join(stable, "bin") and \
            os.path.commonpath([reel, stable]) != stable:
        return reel
    return None


def _adopter(venv, version):
    """Réutilise le venv RÉEL comme version précédente, sans pip ni copie.

    Le `lib/` du venv réel reste à son chemin (les processus déjà lancés y
    importent encore), et devient celui de `venv-<version>` par un lien. Seuls
    l'interpréteur et les scripts console sont recréés, avec des shebangs qui
    pointent sur le nouveau répertoire."""
    cible = _version_dir(venv, version)
    if os.path.isdir(cible):
        return cible
    python = os.path.join(venv, "bin", "python")
    cfg = os.path.join(venv, "pyvenv.cfg")
    if not os.path.exists(python) or not os.path.isfile(cfg):
        _die("venv réel sans bin/python ou sans pyvenv.cfg : conversion impossible")
    os.makedirs(os.path.join(cible, "bin"))
    try:
        for nom in ("lib", "include", "share"):
            source = os.path.join(venv, nom)
            if os.path.isdir(source):
                os.symlink(os.path.relpath(source, cible), os.path.join(cible, nom))
        shutil.copy2(cfg, os.path.join(cible, "pyvenv.cfg"))
        base = _interpreteur_de_base(venv, python)
        if base:
            os.symlink(base, os.path.join(cible, "bin", "python"))
        else:
            # venv `--copies` : le binaire est dans le venv stable, on le copie
            # (le lier bouclerait sur le lanceur installé juste après).
            shutil.copy2(python, os.path.join(cible, "bin", "python"))
        ancien = b"#!" + os.fsencode(python)
        nouveau = b"#!" + os.fsencode(os.path.join(cible, "bin", "python"))
        for nom in sorted(os.listdir(os.path.join(venv, "bin"))):
            source = os.path.join(venv, "bin", nom)
            if nom.startswith("python"):
                if nom != "python" and os.path.exists(source):
                    os.symlink("python", os.path.join(cible, "bin", nom))
                continue
            if os.path.islink(source) or not os.path.isfile(source):
                continue
            with open(source, "rb") as handle:
                data = handle.read()
            if not data.startswith(b"#!"):
                continue
            with open(os.path.join(cible, "bin", nom), "wb") as handle:
                handle.write(data.replace(ancien, nouveau, 1))
            os.chmod(os.path.join(cible, "bin", nom), os.stat(source).st_mode & 0o777)
    except OSError as exc:
        shutil.rmtree(cible, ignore_errors=True)
        _die("adoption du venv réel impossible : %s" % exc)
    with open(os.path.join(cible, MARKER), "w") as handle:
        json.dump({"version": version, "adopted": True, "from": venv, "at": time.time()}, handle)
    return cible


# --------------------------------------------------------------------------
# bascule et retour
# --------------------------------------------------------------------------

def activer(args):
    venv = os.path.abspath(args.venv)
    version = _version_name(args.version)
    if os.path.islink(venv):
        _die("« %s » est un lien : format d'avant #1969, convertir à la main" % venv)
    if not os.path.isdir(venv):
        _die("venv vivant introuvable : %s" % venv)
    cible = _version_dir(venv, version)
    if not os.path.isfile(os.path.join(cible, MARKER)):
        _die("venv-%s n'est pas préparé (lancer « preparer »)" % version)
    vivante = _live_target(venv)
    precedent = vivante
    if vivante is None:
        # première conversion : le venv réel ne bouge pas, son lib devient la
        # version précédente ; on pointe d'abord dessus (aucune fenêtre sans
        # vivant), puis on installe les lanceurs, puis on bascule. Si la
        # version quittée EST la version visée (relance sur le même SHA), le
        # venv réel est adopté sous ce nom : rien à basculer, aucun pip.
        nom = _version_name(args.precedent) if args.precedent else "precedent"
        if not os.path.isdir(_version_dir(venv, nom)):
            print("bascule: adoption du venv réel comme venv-%s (sans pip)" % nom)
            _adopter(venv, nom)
        precedent = None if nom == version else nom
        _pointer(venv, nom)
    perimes = _installer_lanceurs(venv, version)
    if _live_target(venv) == version:
        _retirer_lanceurs(venv, perimes)
        print("bascule: venv-%s déjà actif" % version)
        return 0
    if precedent and precedent != version:
        _write_report(venv, precedent)
    _pointer(venv, version)
    _retirer_lanceurs(venv, perimes)
    print("bascule: venv-%s actif%s" % (version, "" if not precedent else " (avant : %s)" % precedent))
    return 0


def retour(args):
    venv = os.path.abspath(args.venv)
    if os.path.islink(venv) or not os.path.isdir(venv):
        _die("venv vivant introuvable : %s" % venv)
    vivante = _live_target(venv)
    precedent = _read_report(venv)
    if not precedent:
        if vivante is None:
            # installation inachevée avant toute bascule : rien à défaire
            print("bascule: aucun basculement enregistré, le venv réel est déjà en place")
            return 0
        _die("aucune version précédente enregistrée (%s)" % _report_path(venv))
    cible = _version_dir(venv, precedent)
    if not os.path.isfile(os.path.join(cible, MARKER)):
        _die("version précédente venv-%s absente ou incomplète" % precedent)
    if vivante == precedent:
        print("bascule: déjà sur venv-%s (retour idempotent)" % precedent)
        return 0
    if not os.path.isfile(os.path.join(venv, LAUNCHERS)):
        _die("lanceurs absents de %s : activer une version d'abord" % venv)
    # #2155 : le retour est un changement de version comme un autre ; les
    # lanceurs de scripts console suivent la version vers laquelle on revient.
    perimes = _installer_lanceurs(venv, precedent)
    _pointer(venv, precedent)
    _retirer_lanceurs(venv, perimes)
    print("bascule: retour sur venv-%s (sans pip)" % precedent)
    return 0


def etat_de(venv):
    """État lisible : version vivante, précédente, venvs préparés, lanceurs."""
    base = _base(venv)
    vivante = _live_target(venv)
    if vivante is None and os.path.isdir(venv) and not os.path.islink(venv):
        vivante = "répertoire réel (avant première bascule)"
    versions = {}
    try:
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if not name.startswith("venv-") or name == "venv-precedent" \
                    or os.path.islink(path) or not os.path.isdir(path):
                continue
            versions[name[len("venv-"):]] = \
                "complete" if os.path.isfile(os.path.join(path, MARKER)) else "partiel"
    except OSError:
        pass
    return {"venv": os.path.abspath(venv), "vivante": vivante,
            "precedente": _read_report(venv),
            "lanceurs": os.path.isfile(os.path.join(venv, LAUNCHERS)),
            "versions": versions}


def _afficher_etat(args):
    etat = etat_de(args.venv)
    if args.json:
        print(json.dumps(etat, ensure_ascii=False, indent=2))
    else:
        print("vivante : %s" % (etat["vivante"] or "?"))
        print("précédente : %s" % (etat["precedente"] or "?"))
        print("lanceurs : %s" % ("oui" if etat["lanceurs"] else "non"))
        for version, state in sorted(etat["versions"].items()):
            print("  venv-%s : %s" % (version, state))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="bascule atomique du venv d'ameesh (#1969)")
    sub = parser.add_subparsers(dest="commande", required=True)
    p = sub.add_parser("preparer", help="venv versionné, pip et contrôles à l'écart")
    p.add_argument("--venv", required=True, help="chemin du venv vivant")
    p.add_argument("--src", required=True, help="répertoire du code à installer")
    p.add_argument("--version", required=True, help="nom de version (sha court)")
    p.add_argument("--python", default=os.environ.get("BASCULE_PYTHON") or "python3",
                   help="interpréteur qui crée le venv versionné (défaut : python3 du PATH)")
    p.set_defaults(func=preparer)
    p = sub.add_parser("activer", help="bascule atomique vers un venv préparé")
    p.add_argument("--venv", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--precedent", default=None,
                   help="nom de la version quittée lors de la première conversion "
                        "(défaut : « precedent »)")
    p.set_defaults(func=activer)
    p = sub.add_parser("retour", help="bascule en arrière, sans pip")
    p.add_argument("--venv", required=True)
    p.set_defaults(func=retour)
    p = sub.add_parser("etat", help="version vivante, précédente, préparées")
    p.add_argument("--venv", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_afficher_etat)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
