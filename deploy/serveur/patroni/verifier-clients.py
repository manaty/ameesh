#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Preuve côté client d'une bascule de la grappe (lot #6861, EP8.4).

Contre n'importe quelle grappe joignable par un DSN multi-hôtes (le banc
local #6902, la grappe de staging), ce script fait ce que font les clients
du mesh et mesure ce que l'acceptation d'EP8.4 exige :

- un écrivain insère une ligne numérotée toutes les `--pas` secondes, suivie
  d'un NOTIFY, par le DSN normalisé comme celui des clients
  (`ameesh.dsn.primary` : `target_session_attrs=read-write`) ; il note chaque
  COMMIT confirmé et se reconnecte après une coupure ;
- un écouteur LISTEN sur le même DSN qui, après chaque reconnexion, se
  réabonne puis balaie la table avant d'attendre : c'est le balayage du
  runner (#6869), qui remet un message envoyé pendant la bascule sans
  attendre le sondage ; l'écouteur n'a pas de sondage du tout ; après une
  coupure, il attend qu'un COMMIT de l'écrivain soit confirmé avant de se
  réabonner : la coupure contient ainsi toujours au moins un message, que
  seul le balayage peut remettre (#7117) ;
- `--tuer` lance la commande d'exploitant qui tue le primaire (le script ne
  touche aucun hôte lui-même) ; on mesure le délai jusqu'au premier COMMIT
  sur le nouveau primaire (cible 60 s, exigence 300 s), la reprise de
  l'écouteur, et le RPO : chaque COMMIT confirmé doit être sur le nouveau
  primaire.

Le split brain sous partition est l'affaire du banc (#6902, exercer.py) :
un client seul ne le voit pas.

Une table `verif_bascule_<hex>` est créée puis supprimée. Hors des hôtes
locaux (127.0.0.0/8, ::1, socket Unix), `--geste-reel` est exigé.

    verifier-clients.py --dsn 'postgresql://ameesh@h1,h2,h3/ameesh?sslmode=require' \\
        --tuer 'ssh root@h1 systemctl kill -s KILL patroni postgresql' \\
        --geste-reel --rapport rapport.json

Code de sortie : 0 acceptation tenue, 1 écart (détaillé), 2 usage ou
connexion initiale impossible.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parents[3]
if (RACINE / "src" / "ameesh" / "dsn.py").exists():
    sys.path.insert(0, str(RACINE / "src"))

SCHEMA = "ameesh-verif-clients/1"
CIBLE_S = 60.0
EXIGENCE_S = 300.0


def maintenant() -> float:
    return time.monotonic()


def hotes_locaux(dsn: str) -> bool:
    from ameesh import dsn as dsn_mod
    for hote in dsn_mod.hosts(dsn) or [""]:
        if not hote or hote.startswith("/") or hote == "localhost":
            continue
        try:
            if not ipaddress.ip_address(hote).is_loopback:
                return False
        except ValueError:
            return False
    return True


class Journal:
    def __init__(self):
        self.verrou = threading.Lock()
        self.debut = maintenant()
        self.evenements = []

    def note(self, qui: str, quoi: str, **detail):
        with self.verrou:
            self.evenements.append(dict(t=round(maintenant() - self.debut, 3),
                                        qui=qui, quoi=quoi, **detail))


class Ecrivain(threading.Thread):
    def __init__(self, connecter, table, canal, pas, journal, arret):
        super().__init__(daemon=True)
        self.connecter, self.table, self.canal = connecter, table, canal
        self.pas, self.journal, self.arret = pas, journal, arret
        self.confirmes = []          # (n, t_commit)
        self.serveurs = []           # (t, adresse) à chaque connexion
        self.premiere_erreur = None
        self.reprise = None
        self.erreur_fatale = None

    def run(self):
        n, conn = 0, None
        while not self.arret.is_set():
            try:
                if conn is None:
                    conn = self.connecter()
                    adresse = conn.execute(
                        "SELECT coalesce(host(inet_server_addr()), 'socket') || ':' || "
                        "current_setting('port')").fetchone()[0]
                    self.serveurs.append((maintenant(), adresse))
                    self.journal.note("ecrivain", "connecte", serveur=adresse)
                n += 1
                with conn.transaction():
                    conn.execute("INSERT INTO %s (n) VALUES (%%s)" % self.table, (n,))
                    conn.execute("SELECT pg_notify(%s, %s)", (self.canal, str(n)))
                t = maintenant()
                self.confirmes.append((n, t))
                if self.premiere_erreur is not None and self.reprise is None:
                    self.reprise = t
                    self.journal.note("ecrivain", "reprise", n=n)
            except Exception as exc:  # coupure : on ne sait pas si n est commité
                if self.premiere_erreur is None:
                    self.premiere_erreur = maintenant()
                    self.journal.note("ecrivain", "coupure", erreur=type(exc).__name__)
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                self.arret.wait(0.2)
                continue
            self.arret.wait(self.pas)
        if conn is not None:
            conn.close()


class Ecouteur(threading.Thread):
    def __init__(self, connecter, table, canal, journal, arret, ecrivain):
        super().__init__(daemon=True)
        self.connecter, self.table, self.canal = connecter, table, canal
        self.journal, self.arret, self.ecrivain = journal, arret, ecrivain
        self.vus = {}                # n -> (t_vu, par « notify » ou « balayage »)
        self.abonnements = []        # t de chaque LISTEN réussi
        self.coupures = []

    def _voir(self, n, t, par):
        self.vus.setdefault(n, (t, par))

    def _attendre_un_commit(self, depuis):
        # Synchronisation du témoin : un COMMIT confirmé après la coupure et
        # avant le LISTEN, dont le NOTIFY est perdu pour cet écouteur.
        confirmes = self.ecrivain.confirmes
        while not self.arret.is_set() and not (confirmes and confirmes[-1][1] > depuis):
            self.arret.wait(0.05)

    def run(self):
        attente = 0.2
        while not self.arret.is_set():
            conn = None
            try:
                conn = self.connecter()
                conn.execute("LISTEN %s" % self.canal)
                t = maintenant()
                self.abonnements.append(t)
                # Balayage après chaque (ré)abonnement, avant d'attendre :
                # les NOTIFY émis pendant la coupure ne seront jamais remis.
                for (n,) in conn.execute("SELECT n FROM %s" % self.table).fetchall():
                    self._voir(n, t, "balayage")
                self.journal.note("ecouteur", "abonne", vus=len(self.vus))
                attente = 0.2
                while not self.arret.is_set():
                    for notif in conn.notifies(timeout=0.5, stop_after=64):
                        self._voir(int(notif.payload), maintenant(), "notify")
            except Exception as exc:
                t_coupure = maintenant()
                self.coupures.append(t_coupure)
                self.journal.note("ecouteur", "coupure", erreur=type(exc).__name__)
                self.arret.wait(attente)
                self._attendre_un_commit(t_coupure)
                attente = min(attente * 2, 2.0)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


def evaluer(ecrivain, ecouteur, presents, t_tuer, cible, exigence):
    ecarts, mesures = [], {}
    if ecrivain.premiere_erreur is None or ecrivain.reprise is None:
        ecarts.append("l'écrivain n'a vu ni coupure ni reprise : la bascule n'a pas eu lieu")
    elif ecrivain.premiere_erreur < t_tuer or ecrivain.reprise <= t_tuer:
        ecarts.append("coupure de l'écrivain antérieure à --tuer : la mesure ne porte pas sur cette bascule")
    else:
        delai = ecrivain.reprise - t_tuer
        mesures["delai_nouveau_primaire_s"] = round(delai, 3)
        if delai > exigence:
            ecarts.append("nouveau primaire après %.1f s (exigence %.0f s)" % (delai, exigence))
        elif delai > cible:
            mesures["au_dela_de_la_cible"] = True
    adresses = [a for _t, a in ecrivain.serveurs]
    mesures["serveurs_ecrivain"] = adresses
    if len(set(adresses)) < 2:
        ecarts.append("l'écrivain n'a pas changé de serveur : %s" % (adresses,))
    confirmes = [n for n, _t in ecrivain.confirmes]
    perdus = sorted(set(confirmes) - set(presents))
    mesures["commits_confirmes"] = len(confirmes)
    mesures["rpo_lignes_perdues"] = len(perdus)
    if perdus:
        ecarts.append("RPO non nul : COMMIT confirmés absents du nouveau primaire : %s" % perdus[:20])
    non_vus = sorted(set(confirmes) - set(ecouteur.vus))
    if non_vus:
        ecarts.append("l'écouteur n'a jamais vu : %s" % non_vus[:20])
    apres = [t for t in ecouteur.abonnements if t > t_tuer]
    if not apres:
        ecarts.append("l'écouteur ne s'est pas réabonné après la bascule")
    else:
        mesures["reprise_ecouteur_s"] = round(apres[0] - t_tuer, 3)
        # Messages confirmés pendant que l'écouteur était coupé : remis par
        # le balayage au réabonnement, pas par un sondage.
        coupure = min([t for t in ecouteur.coupures if t >= t_tuer] or [apres[0]])
        pendant = [n for n, t in ecrivain.confirmes if coupure <= t < apres[0]]
        mesures["messages_pendant_la_coupure"] = len(pendant)
        if not pendant:
            ecarts.append("aucun COMMIT confirmé pendant la coupure de l'écouteur : "
                          "le balayage n'est pas prouvé")
        mal_remis = [n for n in pendant if ecouteur.vus.get(n, (0, ""))[1] != "balayage"]
        if mal_remis:
            ecarts.append("messages de la coupure non remis par le balayage : %s" % mal_remis[:20])
    return ecarts, mesures


def main(argv=None) -> int:
    analyseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    analyseur.add_argument("--dsn", default=os.environ.get("AMEESH_DSN"),
                           help="DSN multi-hôtes (défaut : AMEESH_DSN)")
    analyseur.add_argument("--tuer", required=True,
                           help="commande d'exploitant qui tue le primaire")
    analyseur.add_argument("--geste-reel", action="store_true",
                           help="exigé hors des hôtes locaux")
    analyseur.add_argument("--pas", type=float, default=0.2)
    analyseur.add_argument("--avant", type=float, default=3.0,
                           help="écritures avant la bascule (s)")
    analyseur.add_argument("--apres", type=float, default=5.0,
                           help="écritures après la reprise (s)")
    analyseur.add_argument("--cible", type=float, default=CIBLE_S)
    analyseur.add_argument("--exigence", type=float, default=EXIGENCE_S)
    analyseur.add_argument("--connect-timeout", type=int, default=5)
    analyseur.add_argument("--rapport", help="rapport JSON")
    args = analyseur.parse_args(argv)
    if not args.dsn:
        analyseur.error("--dsn ou AMEESH_DSN requis")
    try:
        import psycopg
        from ameesh import dsn as dsn_mod
    except ImportError as exc:
        print("verifier-clients : %s (venv ameesh attendu)" % exc, file=sys.stderr)
        return 2
    dsn = dsn_mod.primary(args.dsn)
    if not args.geste_reel and not hotes_locaux(dsn):
        print("verifier-clients : hôtes non locaux, --geste-reel exigé", file=sys.stderr)
        return 2

    def connecter():
        return psycopg.connect(dsn, autocommit=True, connect_timeout=args.connect_timeout)

    suffixe = os.urandom(4).hex()
    table, canal = "verif_bascule_%s" % suffixe, "verif_bascule_%s" % suffixe
    try:
        with connecter() as conn:
            conn.execute("CREATE TABLE %s (n integer PRIMARY KEY, t timestamptz DEFAULT now())" % table)
    except Exception as exc:
        print("verifier-clients : connexion initiale impossible : %s" % exc, file=sys.stderr)
        return 2

    journal, arret = Journal(), threading.Event()
    ecrivain = Ecrivain(connecter, table, canal, args.pas, journal, arret)
    ecouteur = Ecouteur(connecter, table, canal, journal, arret, ecrivain)
    ecouteur.start()
    ecrivain.start()
    code_tuer = None
    t_tuer = maintenant()
    try:
        time.sleep(args.avant)
        t_tuer = maintenant()
        journal.note("exploitant", "tuer", commande=args.tuer)
        code_tuer = subprocess.run(shlex.split(args.tuer), timeout=args.exigence).returncode
        journal.note("exploitant", "tue", code=code_tuer)
        limite = t_tuer + args.exigence
        while ecrivain.reprise is None and maintenant() < limite:
            time.sleep(0.1)
        time.sleep(args.apres)
    finally:
        arret.set()
        ecrivain.join(10)
        ecouteur.join(10)
    presents, erreur_lecture = [], None
    try:
        with connecter() as conn:
            presents = [n for (n,) in conn.execute("SELECT n FROM %s" % table).fetchall()]
            conn.execute("DROP TABLE %s" % table)
    except Exception as exc:
        erreur_lecture = str(exc)
    ecarts, mesures = evaluer(ecrivain, ecouteur, presents, t_tuer,
                              args.cible, args.exigence)
    if code_tuer:
        ecarts.append("la commande --tuer a rendu %s" % code_tuer)
    if erreur_lecture:
        ecarts.append("relecture finale impossible (table %s laissée) : %s" % (table, erreur_lecture))
    rapport = {"schema": SCHEMA, "ok": not ecarts, "ecarts": ecarts,
               "mesures": mesures, "cible_s": args.cible, "exigence_s": args.exigence,
               "evenements": journal.evenements}
    if args.rapport:
        Path(args.rapport).write_text(json.dumps(rapport, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    for cle, valeur in mesures.items():
        print("%s : %s" % (cle, valeur))
    for ecart in ecarts:
        print("ÉCART : %s" % ecart)
    print("acceptation tenue" if not ecarts else "acceptation NON tenue")
    return 0 if not ecarts else 1


if __name__ == "__main__":
    sys.exit(main())
