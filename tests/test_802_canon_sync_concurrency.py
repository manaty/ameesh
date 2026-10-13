# SPDX-License-Identifier: AGPL-3.0-only
"""#802 : les syncs concurrents d'un même canon ne s'empilent plus sur les
verrous de ligne de `work_packages` (statement timeout, puis « canon not read
in full, no claim » : plus aucun tour ne démarre sur l'hôte).

Le témoin reproduit la course réelle : N exécutants d'un même hôte lancent le
premier sync d'un canon neuf en même temps. Sans sérialisation, chaque
transaction lit le même état initial, rejoue les mêmes insertions et attend les
verrous de ligne de la première : avec assez d'exécutants, l'attente d'une
instruction dépasse statement_timeout (le sync échoue, la porte d'hôte reste
fermée). Avec le verrou consultatif pris AVANT la lecture, une seule
transaction écrit, les suivantes relisent l'état à jour et n'écrivent rien.
"""
from __future__ import annotations

import os
import tempfile
import threading

from ameesh import canon, canon_sync

from .support import PgTestCase

#: Assez d'exécutants pour que la file de verrous de ligne dépasse
#: statement_timeout sans sérialisation : l'attente de la dernière instruction
#: croît avec le nombre d'exécutants, alors que l'attente du verrou consultatif
#: ne dure que le sync du premier (~PACKAGES × ROW_SLEEP_S).
WORKERS = 12
PACKAGES = 4
#: Ralentit chaque écriture de fiche : sans cela, les transactions d'une base
#: de test sont trop courtes pour se chevaucher, alors que la course réelle
#: dure le temps de lire tout le registre et de recopier les fiches.
ROW_SLEEP_S = 0.5
STATEMENT_TIMEOUT_MS = 6000


def write_canon(root: str) -> None:
    for index in range(PACKAGES):
        path = os.path.join(root, "plan", "p%d.md" % index)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("---\ntype: WorkPackage\ntitle: Paquet %d\nkind: epic\n"
                     "responsible: human:alice\n---\n" % index)


class CanonSyncConcurrencyTest(PgTestCase):

    def test_concurrent_syncs_of_one_canon_all_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_canon(tmp)
            loaded = canon.load(tmp, untrusted=True)
        self.db.execute(
            "CREATE OR REPLACE FUNCTION t802_slow_package() RETURNS trigger LANGUAGE "
            "plpgsql AS $$ BEGIN PERFORM pg_sleep(%s); RETURN NEW; END $$" % ROW_SLEEP_S)
        self.db.execute(
            "CREATE TRIGGER t802_slow_package BEFORE INSERT OR UPDATE ON work_packages "
            "FOR EACH ROW EXECUTE FUNCTION t802_slow_package()")
        # #802 R1 : le délai se met dans la Config de la connexion, pas par un
        # `SET` après coup. Avec le pilote psql, chaque `execute` ouvre son
        # propre sous-processus : un `SET` isolé meurt avec lui et le sync
        # retomberait sur le délai par défaut (30 s), si bien que le témoin ne
        # prouverait plus que c'est l'attente des verrous qui dépasse le délai.
        # On atteste donc le délai effectif de la session, pour les deux
        # pilotes, avant de lancer la course.
        attest = self.connect(statement_timeout_ms=STATEMENT_TIMEOUT_MS)
        try:
            row = attest.query("SELECT setting, unit FROM pg_settings "
                               "WHERE name = 'statement_timeout'")[0]
        finally:
            attest.close()
        self.assertEqual((row["setting"], row["unit"]),
                         (str(STATEMENT_TIMEOUT_MS), "ms"),
                         "délai de session non appliqué par le pilote %s"
                         % self.db.name)

        barrier = threading.Barrier(WORKERS, timeout=30)
        results: list = []
        errors: list = []
        lock = threading.Lock()

        def attempt() -> None:
            db = self.connect(statement_timeout_ms=STATEMENT_TIMEOUT_MS)
            try:
                barrier.wait()
                report = canon_sync.sync_packages(db, loaded, [])
                outcome = ("ok", len(report.created), len(report.unchanged))
            except Exception as exc:  # noqa: BLE001
                outcome = ("error", type(exc).__name__, " ".join(str(exc).split())[:200])
            finally:
                db.close()
            with lock:
                (results if outcome[0] == "ok" else errors).append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [], "des syncs concurrents ont échoué")
        # Un seul sync écrit les fiches ; les autres les trouvent déjà à jour.
        self.assertEqual(sorted(created for _, created, _ in results),
                         [0] * (WORKERS - 1) + [PACKAGES], results)
        self.assertEqual(sum(unchanged for _, _, unchanged in results),
                         (WORKERS - 1) * PACKAGES, results)
