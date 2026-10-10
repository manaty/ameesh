# SPDX-License-Identifier: AGPL-3.0-only
"""Essai de bout en bout de la voie B (lot L115), sur une machine de tests.

    .venv/bin/python scripts/essai-voie-b/essai.py [--dsh npm|faux] [--garder]

Monte, sur la machine où il tourne (Docker requis, aucun secret) :

* un Postgres jetable (`scripts/pg-up.sh`, nom et port propres à l'essai) ;
* un canon d'essai (hôte `banc`, agent `ouvrier` sur dsh), une « forge »
  locale (dépôt nu) pour le dépôt de travail ;
* un faux fournisseur DeepSeek compatible Anthropic Messages ;
* `ameesh serve --exec-only` (API d'exécuteur, identité L110, relais L111,
  dépôt de travail L113) en TLS sur la passerelle Docker ;
* l'exécuteur médié dans un conteneur qui imite la VM Compute : volume
  `/var/lib/ameesh-exec`, porte `/run/ameesh-gate`, code dans
  `/run/ameesh-enroll/code`, point d'entrée `ameesh-executor`.

Puis enchaîne : enrôlement, tour d'agent, remise du courrier, coût relevé
par le relais (`source=relay`), retrait (`draining`) et restitution de
l'agent, retour (`available`) et reprise, révocation (sortie 6). Mesure les
appels HTTP au serveur par tour et la latence. Résultat : `resultat.json`
dans le dossier de l'essai, et un résumé sur la sortie standard.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
NOM = "essai-vb"
PG_PORT = 55620
SRV_PORT = 18470
FAUX_PORT = 18471
PASSERELLE = "172.17.0.1"
IMAGE = "ameesh-executor:essai"
EXEC = NOM + "-exec"
VOLUME = NOM + "-vol"
PG = "ameesh-pg-" + NOM
AGENT = "ouvrier"
HOTE = "banc"
URL = "https://%s:%d" % (PASSERELLE, SRV_PORT)


def horodate(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def run(cmd, *, env=None, check=True, capture=True, timeout=600, cwd=None):
    proc = subprocess.run(cmd, env=env, cwd=cwd or REPO, text=True, timeout=timeout,
                          capture_output=capture)
    if check and proc.returncode != 0:
        raise RuntimeError("%s : code %d\n%s\n%s" % (" ".join(cmd), proc.returncode,
                                                      proc.stdout[-2000:], proc.stderr[-2000:]))
    return proc


def attendre(cond, timeout: float, pas: float = 0.5, quoi: str = ""):
    fin = time.time() + timeout
    while time.time() < fin:
        val = cond()
        if val:
            return val
        time.sleep(pas)
    raise TimeoutError("délai dépassé : %s" % quoi)


class Essai:
    def __init__(self, args):
        self.args = args
        self.dir = os.path.join(args.dossier, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(self.dir)
        self.procs = []
        self.etapes = collections.OrderedDict()
        self.mesures = {}
        self.env = dict(os.environ)
        for k in ("AMEESH_DSN", "AGENT_MESH_DSN", "AGENT_MAIL_NAME", "AMEESH_RUNNER_ID"):
            self.env.pop(k, None)
        self.env.update({
            "AMEESH_PG_NAME": PG, "AMEESH_PG_PORT": str(PG_PORT),
            "AMEESH_PG_VOLUME": "ameesh-pg-data-" + NOM, "AMEESH_PG_PASSWORD": "banc-" + NOM,
            "PGPASSWORD": "banc-" + NOM,
            "AMEESH_DSN": "postgresql://agent_mesh@127.0.0.1:%d/agent_mesh" % PG_PORT,
            "AMEESH_CONFIG": os.path.join(self.dir, "config-absente.json"),
            "PYTHONPATH": os.path.join(REPO, "src"),
            "AMEESH_BALANCE_INTERVAL": "0", "AMEESH_RESOURCE_INTERVAL": "0",
        })

    # -- outils --------------------------------------------------------------------
    def ameesh(self, *args, env=None, check=True):
        e = dict(self.env, **(env or {}))
        return run([sys.executable, "-m", "ameesh.main", *args], env=e, check=check)

    def sql(self, query: str) -> list:
        proc = run(["docker", "exec", PG, "psql", "-U", "agent_mesh", "-d", "agent_mesh",
                    "-tAF", "\t", "-c", query])
        return [line.split("\t") for line in proc.stdout.splitlines() if line.strip()]

    def etape(self, nom: str, ok: bool, **details):
        self.etapes[nom] = dict(ok=bool(ok), **details)
        horodate("%s %s %s" % ("OK" if ok else "KO", nom,
                               json.dumps(details, ensure_ascii=False)[:400]))

    # -- mise en place ---------------------------------------------------------------
    def nettoyer(self):
        for cmd in (["docker", "rm", "-f", EXEC], ["docker", "volume", "rm", VOLUME],
                    ["docker", "rm", "-f", PG],
                    ["docker", "volume", "rm", "ameesh-pg-data-" + NOM]):
            run(cmd, check=False)

    def base(self):
        run(["./scripts/pg-up.sh"], env=self.env, timeout=300)
        attendre(lambda: run(["docker", "exec", PG, "pg_isready", "-U", "agent_mesh"],
                             check=False).returncode == 0, 120, 1, "Postgres")
        attendre(lambda: self.ameesh("migrate", check=False).returncode == 0, 60, 2,
                 "migrations")

    def canon(self):
        root = os.path.join(self.dir, "canon")
        shutil.copytree(os.path.join(REPO, "examples", "canon"), root)
        with open(os.path.join(root, "hotes", "banc.md"), "w", encoding="utf-8") as fh:
            fh.write("""---
type: Host
title: banc
description: "Appareil prêté de l'essai L115 (exécuteur médié)."
responsible: human:alice
volatile: true
policy:
  harnesses: [deepseek]
  providers: [deepseek]
  credential_modes: [api-key]
  models: ["deepseek-*"]
  max_agents: 1
---

# banc
""")
        self.env.update({"AMEESH_CANON": root, "AMEESH_CANON_UNTRUSTED": "1"})
        return root

    def forge(self):
        forge = os.path.join(self.dir, "forge.git")
        src = os.path.join(self.dir, "forge-src")
        ident = dict(self.env, GIT_AUTHOR_NAME="essai", GIT_AUTHOR_EMAIL="essai@invalid",
                     GIT_COMMITTER_NAME="essai", GIT_COMMITTER_EMAIL="essai@invalid")
        run(["git", "init", "-q", "--bare", "-b", "main", forge])
        run(["git", "init", "-q", "-b", "main", src])
        with open(os.path.join(src, "README.md"), "w") as fh:
            fh.write("# acme-web (essai)\n")
        run(["git", "-C", src, "add", "-A"], env=ident)
        run(["git", "-C", src, "commit", "-q", "-m", "départ"], env=ident)
        run(["git", "-C", src, "push", "-q", forge, "main"], env=ident)
        repos = os.path.join(self.dir, "work-repos.json")
        with open(repos, "w") as fh:
            json.dump({"default": {"repo": forge, "base": "main"}}, fh)
        return forge, repos

    def tls(self):
        d = os.path.join(self.dir, "ca")
        os.makedirs(d)
        run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-days", "2", "-subj", "/CN=ameesh-essai",
             "-addext", "subjectAltName=IP:%s" % PASSERELLE,
             "-keyout", os.path.join(d, "key.pem"), "-out", os.path.join(d, "ca.pem")])
        os.chmod(os.path.join(d, "ca.pem"), 0o600)
        os.chmod(os.path.join(d, "key.pem"), 0o600)
        # copie publique du certificat, seule montée dans le conteneur
        pub = os.path.join(self.dir, "ca-public")
        os.makedirs(pub)
        shutil.copy(os.path.join(d, "ca.pem"), os.path.join(pub, "ca.pem"))
        os.chmod(pub, 0o755)
        os.chmod(os.path.join(pub, "ca.pem"), 0o644)
        self.ca_public = pub
        return d

    def demarrer_faux(self):
        self.faux_log = os.path.join(self.dir, "fournisseur.jsonl")
        open(self.faux_log, "w").close()
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "faux_fournisseur.py"),
                              str(FAUX_PORT), self.faux_log])
        self.procs.append(p)

    def demarrer_serveur(self, ca, repos):
        self.srv_log = os.path.join(self.dir, "serveur.log")
        env = dict(self.env, DEEPSEEK_API_KEY="cle-factice-essai",
                   AMEESH_RELAY_DEEPSEEK_UPSTREAM="http://127.0.0.1:%d" % FAUX_PORT)
        fh = open(self.srv_log, "w")
        p = subprocess.Popen(
            [sys.executable, "-m", "ameesh.main", "serve", "--exec-only",
             "--listen", "%s:%d" % (PASSERELLE, SRV_PORT), "--server-url", URL,
             "--mesh", "essai", "--tls-cert", os.path.join(ca, "ca.pem"),
             "--tls-key", os.path.join(ca, "key.pem"), "--canon-sync", "3",
             "--lease-ttl", "60", "--lease-renew", "20",
             "--work-repos", repos, "--work-cache", os.path.join(self.dir, "depot-cache")],
            env=env, stdout=fh, stderr=subprocess.STDOUT, cwd=REPO)
        self.procs.append(p)
        attendre(lambda: run(["curl", "-sf", "--cacert", os.path.join(ca, "ca.pem"),
                              URL + "/api/exec/v1/health"], check=False).returncode == 0,
                 30, 0.5, "serveur")

    def image(self):
        if self.args.reconstruire or run(["docker", "image", "inspect", IMAGE],
                                         check=False).returncode != 0:
            horodate("construction de l'image (dsh %s)" % self.args.dsh)
            run(["docker", "build", "-q", "-f", os.path.join(HERE, "Containerfile"),
                 "--build-arg", "DSH=%s" % self.args.dsh, "-t", IMAGE, "."], timeout=1800)

    def porte(self, state: str, seq: int):
        path = os.path.join(self.gate, "state", "state.json")
        with open(path + ".tmp", "w") as fh:
            json.dump({"schema": "ameesh-host-state/1", "state": state, "seq": seq,
                       "until_ts": None, "drain_deadline_ts": None,
                       "caps": {"max_concurrent": 1}, "reason": "essai"}, fh)
        os.chmod(path + ".tmp", 0o644)
        os.replace(path + ".tmp", path)

    def ack(self) -> dict:
        try:
            with open(os.path.join(self.gate, "ack", "ack.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def demarrer_executeur(self, ca, code: str):
        self.gate = os.path.join(self.dir, "gate")
        enroll = os.path.join(self.dir, "enroll")
        for d in (os.path.join(self.gate, "state"), os.path.join(self.gate, "ack"), enroll):
            os.makedirs(d)
        os.chmod(self.gate, 0o755)
        os.chmod(os.path.join(self.gate, "state"), 0o755)
        os.chown(os.path.join(self.gate, "ack"), 10001, 10001)
        with open(os.path.join(enroll, "code"), "w") as fh:
            fh.write(code + "\n")
        os.chown(os.path.join(enroll, "code"), 10001, 10001)
        os.chmod(os.path.join(enroll, "code"), 0o400)
        os.chmod(enroll, 0o755)
        self.porte("available", 1)
        run(["docker", "run", "-d", "--name", EXEC,
             "-v", "%s:/var/lib/ameesh-exec" % VOLUME,
             "-v", "%s:/run/ameesh-gate/state:ro" % os.path.join(self.gate, "state"),
             "-v", "%s:/run/ameesh-gate/ack" % os.path.join(self.gate, "ack"),
             "-v", "%s:/run/ameesh-enroll:ro" % enroll,
             "-v", "%s:/run/ameesh-ca:ro" % self.ca_public,
             "-e", "AMEESH_EXEC_URL=" + URL, "-e", "AMEESH_EXEC_HOST=" + HOTE,
             "-e", "SSL_CERT_FILE=/run/ameesh-ca/ca.pem",
             "-e", "NODE_EXTRA_CA_CERTS=/run/ameesh-ca/ca.pem",
             "-e", "AMEESH_EXEC_LABEL=essai-l115",
             IMAGE, "run", "--poll", "2"])

    # -- mesures ----------------------------------------------------------------------
    _REQ = re.compile(r'^(\S+ \S+) ameesh\.exec "(\w+) (\S+) HTTP')

    def requetes(self, t0: float, t1: float) -> dict:
        """Requêtes reçues par le serveur entre t0 et t1, par route."""
        compte = collections.Counter()
        with open(self.srv_log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = self._REQ.match(line)
                if not m:
                    continue
                ts = time.mktime(time.strptime(m.group(1).split(",")[0], "%Y-%m-%d %H:%M:%S"))
                ts += int(m.group(1).split(",")[1]) / 1000.0
                if t0 <= ts <= t1:
                    path = m.group(3).split("?")[0]
                    path = re.sub(r"/work/[^/]+/", "/work/{agent}/", path)
                    compte["%s %s" % (m.group(2), path)] += 1
        return dict(compte)

    def latence_sante(self) -> dict:
        code = ("import time,ssl,urllib.request as u\n"
                "c=ssl.create_default_context(cafile='/run/ameesh-ca/ca.pem')\n"
                "d=[]\n"
                "for i in range(20):\n"
                "  t=time.perf_counter();u.urlopen('%s/api/exec/v1/health',context=c).read();"
                "d.append((time.perf_counter()-t)*1000)\n"
                "print(' '.join('%%.2f'%%x for x in d))\n" % URL)
        out = run(["docker", "exec", EXEC, "python3", "-c", code]).stdout.split()
        vals = [float(x) for x in out]
        return {"n": len(vals), "mediane_ms": round(statistics.median(vals), 2),
                "max_ms": round(max(vals), 2)}

    def tour(self, texte: str, nom: str) -> dict:
        avant = int(self.sql("SELECT count(*) FROM turn_costs WHERE agent='%s' AND "
                             "source='relay'" % AGENT)[0][0])
        t0 = time.time()
        self.ameesh("mail", "send", AGENT, texte, "--from", "alice")
        mid = int(self.sql("SELECT max(id) FROM agent_mailbox WHERE recipient='%s'"
                           % AGENT)[0][0])

        def remis():
            rows = self.sql("SELECT extract(epoch from delivered_at) FROM agent_mailbox "
                            "WHERE id=%d AND delivered_at IS NOT NULL" % mid)
            return float(rows[0][0]) if rows else None
        t_remis = attendre(remis, 300, 0.5, "remise du courrier (%s)" % nom)

        def cout():
            rows = self.sql("SELECT count(*), max(extract(epoch from recorded_at)) FROM "
                            "turn_costs WHERE agent='%s' AND source='relay'" % AGENT)
            return float(rows[0][1]) if int(rows[0][0]) > avant else None
        t_cout = attendre(cout, 300, 0.5, "coût relevé (%s)" % nom)

        def fini():
            rows = self.sql("SELECT status, extract(epoch from last_turn_at) FROM "
                            "agent_registry WHERE name='%s'" % AGENT)
            return rows and rows[0][0] in ("idle", "queued") and rows[0][1] \
                and float(rows[0][1]) >= t0 and float(rows[0][1])
        t_fin = attendre(fini, 300, 0.5, "fin du tour (%s)" % nom)
        time.sleep(1.5)  # derniers renouvellements et envois
        reqs = self.requetes(t0, float(t_fin) + 1.0)
        return {"mail_id": mid, "remise_s": round(t_remis - t0, 2),
                "cout_s": round(t_cout - t0, 2), "tour_s": round(float(t_fin) - t0, 2),
                "appels_http": sum(reqs.values()), "par_route": reqs}

    # -- scénario ---------------------------------------------------------------------------
    def scenario(self):
        self.nettoyer()
        horodate("mise en place : base, canon, forge, TLS, faux fournisseur, serveur")
        self.base()
        self.canon()
        forge, repos = self.forge()
        ca = self.tls()
        self.demarrer_faux()
        self.demarrer_serveur(ca, repos)
        run([sys.executable, "-m", "ameesh.runner", "register", AGENT, "deepseek",
             "--model", "deepseek-chat"], env=dict(self.env, AMEESH_HOST=HOTE))
        invit = json.loads(self.ameesh("host", "enroll", HOTE, "--by", "human:alice",
                                       "--mesh", "essai", "--agents", AGENT, "--json").stdout)
        self.image()
        horodate("démarrage de l'exécuteur (conteneur %s)" % EXEC)
        t0 = time.time()
        self.demarrer_executeur(ca, invit["code"])

        # 1. enrôlement
        try:
            rows = attendre(lambda: self.sql("SELECT id, host FROM executors"), 120, 1,
                            "enrôlement")
            show = json.loads(run(["docker", "exec", EXEC, "ameesh-executor", "show"]).stdout)
            code_lu = run(["docker", "exec", EXEC, "sh", "-c",
                           "grep -rl %s /var/lib/ameesh-exec || true" % invit["code"]]).stdout
            self.etape("1-enrolement", rows[0][1] == HOTE and show.get("host") == HOTE
                       and not code_lu.strip(), executeur=rows[0][0],
                       duree_s=round(time.time() - t0, 2), code_sur_volume=bool(code_lu.strip()))
        except Exception as exc:
            self.etape("1-enrolement", False, erreur=str(exc)[:500])
            raise
        attendre(lambda: (self.ack().get("state") == "available"), 60, 0.5, "acquittement")

        # 2-4. tour, remise, coût
        try:
            t = self.tour("Essai L115 : réponds simplement.", "tour 1")
            self.mesures["tour1"] = t
            prov = [json.loads(l) for l in open(self.faux_log) if l.strip()]
            self.etape("2-tour", True, harnais=self.args.dsh, requetes_fournisseur=len(prov),
                       **{k: t[k] for k in ("tour_s", "appels_http")})
        except Exception as exc:
            self.etape("2-tour", False, erreur=str(exc)[:500])
            raise
        self.etape("3-remise", t["remise_s"] is not None, mail=t["mail_id"],
                   remise_s=t["remise_s"])
        rows = self.sql("SELECT source, count(*), round(sum(usd)::numeric, 6), "
                        "max(lease_epoch), max(executor) FROM turn_costs WHERE agent='%s' "
                        "GROUP BY source" % AGENT)
        sources = {r[0]: r[1:] for r in rows}
        self.etape("4-cout-relay", "relay" in sources, lignes=sources)

        # 5. retrait
        try:
            t5 = time.time()
            self.porte("draining", 2)

            def rendu():
                a = self.ack()
                rows = self.sql("SELECT coalesce(lease_owner, ''), status FROM agent_registry "
                                "WHERE name='%s'" % AGENT)
                return a.get("seq") == 2 and a.get("drained") and rows and rows[0][0] == ""
            attendre(rendu, 150, 0.5, "restitution")
            reste = run(["docker", "exec", EXEC, "sh", "-c",
                         "ls -A /var/lib/ameesh-exec/work/%s 2>/dev/null | wc -l" % AGENT]
                        ).stdout.strip()
            dispo = self.sql("SELECT 1") and True
            self.etape("5-retrait", reste == "0", restitution_s=round(time.time() - t5, 2),
                       dossiers_restants=int(reste or 0), ack=self.ack(), serveur_ok=dispo)
        except Exception as exc:
            self.etape("5-retrait", False, erreur=str(exc)[:500], ack=self.ack())

        # 6. retour et reprise
        try:
            self.porte("available", 3)
            attendre(lambda: self.ack().get("seq") == 3, 60, 0.5, "acquittement available")
            t = self.tour("Essai L115 : second tour après retour.", "tour 2")
            self.mesures["tour2"] = t
            self.etape("6-reprise", True, **{k: t[k] for k in ("tour_s", "appels_http",
                                                               "remise_s")})
        except Exception as exc:
            self.etape("6-reprise", False, erreur=str(exc)[:500])

        try:
            self.mesures["sante"] = self.latence_sante()
        except Exception as exc:
            self.mesures["sante"] = {"erreur": str(exc)[:300]}

        # 7. révocation
        try:
            t7 = time.time()
            self.ameesh("host", "revoke", HOTE, "--by", "human:alice", "--why", "fin de l'essai")

            def sorti():
                p = run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}",
                         EXEC], check=False).stdout.split()
                return p and p[0] == "false" and p
            p = attendre(sorti, 120, 0.5, "arrêt après révocation")
            self.etape("7-revocation", p[1] == "6", code_de_sortie=int(p[1]),
                       delai_s=round(time.time() - t7, 2))
        except Exception as exc:
            self.etape("7-revocation", False, erreur=str(exc)[:500])

    def fin(self):
        run(["docker", "logs", EXEC], check=False)
        logs = run(["docker", "logs", EXEC], check=False)
        with open(os.path.join(self.dir, "executeur.log"), "w") as fh:
            fh.write(logs.stdout + logs.stderr)
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        resultat = {"etapes": self.etapes, "mesures": self.mesures, "dossier": self.dir,
                    "dsh": self.args.dsh}
        with open(os.path.join(self.dir, "resultat.json"), "w") as fh:
            json.dump(resultat, fh, indent=1, ensure_ascii=False)
        print(json.dumps(resultat, indent=1, ensure_ascii=False))
        if not self.args.garder:
            self.nettoyer()
        return all(e["ok"] for e in self.etapes.values()) and len(self.etapes) == 7


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dsh", choices=("npm", "faux"), default="npm")
    p.add_argument("--reconstruire", action="store_true")
    p.add_argument("--garder", action="store_true", help="garder conteneurs et base")
    p.add_argument("--dossier", default="/srv/ameesh/essai")
    args = p.parse_args()
    essai = Essai(args)
    try:
        essai.scenario()
    except Exception as exc:
        horodate("essai interrompu : %s" % str(exc)[:1500])
    return 0 if essai.fin() else 1


if __name__ == "__main__":
    sys.exit(main())
