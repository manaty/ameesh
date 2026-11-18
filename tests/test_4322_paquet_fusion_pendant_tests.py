# SPDX-License-Identifier: AGPL-3.0-only
"""#4322 (panier #4304) — un paquet fusionné au vert après ses tests par unité.

Depuis #7740, la ci-full ne part qu'après les séries directes et la poussée :
la PR ne peut plus fusionner *pendant* les tests. Lue fusionnée juste après la
poussée, ses unités sont sur la cible : paquet-auto ne recompose alors rien,
consigne le résultat au porteur et finit sur `MERGED_LINE` ; un rouge vu avant
la poussée a déjà écarté son unité, donc le chemin fusionné ne rapporte que le
vert restant, sans courrier « corriger en avant ou revert ». paquet-horaire ne
publie pas (ni « verdict non requis » ni ci-full) sur cette ligne, ni sur un
porteur déjà vu fusionné.
"""
from __future__ import annotations

import contextlib
import io
import unittest

from tests import test_3845_paquet_horaire as horaire
from tests.test_4304_paquet_poussee_precoce import Flux, green, paquet_auto

ph = horaire.ph


class FusionPendantLesTestsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.two = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40),
                   paquet_auto.Unit(2, "c" * 40, "d" * 40)],
            discarded=[], head="e" * 40)
        self.one = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="f" * 40)

    def run_flux(self, flux: Flux) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = flux.run()
        return rc, out.getvalue()

    def test_rouge_avant_poussee_est_ecarte_et_la_fusion_dit_vert(self) -> None:
        # #7740 : le rouge est vu avant la poussée, son unité est retirée de la
        # composition ; seule la tête recomposée est poussée, puis fusionne au
        # vert. Le pilote ne reçoit pas « corriger en avant ou revert » : le
        # rouge n'a jamais atteint la cible.
        removal = paquet_auto.Removal(2, tests=["t"], attributed=True)
        red = paquet_auto.TestRun("unité #2", ["tests.test_x"], 1, "FAIL: t\n", 0.1)
        flux = Flux(self, tests=[([red], [2], [removal]), ([green()], [], [])],
                    compositions=[self.two, self.one], states=("MERGED",))
        rc, out = self.run_flux(flux)
        self.assertEqual(rc, 0)
        # le rouge n'a rien poussé : une seule poussée, celle du vert recomposé
        self.assertEqual(len(flux.calls("push")), 1)
        self.assertEqual(len(flux.calls("recompose")), 1)
        self.assertEqual(flux.calls("withdraw"), [])
        note = next(call[1][2] for call in flux.calls("note")
                    if not call[1][2].startswith("paquet-auto: contrats "))
        self.assertIn("fusionnée au vert pendant les tests", note)
        self.assertIn("tests par unité verts", note)
        self.assertNotIn("rouges pour", note)
        # le rouge est consigné au manifeste de la tête poussée, pas dans un
        # courrier de rouge après fusion
        self.assertIn("#2", flux.calls("pr")[0][1][4])
        self.assertIn("tests ciblés rouges", flux.calls("pr")[0][1][4])
        self.assertEqual(flux.calls("mail"), [])
        self.assertEqual(out.strip().splitlines()[-1].split(" : ")[0],
                         paquet_auto.MERGED_LINE)

    def test_verts_apres_fusion_ni_corps_de_pr_ni_courrier(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])], compositions=[self.two],
                    states=("MERGED",))
        rc, out = self.run_flux(flux)
        self.assertEqual(rc, 0)
        # une seule PR : celle de la poussée ; le corps final n'est pas réécrit
        self.assertEqual(len(flux.calls("pr")), 1)
        self.assertEqual(flux.calls("mail"), [])
        note = next(call[1][2] for call in flux.calls("note")
                    if not call[1][2].startswith("paquet-auto: contrats "))
        self.assertIn("tests par unité verts", note)
        self.assertTrue(out.strip().splitlines()[-1].startswith(paquet_auto.MERGED_LINE))


class HoraireNePubliePasTest(horaire.Base):
    def test_after_compose_sur_la_ligne_de_fusion_ne_publie_pas(self) -> None:
        self.shell.items[4000] = horaire.carrier()
        result = ph.Result(0, "poussée\n%s : integration/paquet-h-x, tête aaaa\n"
                           % paquet_auto.MERGED_LINE)
        got = ph.after_compose(self.ctx, 4000, "h-20261027-04", result,
                               ph.Attempt(ph.COMPOSITION, 1, ""))
        self.assertIn("rien à publier", got)
        self.assertEqual(self.shell.ran("ci", "run"), [])
        self.assertEqual(self.shell.ran("work", "reviewer"), [])

    def test_publish_sur_un_porteur_deja_fusionne(self) -> None:
        for state in ("merged", "promoted"):
            item = dict(horaire.carrier(), state=state)
            self.assertIn("déjà fusionné", ph.publish(self.ctx, item))
        item = dict(horaire.carrier(), state="closed", close_reason="abandoned")
        self.assertIn("déjà clos", ph.publish(self.ctx, item))
        self.assertEqual(self.shell.ran("ci", "run"), [])
        self.assertEqual(self.shell.ran("work", "reviewer"), [])
        self.assertEqual(self.shell.mails, [])

    def test_publish_d_un_porteur_ouvert_inchange(self) -> None:
        self.assertIn("ci-full demandée", ph.publish(self.ctx, horaire.carrier()))
        self.assertTrue(self.shell.ran("ci", "run", "4000", "--full"))


if __name__ == "__main__":
    unittest.main()
