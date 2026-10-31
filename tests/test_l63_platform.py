# SPDX-License-Identifier: AGPL-3.0-only
"""L63 : couche plateforme (`ameesh.platform`), adoption fail-closed, lint.

`PlatformTest`, `LintTest` et `GuaranteeTest` tournent sans base : la CI les
lance aussi sous macOS (job court `platform-macos` de `tests.yml`), où L68
exécute en plus la suite sélectionnée par le plan dans `macos.yml` (nocturne et
`ci-macos`, hors du chemin du paquet depuis #3198). Depuis #3026, ce job court
est requis par `tests`. Sous Linux, chacun passe
deux fois : avec psutil (s'il est installé) et par le repli /proc (psutil
retiré).
"""
from __future__ import annotations

import ast
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import platform

from .support import PgTestCase

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "ameesh")


def _variants():
    """(nom, contexte) : psutil tel quel, puis repli /proc sous Linux."""
    out = []
    if platform.has_psutil():
        out.append(("psutil", lambda: mock.patch.object(platform, "psutil", platform.psutil)))
    if platform.is_linux():
        out.append(("repli /proc", lambda: mock.patch.object(platform, "psutil", None)))
    return out


class PlatformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.variants = _variants()
        if not self.variants:
            self.skipTest("ni psutil ni /proc : couche plateforme muette (NotAvailable)")

    def _child(self, code: str, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code, *args])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def _wait(self, predicate, timeout: float = 10.0) -> None:
        fin = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), fin, "délai dépassé")
            time.sleep(0.05)

    def test_systeme_connu(self):
        self.assertIn(platform.SYSTEM, ("linux", "macos", "windows", sys.platform))
        self.assertEqual(sum([platform.is_linux(), platform.is_macos(),
                              platform.is_windows()]), 1 if platform.SYSTEM in
                         ("linux", "macos", "windows") else 0)

    def test_ascendance(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                chain = platform.ancestry()
                self.assertEqual(chain[0], os.getpid())
                self.assertIn(os.getppid(), chain)
                self.assertNotIn(1, chain)
                self.assertNotIn(0, chain)
                self.assertEqual(platform.ancestry(os.getppid())[0], os.getppid())

    def test_heure_de_demarrage_en_secondes_epoch(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                debut = platform.start_time(os.getpid())
                self.assertIsInstance(debut, float)
                self.assertLessEqual(debut, time.time() + platform.process.START_TOLERANCE_S)
                self.assertGreater(debut, platform.boot_time() - 1)
                self.assertIsNone(platform.start_time(2 ** 22 + 12345))
                self.assertIsNone(platform.start_time(None))
                enfant = self._child("import time; time.sleep(60)")
                autre = platform.start_time(enfant.pid)
                self.assertGreaterEqual(autre, debut - 1)
                self.assertTrue(platform.same_start(autre, autre + 0.5))
                self.assertFalse(platform.same_start(autre, autre + 5))
                self.assertFalse(platform.same_start(autre, None))

    def test_les_deux_voies_concordent(self):
        if len(self.variants) < 2:
            self.skipTest("une seule voie disponible")
        mesures = []
        for _nom, ctx in self.variants:
            with ctx():
                mesures.append(platform.start_time(os.getpid()))
        self.assertTrue(platform.same_start(mesures[0], mesures[1]), mesures)

    @unittest.skipUnless(platform.is_linux(), "tops d'horloge : Linux")
    def test_conversion_des_tops(self):
        hertz = os.sysconf("SC_CLK_TCK")
        debut = platform.start_time(os.getpid())
        tops = round((debut - platform.boot_time()) * hertz)
        self.assertTrue(platform.same_start(platform.ticks_to_epoch(tops), debut))
        with mock.patch.object(platform, "SYSTEM", "macos"):
            with self.assertRaises(platform.NotAvailable):
                platform.ticks_to_epoch(tops)

    def test_vivant(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                enfant = self._child("import time; time.sleep(60)")
                self.assertTrue(platform.alive(enfant.pid))
                enfant.kill()
                enfant.wait()
                self.assertFalse(platform.alive(enfant.pid))
                self.assertTrue(platform.alive(os.getpid()))

    def test_detenteurs_fichier_ouvert_et_ligne_de_commande(self):
        with tempfile.TemporaryDirectory() as tmp:
            chemin = os.path.join(tmp, "session.jsonl")
            open(chemin, "w").close()
            pret = os.path.join(tmp, "pret")
            tient = self._child(
                "import sys, time; fh = open(sys.argv[1]); open(sys.argv[2], 'w').close();"
                " time.sleep(60)", chemin, pret)
            nomme = self._child("import time; time.sleep(60)", "--resume", "sid-l63-xyz")
            self._wait(lambda: os.path.exists(pret))
            for nom, ctx in self.variants:
                with self.subTest(nom), ctx():
                    found = {h["pid"]: h for h in platform.holders(chemin, "sid-l63-xyz")}
                    self.assertEqual(found[tient.pid]["why"], "open file")
                    self.assertIn(nomme.pid, found)
                    self.assertIn("command line", found[nomme.pid]["why"])
                    # les PID exclus (ascendance d'adopt) ne comptent pas par leur
                    # ligne de commande
                    exclus = platform.holders(chemin, "sid-l63-xyz", exclude=[nomme.pid])
                    self.assertNotIn(nomme.pid, [h["pid"] for h in exclus])
                    # personne ne tient un autre fichier — sauf sur un hôte où
                    # l'OS cache les fichiers ouverts d'un processus tiers du
                    # même utilisateur (gpg-agent non dumpable, L63b) : le refus
                    # est alors la réponse documentée, pas une liste vide non
                    # vérifiée.
                    autre = os.path.join(tmp, "autre.jsonl")
                    open(autre, "w").close()
                    try:
                        vides = platform.holders(autre, "sid-absent")
                    except platform.NotAvailable as exc:
                        self.assertIn("unreadable", str(exc))
                    else:
                        self.assertEqual(vides, [])

    def test_une_session_citee_n_est_pas_un_detenteur(self):
        """#1283 : un argv qui CITE l'id (brief JSON, message) n'est pas une
        reprise en cours ; seuls les sélecteurs de session le nomment."""
        with tempfile.TemporaryDirectory() as tmp:
            chemin = os.path.join(tmp, "session.jsonl")
            open(chemin, "w").close()
            sid = "sid-l63-cite-xyz"
            pret = {nom: os.path.join(tmp, "pret-" + nom)
                    for nom in ("citant", "repris", "lie", "options")}
            code = "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(60)"
            citant = self._child(code, pret["citant"], "--json",
                                 '{"brief": "reprends la session %s"}' % sid)
            repris = self._child(code, pret["repris"], "--resume", sid)
            lie = self._child(code, pret["lie"], "--resume=%s" % sid)
            # #1283 R1 cx2 : les options intercalees avant l'id ne perdent pas
            # la reprise (`codex exec resume --json <id>`).
            options = self._child(code, pret["options"], "exec", "resume",
                                  "--json", sid)
            self._wait(lambda: all(os.path.exists(p) for p in pret.values()))
            for nom, ctx in self.variants:
                with self.subTest(nom), ctx():
                    found = {h["pid"]: h for h in
                             platform.holders(chemin, sid, session=True)}
                    self.assertNotIn(citant.pid, found)
                    self.assertIn(repris.pid, found)
                    self.assertIn(lie.pid, found)
                    self.assertIn(options.pid, found)

    def test_reconnaissance_du_positional_de_reprise(self):
        """#1283 R1 cx2 : options intercalees positives, citations negatives.

        `--all` elargit la recherche (il ne designe pas la derniere session) ;
        les flags sans valeur ne mangent pas l'id ; `-p`/`--profile` de Codex
        porte une valeur (contrairement au `-p` de Claude)."""
        from ameesh.platform import process as platform_process
        sid, autre = "sid-r1-xyz", "sid-autre-xyz"
        cas = [
            (["codex", "exec", "resume", "--json", sid], True),
            (["codex", "resume", "--no-alt-screen", sid], True),
            (["codex", "resume", "--no-daemon", sid], True),
            (["codex", "exec", "resume", "--worktree", sid], True),
            (["codex", "exec", "resume", "--ephemeral", sid], True),
            (["codex", "exec", "resume", "--ignore-user-config", sid], True),
            (["codex", "exec", "resume", "--ignore-rules", sid], True),
            (["codex", "exec", "resume", "--strict-config", sid], True),
            # --approve-for-me (interactif seulement) ne prend pas de valeur :
            # l'id qui suit reste le positional (liste cx2, 14/10 09:42).
            (["codex", "resume", "--approve-for-me", sid], True),
            (["codex", "resume", "--dangerously-bypass-hook-trust", sid], True),
            # --all : Show all sessions, l'id qui suit est le positional
            (["codex", "resume", "--all", sid], True),
            (["codex", "resume", "-m", "gpt-5.1-codex", sid], True),
            (["codex", "resume", "-c", 'model_reasoning_effort="high"', sid], True),
            (["codex", "resume", "--model=gpt-5.1-codex", sid], True),
            (["codex", "resume", "--", sid], True),
            (["claude", "--resume", sid], True),
            (["claude", "-r", sid], True),
            (["claude", "--resume", sid, "-p", "prompt qui cite " + sid], True),
            (["claude", "-p", "prompt qui cite " + sid, "--resume", sid], True),
            (["dsh", "--session-id", sid], True),
            (["dsh", "--session-id=%s" % sid], True),
            (["dsh", "tui", "--resume", sid], True),
            # un mot `resume` du texte ne masque pas le selecteur reel
            (["claude", "-p", "resume", "--resume", sid], True),
            (["codex", "resume", "--json", "prompt qui cite " + sid], False),
            (["codex", "resume", "--last", "prompt qui cite " + sid], False),
            (["codex", "resume", "--last", sid], False),
            (["codex", "resume", autre, "prompt qui cite " + sid], False),
            (["codex", "resume", "--model", sid, autre], False),
            (["codex", "resume", "--model=" + sid, autre], False),
            (["codex", "resume", "-p", sid, autre], False),
            (["codex", "resume", "--profile", sid, autre], False),
            (["dsh", "--json", '{"brief": "session %s"}' % sid], False),
        ]
        for argv, attendu in cas:
            with self.subTest(argv=argv):
                self.assertIs(platform_process._session_argument(argv, sid), attendu)

    def test_detenteurs_d_un_dossier(self):
        with tempfile.TemporaryDirectory() as tmp:
            dossier = os.path.join(tmp, "session")
            os.makedirs(dossier)
            dedans = self._child("import time; time.sleep(60)")
            enfant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                      cwd=dossier)
            self.addCleanup(lambda: (enfant.kill(), enfant.wait()))
            del dedans
            for nom, ctx in self.variants:
                with self.subTest(nom), ctx():
                    found = {h["pid"]: h for h in platform.holders(dossier)}
                    self.assertEqual(found[enfant.pid]["why"], "working directory")

    def test_os_muet_leve_not_available(self):
        """Ni psutil ni /proc : jamais de liste vide, jamais de None muet."""
        with mock.patch.object(platform, "psutil", None), \
                mock.patch.object(platform, "SYSTEM", "macos"):
            for appel in (lambda: platform.holders(__file__, "x"),
                          lambda: platform.ancestry(),
                          lambda: platform.start_time(os.getpid()),
                          lambda: platform.boot_time(),
                          lambda: platform.memory(),
                          lambda: platform.open_fd_count()):
                with self.assertRaises(platform.NotAvailable) as ctx:
                    appel()
                self.assertEqual(ctx.exception.system, "macos")
            # mesures non sensibles : inconnues, sans erreur
            self.assertIsNone(platform.human_tty())
            self.assertEqual(platform.power(), {"on_ac": None, "battery_percent": None})

    def test_detenteurs_illisibles_pour_soi_meme(self):
        if not platform.has_psutil():
            self.skipTest("psutil absent")

        class Refus(Exception):
            pass

        class Muet:
            def open_files(self):
                raise Refus("refusé")

        faux = mock.MagicMock(AccessDenied=Refus)
        faux.Process.return_value = Muet()
        with mock.patch.object(platform, "psutil", faux), \
                mock.patch.object(platform.process, "_proc_ok", return_value=False):
            with self.assertRaises(platform.NotAvailable):
                platform.holders(__file__, "x")

    def _faux_proc(self, comm: str, uids: str, *, state: str = "S",
                   lien_refuse: bool = False, status_illisible: bool = False) -> str:
        """Un /proc minimal : ce processus-ci (lisible) et le pid 4242
        (`uids` : réel effectif …), dont `fd` refuse la lecture (non
        dumpable) ou, avec `lien_refuse`, se liste mais pas ses liens."""
        import tempfile
        racine = tempfile.mkdtemp()
        autre = os.path.join(racine, "4242")
        self.addCleanup(lambda: (subprocess.run(["chmod", "-R", "u+rwx", racine]),
                                 subprocess.run(["rm", "-rf", racine])))
        moi = os.path.join(racine, str(os.getpid()))
        os.makedirs(os.path.join(moi, "fd"))
        os.symlink(moi, os.path.join(racine, "self"))
        os.makedirs(os.path.join(autre, "fd"))
        with open(os.path.join(autre, "comm"), "w") as fh:
            fh.write(comm + "\n")
        with open(os.path.join(autre, "status"), "w") as fh:
            fh.write("Name:\t%s\nState:\t%s (x)\nUid:\t%s\n" % (comm, state, uids))
        with open(os.path.join(autre, "cmdline"), "w") as fh:
            fh.write(comm + "\0")
        if status_illisible:
            os.chmod(os.path.join(autre, "status"), 0)
        if lien_refuse:   # readlink d'un fichier ordinaire : EINVAL, pas de cible
            open(os.path.join(autre, "fd", "3"), "w").close()
        else:
            os.chmod(os.path.join(autre, "fd"), 0)
        return racine

    def test_detenteur_du_meme_utilisateur_illisible(self):
        """L63b : un processus de l'utilisateur dont les fichiers ouverts se
        refusent n'est pas une observation vide : NotAvailable (repli /proc,
        psutil retiré). Revue #377 r2 : le gestionnaire de services se
        reconnaît à son PID, pas à son nom ; un refus sur un lien compte ; un
        status illisible vaut « inconnu ». CI : un processus qui se termine
        (zombie) ou une aide setuid (sudo) ne compte pas."""
        if os.geteuid() == 0:
            self.skipTest("root lit tout : pas de refus de lecture à simuler")
        moi = "%d %d %d %d" % ((os.getuid(),) * 4)
        cas = (("harnais muet", "node", moi, {}, (), True),
               ("nom emprunté au gestionnaire", "systemd", moi, {}, (), True),
               ("gestionnaire, par son PID", "systemd", moi, {}, {4242}, False),
               ("refus sur un lien", "node", moi, {"lien_refuse": True}, (), True),
               ("status illisible", "node", moi, {"status_illisible": True}, (), True),
               ("zombie", "git", moi, {"state": "Z"}, (), False),
               ("aide setuid (sudo)", "sudo", "%d 0 0 0" % os.getuid(), {}, (), False),
               ("autre utilisateur", "node", "0 0 0 0", {}, (), False))
        for nom, comm, uids, options, gestionnaires, refus in cas:
            with self.subTest(nom):
                racine = self._faux_proc(comm, uids, **options)
                with mock.patch.object(platform, "psutil", None), \
                        mock.patch.object(platform.process, "PROC", racine), \
                        mock.patch.object(platform.process, "DOUBT_RETRY_S", 0), \
                        mock.patch.object(platform.process, "service_managers",
                                          return_value=frozenset(gestionnaires)), \
                        mock.patch.object(platform, "SYSTEM", "linux"):
                    if refus:
                        with self.assertRaises(platform.NotAvailable) as ctx:
                            platform.holders(__file__, "x")
                        self.assertIn("4242", str(ctx.exception))
                    else:
                        self.assertEqual(platform.holders(__file__, "x"), [])

    def test_allow_des_processus_illisibles(self):
        """L230 : un processus illisible dont le `comm` ET l'`argv[0]` sont
        dans `allow` n'est plus un doute (le ménage de /tmp peut conclure que
        personne ne tient l'entrée) ; un nom emprunté d'un seul côté, ou
        absent de la liste, reste un doute."""
        if os.geteuid() == 0:
            self.skipTest("root lit tout : pas de refus de lecture à simuler")
        uid = "%d %d %d %d" % ((os.getuid(),) * 4)
        cas = (("nommé et annoncé", "gpg-agent", None, ("gpg-agent",), False),
               ("nom absent de la liste", "gpg-agent", None, ("ssh-agent",), True),
               ("argv emprunté", "gpg-agent", "/usr/bin/malveillant",
                ("gpg-agent",), True),
               ("comm emprunté", "node", "/usr/bin/gpg-agent", ("gpg-agent",), True),
               ("allow vide", "gpg-agent", None, (), True))
        for nom, comm, argv0, allow, doute in cas:
            with self.subTest(nom):
                racine = self._faux_proc(comm, uid)
                if argv0:
                    with open(os.path.join(racine, "4242", "cmdline"), "w") as fh:
                        fh.write(argv0 + "\0")
                with mock.patch.object(platform, "psutil", None), \
                        mock.patch.object(platform.process, "PROC", racine), \
                        mock.patch.object(platform.process, "DOUBT_RETRY_S", 0), \
                        mock.patch.object(platform.process, "service_managers",
                                          return_value=frozenset()), \
                        mock.patch.object(platform, "SYSTEM", "linux"):
                    if doute:
                        with self.assertRaises(platform.NotAvailable):
                            platform.holders(__file__, "x", allow=allow)
                    else:
                        self.assertEqual(platform.holders(__file__, "x", allow=allow), [])

    def test_allow_ne_cache_pas_un_detenteur_constate(self):
        """L230 : `allow` ne lève que le DOUTE ; un processus lisible qui tient
        le fichier reste rapporté, même nommé dans la liste."""
        if os.geteuid() == 0:
            self.skipTest("root lit tout : pas de refus de lecture à simuler")
        uid = "%d %d %d %d" % ((os.getuid(),) * 4)
        racine = self._faux_proc("gpg-agent", uid)
        # le pid 4242 redevient lisible : ses `fd` pointent le fichier examiné
        os.chmod(os.path.join(racine, "4242", "fd"), 0o700)
        preuve = os.path.realpath(__file__)
        os.symlink(preuve, os.path.join(racine, "4242", "fd", "3"))
        with mock.patch.object(platform, "psutil", None), \
                mock.patch.object(platform.process, "PROC", racine), \
                mock.patch.object(platform.process, "DOUBT_RETRY_S", 0), \
                mock.patch.object(platform.process, "service_managers",
                                  return_value=frozenset()), \
                mock.patch.object(platform, "SYSTEM", "linux"):
            found = platform.holders(preuve, "x", allow=("gpg-agent",))
        self.assertEqual([h["pid"] for h in found], [4242])
        self.assertEqual(found[0]["why"], "open file")

    def test_doubt_psutil_allow(self):
        """L230, hors Linux : `allow` lève le doute par nom ET `argv[0]`."""
        process = platform.process

        class Uids:
            effective = os.getuid()

        class P:
            def __init__(self, name, argv0):
                self._name, self._argv0 = name, argv0

            def status(self):
                return "running"

            def uids(self):
                return Uids()

            def name(self):
                return self._name

            def exe(self):
                return "/usr/local/bin/" + self._name

            def cmdline(self):
                return [self._argv0]

        class PS:
            STATUS_ZOMBIE = "zombie"
            AccessDenied = type("AccessDenied", (Exception,), {})
            ZombieProcess = type("ZombieProcess", (Exception,), {})
            NoSuchProcess = type("NoSuchProcess", (Exception,), {})

            def __init__(self, proc):
                self._proc = proc

            def Process(self, pid):
                return self._proc

        cas = (("nommé et annoncé", P("gpg-agent", "/usr/bin/gpg-agent"),
                ("gpg-agent",), None),
               ("nom absent", P("gpg-agent", "/usr/bin/gpg-agent"),
                ("ssh-agent",), "4242 gpg-agent"),
               ("argv différent", P("gpg-agent", "/usr/bin/autre"),
                ("gpg-agent",), "4242 gpg-agent"))
        for nom, proc, allow, attendu in cas:
            with self.subTest(nom):
                self.assertEqual(process._doubt_psutil(PS(proc), 4242, os.getuid(),
                                                       allow), attendu)

    def test_detenteur_verifie_malgre_un_tiers_illisible(self):
        """#1749 : un détenteur vérifié tient l'entrée ; le refus d'un
        processus tiers du même utilisateur (gpg-agent non dumpable) ne jette
        plus cette réponse positive — le ménage gardait sinon tout /tmp
        (« holders unreadable »). Le doute ne vaut que sans détenteur."""
        if os.geteuid() == 0:
            self.skipTest("root lit tout : pas de refus de lecture à simuler")
        moi = "%d %d %d %d" % ((os.getuid(),) * 4)
        with tempfile.TemporaryDirectory() as tmp:
            # macOS : `tempfile` rend /var/folders/… quand le chemin réel est
            # /private/var/… ; `holders` compare le lien lu de /proc à son
            # realpath, donc la cible doit être canonique dès le symlink, sinon
            # le détenteur n'est pas retrouvé et le doute l'emporte (CI macOS).
            cible = os.path.realpath(os.path.join(tmp, "tenu"))
            open(cible, "w").close()
            racine = self._faux_proc("gpg-agent", moi)
            # ce processus-ci tient `cible` : le /proc simulé porte son lien
            os.symlink(cible, os.path.join(racine, str(os.getpid()), "fd", "3"))
            with mock.patch.object(platform, "psutil", None), \
                    mock.patch.object(platform.process, "PROC", racine), \
                    mock.patch.object(platform.process, "DOUBT_RETRY_S", 0), \
                    mock.patch.object(platform.process, "service_managers",
                                      return_value=frozenset()), \
                    mock.patch.object(platform, "SYSTEM", "linux"):
                found = platform.holders(cible)
        self.assertEqual([h["pid"] for h in found], [os.getpid()])

    def test_un_processus_qui_se_termine_n_est_pas_un_doute(self):
        """CI #377 r2 : un `git` en fin de vie refuse son `fd` avant d'être
        zombie ; relu, il est zombie ou parti. Un détenteur vivant, lui,
        reste un doute à chaque relecture."""
        uid = str(os.getuid())
        vivant = {"Name": ["git"], "State": ["R"], "Uid": [uid, uid]}
        process = platform.process
        for nom, suite, refus in (("se termine", [vivant, dict(vivant, State=["Z"])], False),
                                  ("disparaît", [vivant, None], False),
                                  ("reste vivant", [vivant] * 10, True)):
            with self.subTest(nom), \
                    mock.patch.object(process, "_proc_status", side_effect=suite), \
                    mock.patch.object(process, "DOUBT_RETRY_S", 0):
                doutes = {"4242": process._doubt_proc("4242", os.getuid(), lambda: ())}
                again = lambda pid: process._doubt_proc(pid, os.getuid(), lambda: ())
                if refus:
                    with self.assertRaises(platform.NotAvailable):
                        process._settle(doutes, again)
                else:
                    process._settle(doutes, again)

    def test_gestionnaire_de_services_par_pid(self):
        """Le MainPID de user@<uid>.service (demandé à systemd) et ses
        enfants directs aux groupes supplémentaires VIDES quand ceux du
        gestionnaire ne le sont pas ((sd-pam), forké root). Ni le nom ni
        l'heure de démarrage ne comptent (revue #377 r2-r4)."""
        import shutil
        import tempfile
        racine = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, racine)

        def proc(pid, comm, ppid, groupes):
            os.makedirs(os.path.join(racine, str(pid)))
            champs = ["S", str(ppid)] + ["0"] * 28
            with open(os.path.join(racine, str(pid), "stat"), "w") as fh:
                fh.write("%d (%s) %s\n" % (pid, comm, " ".join(champs)))
            with open(os.path.join(racine, str(pid), "status"), "w") as fh:
                fh.write("Name:\t%s\nGroups:\t%s\n" % (comm, groupes))

        os.makedirs(os.path.join(racine, "self"))
        proc(100, "systemd", 1, "966 1000 ")
        proc(102, "(sd-pam)", 100, "966 1000 ")    # nom emprunté, groupes du gestionnaire
        proc(103, "node", 7, "")                    # groupes vides, pas son enfant
        fait = subprocess.CompletedProcess([], 0, stdout="100\n", stderr="")
        process = platform.process
        with mock.patch.object(process, "PROC", racine), \
                mock.patch.object(platform, "SYSTEM", "linux"), \
                mock.patch("subprocess.run", return_value=fait):
            self.assertEqual(process.service_managers(1000), frozenset({100}))
            proc(101, "(sd-pam)", 100, "")
            self.assertEqual(process.service_managers(1000), frozenset({100, 101}))
            # un gestionnaire sans groupe n'exempte aucun enfant
            with open(os.path.join(racine, "100", "status"), "w") as fh:
                fh.write("Name:\tsystemd\nGroups:\t\n")
            self.assertEqual(process.service_managers(1000), frozenset({100}))
        sans = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(process, "PROC", racine), \
                mock.patch.object(platform, "SYSTEM", "linux"), \
                mock.patch("subprocess.run", return_value=sans):
            self.assertEqual(process.service_managers(1000), frozenset())

    def test_detenteur_du_meme_utilisateur_refuse_psutil(self):
        """L63b, voie psutil hors Linux : AccessDenied sur un processus de
        l'utilisateur (uid effectif) est un doute ; sur une aide setuid, un
        zombie ou un exécutable système (macOS : ContextStoreAgent sous
        /System, CI #377 r2), non."""
        class AccessDenied(Exception):
            pass

        class NoSuchProcess(Exception):
            pass

        class Proc:
            def __init__(self, pid, effective, exe="/opt/node", status="sleeping"):
                self.pid, self._eff, self._exe, self._status = pid, effective, exe, status

            def oneshot(self):
                return contextlib.nullcontext()

            def open_files(self):
                if self.pid == os.getpid():
                    return []
                raise AccessDenied()

            def cmdline(self):
                raise AccessDenied()

            def name(self):
                return "x"

            def exe(self):
                return self._exe

            def status(self):
                return self._status

            def uids(self):
                return mock.Mock(real=os.getuid(), effective=self._eff)

        cas = (("harnais", Proc(4242, os.getuid()), True),
               ("setuid", Proc(4242, 0), False),
               ("zombie", Proc(4242, os.getuid(), status="zombie"), False),
               ("système", Proc(4242, os.getuid(),
                                exe="/System/Library/PrivateFrameworks/X/ContextStoreAgent"),
                False))
        for nom, autre, refus in cas:
            with self.subTest(nom):
                faux = mock.MagicMock(AccessDenied=AccessDenied, NoSuchProcess=NoSuchProcess,
                                      ZombieProcess=NoSuchProcess, STATUS_ZOMBIE="zombie")
                faux.Process.side_effect = lambda pid, autre=autre: (
                    autre if pid == autre.pid else Proc(pid, os.getuid()))
                faux.process_iter.return_value = [autre]
                with mock.patch.object(platform, "psutil", faux), \
                        mock.patch.object(platform.process, "_proc_ok", return_value=False), \
                        mock.patch.object(platform.process, "DOUBT_RETRY_S", 0):
                    if refus:
                        with self.assertRaises(platform.NotAvailable):
                            platform.holders(__file__, "x")
                    else:
                        self.assertEqual(platform.holders(__file__, "x"), [])

    def test_mesures_de_l_hote(self):
        for nom, ctx in self.variants:
            with self.subTest(nom), ctx():
                mem = platform.memory()
                self.assertGreater(mem.get("mem_available_bytes", 1), 0)
                charge = platform.load_average()
                self.assertTrue(charge is None or charge >= 0)
                self.assertIsInstance(platform.power(), dict)
                self.assertIn(platform.mount_of(tempfile.gettempdir()) is None, (True, False))
                self.assertGreater(platform.open_fd_count(), 0)
                tty = platform.human_tty()
                self.assertTrue(tty is None or tty.startswith("/dev/"))

    def test_point_de_montage_et_alimentation_depuis_des_fichiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Une table de montage porte des chemins réels ; sur macOS le
            # dossier temporaire passe par un lien (/var -> /private/var).
            tmp = os.path.realpath(tmp)
            mounts = os.path.join(tmp, "mounts")
            with open(mounts, "w") as fh:
                fh.write("rootfs / ext4 rw 0 0\ntmpfs %s tmpfs rw 0 0\n" % tmp)
            self.assertEqual(platform.mount_of(os.path.join(tmp, "x"), mounts), (tmp, "tmpfs"))
            bat = os.path.join(tmp, "power", "BAT0")
            os.makedirs(bat)
            for nom, valeur in (("type", "Battery"), ("capacity", "42"),
                                ("status", "Discharging")):
                with open(os.path.join(bat, nom), "w") as fh:
                    fh.write(valeur)
            self.assertEqual(platform.power(os.path.join(tmp, "power")),
                             {"on_ac": False, "battery_percent": 42.0})

    def test_alimentation_psutil_hors_linux(self):
        if not platform.has_psutil():
            self.skipTest("psutil absent")
        faux = mock.MagicMock()
        faux.sensors_battery.return_value = mock.Mock(percent=73.0, power_plugged=True)
        with mock.patch.object(platform, "psutil", faux), \
                mock.patch.object(platform, "SYSTEM", "macos"):
            self.assertEqual(platform.power(), {"on_ac": True, "battery_percent": 73.0})
            faux.sensors_battery.return_value = None
            self.assertEqual(platform.power(), {"on_ac": None, "battery_percent": None})


# --------------------------------------------------------------------------
# adopt : jamais fail-open
# --------------------------------------------------------------------------

class AdoptFailClosedTest(PgTestCase):
    """Sans moyen de vérifier qu'une session est fermée, `adopt` refuse, même
    avec `--force` (L63b, décision du propriétaire #9413 b). `--force` sur une
    session que l'hôte voit ouverte reste possible, journalisé."""

    def _session(self):
        import dataclasses
        from ameesh import registry
        from .test_l39_adoption_reprise import SID, _rollout
        comptes_dir = os.path.join(self.tmp, "comptes", "codex-1")
        os.makedirs(comptes_dir, mode=0o700)
        open(os.path.join(comptes_dir, "auth.json"), "w").close()
        os.chmod(os.path.join(comptes_dir, "auth.json"), 0o600)
        travail = os.path.join(self.tmp, "travail")
        os.makedirs(travail)
        _rollout(comptes_dir, SID, travail)
        cfg = dataclasses.replace(
            self.cfg, accounts={"codex": [{"name": "primaire", "path": comptes_dir}]})
        registry.upsert(self.db, "coord", harness="codex", host=cfg.host, mode="externe")
        return cfg, SID

    def _fil(self) -> str:
        texte = []
        for racine, _d, fichiers in os.walk(self.cfg.threads_root):
            for nom in fichiers:
                with open(os.path.join(racine, nom), encoding="utf-8", errors="ignore") as fh:
                    texte.append(fh.read())
        return "\n".join(texte)

    def test_adopt_refuse_meme_avec_force(self):
        from ameesh import registry, reprise
        cfg, sid = self._session()
        muet = platform.NotAvailable("détenteurs d'un fichier", "test", "macos")
        for force in (False, True):
            with self.subTest(force=force):
                avert: list[str] = []
                with mock.patch.object(platform, "holders", side_effect=muet):
                    with self.assertRaises(reprise.RepriseError) as ctx:
                        reprise.adopt(cfg, self.db, "coord", session_id=sid,
                                      harness="codex", force=force, warn=avert.append)
                self.assertIn("cannot check", str(ctx.exception))
                self.assertIn("with or without --force", str(ctx.exception))
                self.assertEqual(avert, [])
                row = registry.get(self.db, "coord")
                self.assertEqual(row["mode"], "externe")
                self.assertNotEqual(row.get("session_id"), sid)
        self.assertNotIn("without checking", self._fil())

    def test_force_sur_une_session_vue_ouverte_reste_journalise(self):
        from ameesh import registry, reprise
        cfg, sid = self._session()
        tenant = [{"pid": 4242, "why": "open file", "cmd": "codex"}]
        with mock.patch.object(platform, "holders", return_value=tenant):
            with self.assertRaises(reprise.RepriseError) as ctx:
                reprise.adopt(cfg, self.db, "coord", session_id=sid, harness="codex")
            self.assertIn("still open", str(ctx.exception))
            avert: list[str] = []
            out = reprise.adopt(cfg, self.db, "coord", session_id=sid, harness="codex",
                                force=True, warn=avert.append)
        self.assertTrue(out["forced"])
        self.assertTrue(any("while it is still open" in a for a in avert))
        self.assertIn("while it is still open", self._fil())
        self.assertEqual(registry.get(self.db, "coord")["mode"], "execute")

    def test_os_muet_sans_mock_de_holders(self):
        """Ni psutil ni /proc (l'OS réellement muet, `holders` non simulé) :
        refus avec ou sans --force — Codex avec son journal, et DeepSeek sans
        dossier de sessions (DSH_HOME vers un magasin absent), qui sautait le
        contrôle (revue #377 P1)."""
        import dataclasses
        from ameesh import registry, reprise
        cfg, sid = self._session()
        absent = os.path.join(self.tmp, "dsh-absent")
        cfg_dsh = dataclasses.replace(cfg, accounts={"deepseek": [{"name": "d", "path": absent}]})
        registry.upsert(self.db, "dsh", harness="deepseek", host=cfg.host, mode="externe")
        for agent, harness, conf in (("coord", "codex", cfg), ("dsh", "deepseek", cfg_dsh)):
            for force in (False, True):
                with self.subTest(harness=harness, force=force), \
                        mock.patch.object(platform, "psutil", None), \
                        mock.patch.object(platform.process, "PROC",
                                          os.path.join(self.tmp, "pas-de-proc")), \
                        mock.patch.dict(os.environ, {"DSH_HOME": absent}):
                    with self.assertRaises(reprise.RepriseError) as ctx:
                        reprise.adopt(conf, self.db, agent, session_id=sid,
                                      harness=harness, force=force, warn=lambda _m: None)
                    self.assertIn("cannot check", str(ctx.exception))
                    self.assertEqual(registry.get(self.db, agent)["mode"], "externe")

    def test_cli_force_ne_contourne_pas(self):
        from ameesh import reprise
        cfg, sid = self._session()
        muet = platform.NotAvailable("détenteurs d'un fichier", "test", "macos")
        sortie = io.StringIO()
        with mock.patch.object(platform, "holders", side_effect=muet), \
                mock.patch("sys.stderr", sortie):
            code = reprise.cmd_adopt(cfg, reprise.build_parser().parse_args(
                ["adopt", "coord", "--session", sid, "--harness", "codex", "--force"]))
        self.assertNotEqual(code, 0)
        self.assertIn("with or without --force", sortie.getvalue())


# --------------------------------------------------------------------------
# L68 : une garantie muette lève, jamais une observation vide
# --------------------------------------------------------------------------

class GuaranteeTest(unittest.TestCase):
    """L68: on a mute OS every guarantee raises `NotAvailable`.

    `platform.GUARANTEES` is the contract list; this test names the call for
    each entry, so adding a guarantee without its witness fails here, and a
    function that goes back to returning `[]`/`None` fails its subTest. The
    OS is made mute *and* told it is macOS: the test proves on Linux exactly
    what the macOS job runs (psutil removed, no `/proc`), without a macOS host.
    """

    CALLS = {
        "ancestry": lambda: platform.ancestry(),
        "boot_time": lambda: platform.boot_time(),
        "children": lambda: platform.children(),
        "env_values": lambda: platform.env_values("AMEESH_L68"),
        "environ": lambda: platform.environ(os.getpid()),
        "holders": lambda: platform.holders(__file__, "x"),
        "open_fd_count": lambda: platform.open_fd_count(),
        "process_table": lambda: platform.process_table(),
        "self_cgroup_paths": lambda: platform.self_cgroup_paths(),
        "start_time": lambda: platform.start_time(os.getpid()),
    }

    def test_the_contract_names_every_guarantee(self):
        self.assertEqual(len(platform.GUARANTEES), len(set(platform.GUARANTEES)))
        self.assertEqual(set(self.CALLS), set(platform.GUARANTEES),
                         "une garantie de platform.GUARANTEES n'a pas son témoin ici")

    def test_a_mute_os_raises_instead_of_an_empty_answer(self):
        # un /proc vide : la couche ne peut ni lister ni lire, comme sur un OS
        # sans cette interface ; SYSTEM=macos rend le cas identique partout.
        vide = tempfile.mkdtemp(prefix="l68-sans-proc-")
        self.addCleanup(shutil.rmtree, vide, True)
        for name in platform.GUARANTEES:
            with self.subTest(name), \
                    mock.patch.object(platform, "psutil", None), \
                    mock.patch.object(platform, "SYSTEM", "macos"), \
                    mock.patch.object(platform.process, "PROC", vide):
                with self.assertRaises(platform.NotAvailable) as ctx:
                    self.CALLS[name]()
                self.assertEqual(ctx.exception.system, "macos")
                self.assertTrue(str(ctx.exception).strip(),
                                "le refus doit dire pourquoi, pas seulement lever")


# --------------------------------------------------------------------------
# lint : l'OS ne se lit que dans ameesh.platform
# --------------------------------------------------------------------------

#: fichiers (relatifs à src/ameesh) encore autorisés à lire l'OS directement,
#: avec la raison. Cette liste ne peut que DIMINUER : `MAX_EXCEPTIONS` en
#: borne la taille, et une exception devenue inutile fait échouer le test.
EXCEPTIONS: dict[str, str] = {}
MAX_EXCEPTIONS = 0


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def os_accesses(source: str) -> list[str]:
    """Les accès directs à l'OS d'un source Python : `sys.platform`, import
    du module standard `platform`, chaîne contenant `/proc` (hors docstring)."""
    tree = ast.parse(source)
    skip = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "platform" \
                and isinstance(node.value, ast.Name) and node.value.id == "sys":
            found.append("ligne %d : sys.platform" % node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name == "platform" for alias in node.names):
                found.append("ligne %d : import platform" % node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "platform":
                found.append("ligne %d : from platform import" % node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip and "/proc" in node.value:
            found.append("ligne %d : %r" % (node.lineno, node.value[:40]))
    return found


class LintTest(unittest.TestCase):
    def _violations(self) -> dict[str, list[str]]:
        out = {}
        for racine, _dirs, fichiers in os.walk(SRC):
            rel_root = os.path.relpath(racine, SRC)
            if rel_root.split(os.sep)[0] == "platform":
                continue
            for nom in fichiers:
                if not nom.endswith(".py"):
                    continue
                chemin = os.path.join(racine, nom)
                with open(chemin, encoding="utf-8") as fh:
                    found = os_accesses(fh.read())
                if found:
                    out[os.path.relpath(chemin, SRC)] = found
        return out

    def test_l_os_ne_se_lit_que_dans_la_couche_plateforme(self):
        violations = {k: v for k, v in self._violations().items() if k not in EXCEPTIONS}
        self.assertEqual(violations, {}, "accès direct à l'OS hors de ameesh.platform : "
                                         "passez par la couche plateforme (L63)")

    def test_la_liste_d_exceptions_ne_peut_que_diminuer(self):
        self.assertLessEqual(len(EXCEPTIONS), MAX_EXCEPTIONS)
        restantes = self._violations()
        inutiles = [k for k in EXCEPTIONS if k not in restantes]
        self.assertEqual(inutiles, [], "exception devenue inutile : retirez-la et "
                                       "baissez MAX_EXCEPTIONS")

    def test_le_lint_voit_ce_qu_il_doit_voir(self):
        self.assertEqual(len(os_accesses(
            '"""doc /proc"""\nimport sys, platform\nfrom platform import system\n'
            'x = sys.platform\ny = open("/proc/self/stat")\n')), 4)
        self.assertEqual(os_accesses('from . import platform\nplatform.is_linux()\n'), [])


if __name__ == "__main__":
    unittest.main()
