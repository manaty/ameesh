# SPDX-License-Identifier: AGPL-3.0-only
"""#831 : un seul verrou de synchronisation des paquets par schéma.

Les identifiants de `work_packages` sont globaux : le premier canon qui en
déclare un le garde (`canon-name-conflict`, L42/0031). Un verrou par canon
laissait deux canons déclarant le même identifiant lire tous deux « absent »
puis s'écraser ; la bascule du canon par défaut (`rebase_default`) écrivait
`work_packages.canon` sans ce verrou, donc un sync du canon par défaut parti
avant la bascule pouvait réécrire NULL après elle ; enfin l'attente du verrou
comptait dans `statement_timeout`, si bien qu'un premier sync long (déploiement
qui change beaucoup de fiches) faisait échouer les suivants en 57014 — la porte
d'hôte restait fermée jusqu'au réessai.

Témoins, tous sur la course réelle : deux canons déclarant le même identifiant
(un seul écrit, l'autre constate le conflit) ; `rebase_default` attend le sync
en cours ; l'attente du verrou ne compte pas dans le délai de requête de la
session.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

from ameesh import canon, canon_sync
from ameesh.storage import of as storage_of

from .support import PgTestCase

#: Deux fiches suffisent : chaque écriture prend ROW_SLEEP_S, la transaction du
#: sync reste ouverte assez longtemps pour que les autres s'y heurtent.
PACKAGES = 2
ROW_SLEEP_S = 0.5
#: Délai de requête de la session qui attend le verrou : plus court que la
#: tenue du sync lent (PACKAGES × ROW_SLEEP_S = 1 s), donc l'attente le
#: dépasserait si elle y comptait.
STATEMENT_TIMEOUT_MS = 800


def write_canon(root: str, packages: int) -> None:
    for index in range(packages):
        path = os.path.join(root, "plan", "p%d.md" % index)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("---\ntype: WorkPackage\ntitle: Paquet %d\nkind: epic\n"
                     "responsible: human:alice\n---\n" % index)


class PackagesLockScopeTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        # Ralentit toute écriture de fiche : sans cela, les transactions d'une
        # base de test sont trop courtes pour se chevaucher.
        self.db.execute(
            "CREATE OR REPLACE FUNCTION t831_slow_package() RETURNS trigger LANGUAGE "
            "plpgsql AS $$ BEGIN PERFORM pg_sleep(%s); RETURN NEW; END $$" % ROW_SLEEP_S)
        self.db.execute(
            "CREATE TRIGGER t831_slow_package BEFORE INSERT OR UPDATE ON work_packages "
            "FOR EACH ROW EXECUTE FUNCTION t831_slow_package()")
        self.addCleanup(self._drop_slow_package)

    def _drop_slow_package(self) -> None:
        self.db.execute("DROP TRIGGER IF EXISTS t831_slow_package ON work_packages")
        self.db.execute("DROP FUNCTION IF EXISTS t831_slow_package()")

    # -- utilitaires ---------------------------------------------------------
    def _sync_in_thread(self, loaded, **overrides):
        """Un sync dans son propre fil, sa transaction ouverte ; rend
        (fil, état) où l'état porte le pid du backend et son compte rendu."""
        state: dict = {"pid": None, "report": None, "error": None}
        ready = threading.Event()

        def run() -> None:
            db = self.connect(**overrides)
            try:
                # La transaction est ouverte par le témoin : le pid est lu dans
                # la session du sync (le pilote psql en ouvre une par
                # transaction), et le sync s'y imbrique.
                with db.transaction() as tx:
                    state["pid"] = tx.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
                    ready.set()
                    # `tx` : le pilote psql rend la transaction (pas la
                    # connexion) ; le sync doit s'y imbriquer pour tenir le
                    # verrou dans la session dont on a lu le pid.
                    state["report"] = canon_sync.sync_packages(tx, loaded, [])
            except Exception as exc:  # noqa: BLE001
                state["error"] = exc
            finally:
                db.close()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(15), "le sync n'a pas ouvert sa transaction")
        return thread, state

    def _await_advisory_lock(self, pid, timeout: float = 15.0) -> None:
        """Attend que le backend `pid` tienne un verrou consultatif : le sync
        est alors dans sa transaction, son verrou pris."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.db.query(
                "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND objsubid = 1 AND pid = %s LIMIT 1", (pid,))
            if rows:
                return
            time.sleep(0.05)
        self.fail("le sync (pid %s) n'a pas pris son verrou consultatif" % pid)

    def _join(self, thread, state, timeout: float = 60.0) -> None:
        thread.join(timeout=timeout)
        self.assertFalse(thread.is_alive(), "le fil de sync ne s'est pas terminé")
        self.assertIsNone(state["error"], "le sync a échoué : %r" % (state["error"],))

    # -- témoins -------------------------------------------------------------
    def test_two_canons_declaring_one_id_write_it_once(self):
        """Deux canons, le même identifiant : un seul l'écrit, l'autre constate
        le conflit — jamais deux écritures qui s'écrasent."""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            write_canon(first, 1)
            write_canon(second, 1)
            default = canon.load(first, untrusted=True)
            other = canon.load(second, untrusted=True, default=False)
        self.assertTrue(other.id, "le canon nommé doit porter un identifiant")

        barrier = threading.Barrier(2, timeout=30)
        results: list = []
        errors: list = []
        guard = threading.Lock()

        def attempt(loaded) -> None:
            db = self.connect()
            try:
                barrier.wait()
                report = canon_sync.sync_packages(db, loaded, [])
                outcome = ("ok", list(report.created), list(report.conflicts))
            except Exception as exc:  # noqa: BLE001
                outcome = ("error", type(exc).__name__, " ".join(str(exc).split())[:200])
            finally:
                db.close()
            with guard:
                (results if outcome[0] == "ok" else errors).append(outcome)

        threads = [threading.Thread(target=attempt, args=(loaded,))
                   for loaded in (default, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [], "des syncs concurrents ont échoué")
        self.assertEqual(sorted(len(created) for _, created, _ in results), [0, 1],
                         results)
        conflicts = [conflict for _, _, conflict in results if conflict]
        self.assertEqual(len(conflicts), 1, results)
        self.assertEqual(conflicts[0], ["p0"], results)
        row = storage_of(self.db).packages.get("p0")
        self.assertIsNotNone(row)
        self.assertIn(row["canon"], (None, other.id),
                      "la fiche appartient au canon qui l'a écrite")

    def test_rebase_default_waits_for_the_running_sync(self):
        """La bascule du canon par défaut ne peut pas passer sous un sync : les
        lignes écrites NULL par ce sync reçoivent l'ancien identifiant, au lieu
        de rester NULL après la bascule."""
        with tempfile.TemporaryDirectory() as root:
            write_canon(root, PACKAGES)
            loaded = canon.load(root, untrusted=True)
        thread, state = self._sync_in_thread(loaded)
        self._await_advisory_lock(state["pid"])
        with self.db.transaction() as tx:
            storage_of(tx).canon.rebase_default("h831", "former")
        self._join(thread, state)
        rows = storage_of(self.db).packages.all()
        self.assertEqual([row["id"] for row in rows], ["p0", "p1"])
        self.assertEqual({row["canon"] for row in rows}, {"former"},
                         "des fiches écrites par le sync sont restées NULL")

    def test_lock_wait_does_not_count_against_statement_timeout(self):
        """L'attente du verrou ne compte pas dans `statement_timeout` : un sync
        qui suit un premier sync long aboutit, au lieu d'échouer en 57014."""
        with tempfile.TemporaryDirectory() as root:
            write_canon(root, PACKAGES)
            loaded = canon.load(root, untrusted=True)
        thread, state = self._sync_in_thread(loaded)
        self._await_advisory_lock(state["pid"])
        follower = self.connect(statement_timeout_ms=STATEMENT_TIMEOUT_MS)
        try:
            report = canon_sync.sync_packages(follower, loaded, [])
        finally:
            follower.close()
        self._join(thread, state, timeout=120)
        self.assertEqual(report.created, [], "le suiveur a réécrit des fiches")
        self.assertEqual(sorted(report.unchanged), ["p0", "p1"])
