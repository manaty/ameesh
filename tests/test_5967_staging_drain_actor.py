# SPDX-License-Identifier: AGPL-3.0-only
"""#5967 (#5495, phase 2) — l'acteur machine `ameesh:staging` du drain #2152.

L'exécuteur élu du staging lance vm.sh en service : son drain ne peut ni se
dire humain ni fabriquer human:<owner>. Contrat prouvé ici :

- `agent_pause.change` admet `ameesh:staging` avec un principal humain, pour
  la seule pose du drain (motif exact, barrière not_after, plafond par défaut)
  et sa seule levée conditionnelle ; tout autre geste est refusé ;
- le CLI (`pause/resume --operator --actor ameesh:staging`) refuse `--human`
  (le principal est l'owner de la configuration) et toute session d'agent ;
- vm.sh n'admet AMEESH_OPERATEUR=ameesh:staging que dans le cgroup de
  l'unité ameesh-staging.service, hors tour d'agent, et n'envoie `--actor`
  qu'à un CLI de cible qui le connaît ; sinon arrêt prouvé sans drain.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from ameesh import agent_pause, registry, serve

from .support import PgTestCase

ROOT = Path(__file__).resolve().parents[1]
VM_SH = ROOT / "deploy/mise-a-jour/vm.sh"
ACTOR = agent_pause.STAGING_ACTOR
MOTIF = "drain de déploiement #2152 (vm.sh agents redemarrer sur ameesh-agents-1) [ag3-412-77]"
REPRISE = ("reprise après drain de déploiement #2152 (vm.sh agents redemarrer sur "
           "ameesh-agents-1) : redémarrage hors tour effectué")


class StagingActorContractTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "worker", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)

    def pose(self, **kw):
        args = dict(paused=True, reason=MOTIF, actor=ACTOR, principal="human:alice",
                    not_after=time.time() + 60)
        args.update(kw)
        return agent_pause.change(self.db, "worker", **args)

    def leve(self, **kw):
        args = dict(paused=False, reason=REPRISE, actor=ACTOR, principal="human:alice",
                    expect_reason=MOTIF)
        args.update(kw)
        return agent_pause.change(self.db, "worker", **args)

    def events(self):
        return self.db.query("SELECT paused, actor, principal FROM operator_pause_events"
                             " WHERE agent = 'worker' ORDER BY id")

    def test_the_drain_is_set_and_lifted_by_the_machine_actor_for_the_owner(self):
        out = self.pose()
        self.assertTrue(out["changed"])
        self.assertTrue(out["capped"])
        row = registry.get(self.db, "worker")
        self.assertEqual((row["operator_pause_by"], row["operator_pause_for"]),
                         (ACTOR, "human:alice"))
        self.assertTrue(self.leve()["changed"])
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        self.assertEqual([tuple(e.values()) for e in self.events()],
                         [(True, ACTOR, "human:alice"), (False, ACTOR, "human:alice")])

    def test_any_other_gesture_of_the_machine_actor_is_refused(self):
        refus = (
            ("hors motif", dict(reason="maintenance")),
            ("motif d'un autre ticket", dict(reason=MOTIF.replace("#2152", "#2153"))),
            ("motif sans jeton", dict(reason=MOTIF.rsplit(" [", 1)[0])),
            ("sans barrière", dict(not_after=None)),
            ("échéance choisie", dict(until=time.time() + 600)),
            ("pause sans fin", dict(indefinite=True)),
            ("principal machine", dict(principal=ACTOR)),
            ("principal absent", dict(principal=None)),
        )
        for nom, kw in refus:
            with self.subTest(nom), self.assertRaises(agent_pause.PauseRefused):
                self.pose(**kw)
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        # Une pause humaine en place n'est jamais levée par l'acteur machine
        # hors de sa levée conditionnelle exacte.
        agent_pause.change(self.db, "worker", paused=True, reason="maintenance",
                           actor="human:alice")
        for nom, kw in (("levée sans condition", dict(expect_reason=None)),
                        ("condition étrangère", dict(expect_reason="maintenance")),
                        ("raison libre", dict(reason="reprise"))):
            with self.subTest(nom), self.assertRaises(agent_pause.PauseRefused):
                self.leve(**kw)
        self.assertFalse(self.leve()["changed"])
        self.assertTrue(registry.get(self.db, "worker")["operator_paused"])

    def test_a_capped_drain_still_expires_under_the_machine_actor(self):
        self.pose()
        self.db.execute("UPDATE agent_registry SET operator_pause_until ="
                        " clock_timestamp() - interval '1 second' WHERE name = 'worker'")
        self.assertEqual([r["agent"] for r in agent_pause.expire(self.db, "worker")],
                         ["worker"])
        self.assertEqual(tuple(self.events()[-1].values()), (False, ACTOR, "human:alice"))

    def test_the_database_still_refuses_any_other_machine_actor(self):
        with self.assertRaises(Exception):
            self.db.query("SELECT * FROM ameesh_operator_pause('worker', true, 'x',"
                          " 'ameesh:other', 'human:alice')")

    # -- CLI ----------------------------------------------------------------
    def test_cli_drain_takes_the_owner_of_the_configuration_as_principal(self):
        out = self.mesh("pause", "worker", "--actor", ACTOR, "--reason", MOTIF,
                        "--not-after", str(time.time() + 60), "--json")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(json.loads(out.stdout)["changed"])
        row = registry.get(self.db, "worker")
        self.assertEqual((row["operator_pause_by"], row["operator_pause_for"]),
                         (ACTOR, serve.owner(self.cfg)))
        out = self.mesh("resume", "worker", "--operator", "--actor", ACTOR,
                        "--reason", REPRISE, "--expect-reason", MOTIF, "--json")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue(json.loads(out.stdout)["changed"])

    def test_cli_refuses_a_client_principal_an_agent_session_and_free_actors(self):
        pose = ("pause", "worker", "--actor", ACTOR, "--reason", MOTIF,
                "--not-after", str(time.time() + 60))
        out = self.mesh(*pose, "--human", "human:alice")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("without --human", out.stderr)
        out = self.mesh(*pose, env=self.env(AGENT_MAIL_NAME="worker",
                                            AMEESH_RUNNER_ID="stale-runner",
                                            AMEESH_LEASE_EPOCH="99"))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("agent session", out.stderr)
        out = self.mesh("pause", "worker", "--actor", "ameesh:file-decoupage",
                        "--reason", MOTIF)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        out = self.mesh("resume", "worker", "--actor", ACTOR, "--reason", REPRISE)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("--actor requires --operator", out.stderr)
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])


# -- vm.sh ---------------------------------------------------------------------
def fonctions(*noms: str) -> str:
    """Le texte des fonctions de vm.sh nommées, tel quel."""
    texte = VM_SH.read_text()
    corps = []
    for nom in noms:
        m = re.search(r"^%s\(\) \{.*?^\}\n" % re.escape(nom), texte, re.M | re.S)
        assert m, nom
        corps.append(m.group(0))
    return "".join(corps)


def constantes() -> str:
    texte = VM_SH.read_text()
    return "".join(re.findall(r"^(?:ACTEUR|UNITE)_STAGING=.*\n", texte, re.M))


class VmShStagingActorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ameesh-5967-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def bash(self, script: str, **env: str) -> subprocess.CompletedProcess:
        base = {k: v for k, v in os.environ.items()
                if k not in ("AGENT_MAIL_NAME", "AMEESH_TURN_ID", "AMEESH_OPERATEUR")}
        base.update(env)
        return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script],
                              env=base, capture_output=True, text=True, timeout=60)

    def cgroup(self, ligne: str) -> str:
        path = self.root / "cgroup"
        path.write_text(ligne + "\n")
        return str(path)

    def test_the_motif_written_by_vm_sh_is_the_one_the_cli_admits(self):
        script = constantes() + fonctions("drain_lieu", "drain_motif", "drain_reprise") + '''
hostname() { echo ameesh-agents-3; }
for MODE in agents serveur; do
  ACTION=redemarrer HOTE_AGENTS=ameesh-agents-1 VM=10.77.0.2
  drain_motif "ameesh-agents-3-4242-1717"; echo
  drain_reprise; echo
done'''
        p = self.bash(script)
        self.assertEqual(p.returncode, 0, p.stderr)
        lignes = p.stdout.splitlines()
        self.assertEqual(len(lignes), 4)
        for motif, reprise in (lignes[0:2], lignes[2:4]):
            self.assertRegex(motif, agent_pause.DRAIN_REASON_RE)
            self.assertRegex(reprise, agent_pause.DRAIN_RESUME_RE)

    def test_the_unit_proof_reads_the_cgroup_and_refuses_agent_turns(self):
        script = constantes() + fonctions("preuve_unite_staging") + '''
preuve_unite_staging "$1" && echo oui || echo non'''
        unite = "0::/user.slice/user-980.slice/user@980.service/app.slice/ameesh-staging.service"
        cas = ((unite, {}, "oui"),
               (unite, {"AGENT_MAIL_NAME": "mty-amh-impl-12"}, "non"),
               (unite, {"AMEESH_TURN_ID": "t-1"}, "non"),
               (unite + "/sous", {}, "non"),
               (unite.replace("ameesh-staging", "run-u42"), {}, "non"),
               (unite.replace("ameesh-staging", "x-ameesh-staging"), {}, "non"),
               ("1:name=systemd:/app.slice/ameesh-staging.service", {}, "non"))
        for ligne, env, attendu in cas:
            with self.subTest(ligne=ligne, env=env):
                fichier = self.cgroup(ligne)
                p = subprocess.run(
                    ["bash", "-c", "set -euo pipefail\n" + script, "x", fichier],
                    env={**{k: v for k, v in os.environ.items()
                            if k not in ("AGENT_MAIL_NAME", "AMEESH_TURN_ID")}, **env},
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(p.stdout.strip(), attendu, p.stderr)
        p = self.bash(script.replace('"$1"', "/nonexistent/cgroup"))
        self.assertEqual(p.stdout.strip(), "non")

    def test_outside_the_unit_the_machine_actor_stops_without_drain(self):
        # Ce banc ne tourne pas dans ameesh-staging.service : la vraie preuve
        # (/proc/self/cgroup) échoue, rien n'est posé ni attribué.
        script = constantes() + fonctions(
            "preuve_unite_staging", "operateur_drain", "choisir_operateur_drain") + '''
DRAIN_ATTENTE=1 OPERATEUR_DRAIN=
choisir_operateur_drain
echo "attente=$DRAIN_ATTENTE operateur=$OPERATEUR_DRAIN"'''
        p = self.bash(script, AMEESH_OPERATEUR=ACTOR)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "attente=0 operateur=")
        self.assertIn("non prouvé", p.stderr)

    def cli_cible(self, aide: str) -> str:
        """Faux CLI de la cible : `--help` rend `aide`, le reste est journalisé."""
        journal = self.root / "cli.log"
        return '''
ameesh_local_drain() {
  if [ "${2:-}" = --help ]; then printf '%%s\\n' %s; return 0; fi
  printf '%%s\\n' "$*" >> %s
  echo '{"changed": true}'
}
preuve_unite_staging() { return 0; }
MODE=serveur
''' % (json.dumps(aide), json.dumps(str(journal)))

    def test_an_old_target_cli_means_a_proven_stop_without_drain(self):
        script = constantes() + fonctions(
            "operateur_drain", "drain_acteur_supporte", "choisir_operateur_drain") \
            + self.cli_cible("usage: ameesh pause AGENT --reason R [--human H] "
                             "[--not-after EPOCH] [--json]") + '''
DRAIN_ATTENTE=1 OPERATEUR_DRAIN=
choisir_operateur_drain
echo "attente=$DRAIN_ATTENTE operateur=$OPERATEUR_DRAIN"'''
        p = self.bash(script, AMEESH_OPERATEUR=ACTOR)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "attente=0 operateur=")
        self.assertIn("ne connaît pas --actor ameesh:staging", p.stderr)
        self.assertIn("arrêt prouvé sans drain", p.stderr)

    def test_a_proven_actor_sends_actor_and_never_a_client_principal(self):
        script = constantes() + fonctions(
            "operateur_drain", "drain_acteur_supporte", "choisir_operateur_drain",
            "drain_acteur", "pause_operateur", "reprise_operateur") \
            + self.cli_cible("usage: ameesh pause [--actor {ameesh:staging}] "
                             "[--not-after EPOCH]") + '''
drain_reprise() { printf 'reprise'; }
DRAIN_ATTENTE=1 OPERATEUR_DRAIN=
choisir_operateur_drain
echo "attente=$DRAIN_ATTENTE operateur=$OPERATEUR_DRAIN"
pause_operateur dev9 "motif" 1700000000 >/dev/null
reprise_operateur dev9 "motif" >/dev/null'''
        p = self.bash(script, AMEESH_OPERATEUR=ACTOR)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "attente=1 operateur=ameesh:staging")
        appels = (self.root / "cli.log").read_text().splitlines()
        self.assertEqual(appels, [
            "pause dev9 --reason motif --actor ameesh:staging --not-after 1700000000 --json",
            "resume dev9 --operator --reason reprise --expect-reason motif "
            "--actor ameesh:staging --json"])

    def test_a_human_operator_keeps_the_human_flag(self):
        script = constantes() + fonctions("drain_acteur", "pause_operateur") \
            + self.cli_cible("") + '''
OPERATEUR_DRAIN=human:alice
pause_operateur dev9 "motif" 1700000000 >/dev/null'''
        p = self.bash(script)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.root / "cli.log").read_text().strip(),
                         "pause dev9 --reason motif --human human:alice "
                         "--not-after 1700000000 --json")


if __name__ == "__main__":
    unittest.main()
