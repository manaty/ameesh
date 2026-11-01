# SPDX-License-Identifier: AGPL-3.0-only
"""CTX-05b (#5135) : rejeu contrefactuel de CTX-04a et CTX-04b sur le corpus.

Témoins : R1 à R5 à leur attendu cible (étude §6.1), scénarios multi-tours,
projection incomplète sans décision, déterminisme, observé et contrefactuel
jamais confondus. Les décisions viennent des fonctions livrées.
"""
import copy
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from ameesh import context_corpus, context_replay, resident_runner, runner

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ctx05")


def fixture(name):
    return context_corpus.read_corpus(os.path.join(FIXTURES, name + ".jsonl"))


def candidates(match=None, **settings):
    return context_replay.load_candidates(
        {"candidates": [{"id": "c1", "match": match or {}, "settings": settings}]})


def turn(base, turn_id, **measures):
    """Un tour dérivé de `base`, avec ces valeurs de mesure."""
    line = copy.deepcopy(base)
    line["turn_id"] = turn_id
    for name, value in measures.items():
        line[name]["value"] = value
    return line


def resident(line):
    line = copy.deepcopy(line)
    line["mode"] = context_replay.RESIDENT_MODE
    # la compaction publiée et absente : sinon la mesure reste inconnue
    line["decision"]["compacted"] = False
    line["decision"]["compaction"]["observed"] = False
    return line


WINDOW = dict(window_maintenance_share=0.5, window_input_reserve=10_000,
              window_output_reserve=10_000)


class ReproductionsTest(unittest.TestCase):
    def test_r1_la_fenetre_est_celle_du_principal(self):
        line = resident(fixture("r1_model_usage_order")[0])
        cf = context_replay.replay_turns([line], candidates(**WINDOW))[0]["counterfactual"]
        self.assertEqual(cf["window"]["W"], 1_000_000)
        self.assertIs(cf["window"]["near"], False)
        self.assertEqual(cf["decision"], "none")
        # sur la fenêtre de l'auxiliaire, la même projection aurait décidé
        aux = copy.deepcopy(line)
        aux["W"]["value"] = 200_000
        cf_aux = context_replay.replay_turns([aux], candidates(**WINDOW))[0]["counterfactual"]
        self.assertEqual(cf_aux["decision"], "compaction")

    def test_r2_ecriture_de_cache_dans_c_last_hors_de_r_turn(self):
        line = resident(fixture("r2_cache_creation")[0])
        line["W"].update(value=40_000, quality="measured", reasons=[])
        cf = context_replay.replay_turns([line], candidates(
            turn_reread_budget=22_000, window_maintenance_share=0.6,
            window_input_reserve=0, window_output_reserve=0))[0]["counterfactual"]
        # R_turn 21 k sous le budget, malgré 24 k de contexte
        self.assertIsNone(cf["reread"])
        # 24 k + 200 de sortie : l'écriture compte dans la projection
        self.assertEqual(cf["window"]["C_next"], 24_200)
        self.assertEqual(cf["decision"], "compaction")

    def test_r3_codex_la_somme_des_appels_ne_tourne_pas(self):
        line = fixture("r3_codex_ten_calls")[0]
        cf = context_replay.replay_turns([line], candidates(
            context_max_tokens=400_000, session_min_turns=3,
            turn_reread_budget=400_000))[0]["counterfactual"]
        self.assertIsNone(cf["guard"])
        self.assertEqual(cf["reread"]["measure"], "R_turn")
        self.assertEqual(cf["decision"], "close")

    def test_r4_budget_cumule_cloture_sans_rotation_de_fenetre(self):
        lines = [resident(line) for line in fixture("r4_cumulative_budget")]
        report = context_replay.replay_turns(lines, candidates(
            session_reread_budget=400_000, session_min_turns=3,
            session_max_tokens=1_000_000, **WINDOW))
        self.assertEqual([t["counterfactual"]["decision"] for t in report],
                         ["none", "none", "close"])
        last = report[-1]["counterfactual"]
        self.assertEqual(last["reread"]["measure"], "R_session")
        self.assertEqual(last["window"]["W"], 1_000_000)
        self.assertIs(last["window"]["near"], False)
        self.assertEqual(len({t["session"] for t in report}), 1)

    def test_r5_meme_s_hist_decisions_distinctes_par_harnais(self):
        lines = fixture("r5_session_guard")
        self.assertEqual({line["S_hist"]["value"] for line in lines}, {150_000})
        table = context_replay.load_candidates({"candidates": [
            {"id": "claude", "match": {"harness": "claude"},
             "settings": {"session_min_turns": 3}},
            {"id": "codex", "match": {"harness": "codex"},
             "settings": {"session_min_turns": 3, "session_max_tokens": 400_000}},
        ]})
        decisions = {t["profile"]["harness"]: t["counterfactual"]["decision"]
                     for t in context_replay.replay_turns(lines, table)}
        self.assertEqual(decisions, {"claude": "rotation", "codex": "none"})
        # 150 k reste le repli commun sans réglage propre au harnais
        repli = context_replay.replay_turns(lines, candidates(session_min_turns=3))
        self.assertEqual({t["counterfactual"]["cause"] for t in repli}, {"session_size"})
        # chaque harnais a sa synthèse : aucun total entre harnais
        synth = context_replay.replay(lines, table)["synthesis"]
        self.assertEqual({entry["profile"]["harness"] for entry in synth},
                         {"claude", "codex"})
        self.assertEqual(len(synth), 2)


class MultiToursTest(unittest.TestCase):
    def test_session_longue_claude_compaction_puis_divergence(self):
        base = resident(fixture("r4_cumulative_budget")[0])
        lines = [turn(base, "t%d" % i, C_last=c) for i, c in enumerate(
            (100_000, 300_000, 480_000, 600_000, 700_000))]
        report = context_replay.replay_turns(lines, candidates(**WINDOW))
        self.assertEqual([t["counterfactual"]["decision"] for t in report],
                         ["none", "none", "compaction", None, None])
        self.assertEqual(report[3]["counterfactual"]["unknowns"],
                         [context_replay.UNKNOWN_DIVERGED])

    def test_session_longue_claude_compaction_observee_puis_renouvellement(self):
        base = resident(fixture("r4_cumulative_budget")[0])
        compacte = turn(base, "t2", C_last=480_000)
        compacte["decision"].update(observed="compaction", cause="auto", compacted=True, compaction={
            "pre_tokens": 480_000, "post_tokens": 400_000, "quality": "measured",
            "reasons": [], "trigger": "auto", "requested": True, "observed": True})
        lines = [turn(base, "t1", C_last=100_000), compacte,
                 turn(base, "t3", C_last=490_000)]
        report = context_replay.replay_turns(lines, candidates(**WINDOW))
        self.assertEqual([t["counterfactual"]["decision"] for t in report],
                         ["none", "none", "rotation"])
        self.assertEqual(report[1]["counterfactual"]["window"]["near"], None)
        self.assertEqual(report[2]["counterfactual"]["cause"], "window_maintenance")
        # politique jamais : le compactage reste, le renouvellement non
        jamais = context_replay.replay_turns(
            lines, candidates(session_policy="jamais", **WINDOW))
        self.assertEqual(jamais[2]["counterfactual"]["decision"], "none")

    def test_retombee_sous_le_seuil_rouvre_le_compactage(self):
        base = resident(fixture("r4_cumulative_budget")[0])
        compacte = turn(base, "t1", C_last=480_000)
        compacte["decision"].update(compacted=True, compaction={
            "pre_tokens": 480_000, "post_tokens": 90_000, "quality": "measured",
            "reasons": [], "trigger": "auto", "requested": False, "observed": True})
        lines = [compacte, turn(base, "t2", C_last=90_000), turn(base, "t3", C_last=490_000)]
        report = context_replay.replay_turns(lines, candidates(**WINDOW))
        self.assertEqual(report[2]["counterfactual"]["decision"], "compaction")

    def test_codex_a_cumul_cloture_par_tranche_jamais_de_rotation(self):
        base = fixture("r3_codex_ten_calls")[0]
        lines = [turn(base, "t%d" % i, R_turn=120_000, R_session=120_000 * (i + 1),
                      R_session_base=120_000 * i, S_hist=120_000 * (i + 1)) for i in range(5)]
        report = context_replay.replay_turns(lines, candidates(
            session_reread_budget=250_000, session_min_turns=3,
            session_max_tokens=10_000_000))
        self.assertEqual([t["counterfactual"]["decision"] for t in report],
                         ["none", "none", "close", "none", "close"])
        self.assertTrue(all(t["counterfactual"]["guard"] is None for t in report))


class InconnuesTest(unittest.TestCase):
    def test_projection_incomplete_aucune_decision(self):
        base = resident(fixture("r4_cumulative_budget")[0])
        base["C_last"]["value"] = 990_000
        cas = {
            "C_last": lambda l: l["C_last"].update(quality="partial"),
            "O_last": lambda l: l["O_last"].update(value=None, quality="unknown"),
            "W": lambda l: l["W"].update(value=None, quality="unknown"),
        }
        for nom, altere in cas.items():
            with self.subTest(inconnue=nom):
                line = copy.deepcopy(base)
                altere(line)
                cf = context_replay.replay_turns([line], candidates(**WINDOW))[0]["counterfactual"]
                self.assertEqual(cf["decision"], "none")
                self.assertIn(nom, cf["unknowns"])
        for reglages, nom in (
                ({"window_maintenance_share": 0.5, "window_output_reserve": 0},
                 "G_input_reserve"),
                ({"window_maintenance_share": 0.5, "window_input_reserve": 0},
                 "O_reserve_next")):
            with self.subTest(inconnue=nom):
                cf = context_replay.replay_turns(
                    [base], candidates(**reglages))[0]["counterfactual"]
                self.assertEqual(cf["decision"], "none")
                self.assertIn(nom, cf["unknowns"])

    def test_compaction_non_publiee_aucune_decision(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        line["C_last"]["value"] = 990_000
        line["decision"]["compacted"] = None
        report = context_replay.replay([line], candidates(**WINDOW))
        cf = report["turns"][0]["counterfactual"]
        self.assertEqual(cf["decision"], "none")
        self.assertIn(context_replay.UNKNOWN_COMPACTED, cf["unknowns"])
        self.assertEqual(report["synthesis"][0]["judgeable"], {"C_last": 1, "window": 0})

    def test_fenetre_de_repli_reste_inconnue(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        line["C_last"]["value"] = 990_000
        line["W"]["quality"] = "fallback"
        cf = context_replay.replay_turns([line], candidates(**WINDOW))[0]["counterfactual"]
        self.assertEqual(cf["decision"], "none")
        self.assertIn("W", cf["unknowns"])

    def test_non_residente_pas_d_entretien_de_fenetre(self):
        line = fixture("r4_cumulative_budget")[0]
        line["C_last"]["value"] = 990_000
        cf = context_replay.replay_turns([line], candidates(**WINDOW))[0]["counterfactual"]
        self.assertEqual(cf["decision"], "none")
        self.assertIn(context_replay.UNKNOWN_RESIDENT, cf["unknowns"])

    def test_autre_harnais_compaction_native_nommee(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        line["harness"] = "codex"
        line["C_last"]["value"] = 900_000
        cf = context_replay.replay_turns([line], candidates(**WINDOW))[0]["counterfactual"]
        self.assertEqual(cf["decision"], "none")
        self.assertIn("native_compaction", cf["unknowns"])

    def test_mesure_de_relecture_absente_aucune_cloture(self):
        line = fixture("r4_cumulative_budget")[0]
        line["R_turn"].update(value=None, quality="unknown")
        line["R_session_base"].update(value=None, quality="unknown")
        cf = context_replay.replay_turns([line], candidates(
            turn_reread_budget=1, session_reread_budget=1))[0]["counterfactual"]
        self.assertIsNone(cf["reread"])
        self.assertEqual(cf["unknowns"][-2:], [context_replay.UNKNOWN_R_TURN,
                                               context_replay.UNKNOWN_R_SESSION_BASE])

    def test_minimum_de_tours_non_declare_pas_de_garde_de_taille(self):
        line = fixture("r5_session_guard")[0]
        cf = context_replay.replay_turns([line], candidates())[0]["counterfactual"]
        self.assertEqual(cf["decision"], "none")
        self.assertIn(context_replay.UNKNOWN_MIN_TURNS, cf["unknowns"])

    def test_profil_sans_candidat_non_rejoue(self):
        line = fixture("r4_cumulative_budget")[0]
        report = context_replay.replay([line], candidates(match={"harness": "codex"}))
        self.assertIsNone(report["turns"][0]["counterfactual"]["decision"])
        self.assertEqual(report["synthesis"][0]["not_replayed"], 1)
        self.assertEqual(report["synthesis"][0]["counterfactual"], {})
        self.assertIn(context_replay.UNKNOWN_NO_CANDIDATE,
                      report["synthesis"][0]["unknowns"])


class SeparationTest(unittest.TestCase):
    def test_observe_et_contrefactuel_jamais_confondus(self):
        line = fixture("r4_cumulative_budget")[0]
        settings = candidates(turn_reread_budget=40_000)
        reference = context_replay.replay_turns([line], settings)[0]
        self.assertEqual(reference["observed"], {"decision": "none", "cause": None})
        self.assertEqual(reference["counterfactual"]["decision"], "close")
        # changer l'observé ne change pas le contrefactuel
        altere = copy.deepcopy(line)
        altere["decision"].update(observed="rotation", cause="context_cap",
                                  rotation={"kind": "context_cap"})
        autre = context_replay.replay_turns([altere], settings)[0]
        self.assertEqual(autre["observed"]["decision"], "rotation")
        self.assertEqual(autre["counterfactual"], reference["counterfactual"])
        # changer le réglage candidat ne change pas l'observé
        eteint = context_replay.replay_turns([line], candidates())[0]
        self.assertEqual(eteint["counterfactual"]["decision"], "none")
        self.assertEqual(eteint["observed"], reference["observed"])

    def test_la_fenetre_observee_n_entre_pas_dans_le_rejeu(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        line["decision"]["window"] = {
            "C_last": 1, "O_last": 1, "W": 2, "C_next_input": 2, "C_next": 2,
            "O_reserve_next": 0, "share": 0.1, "near": True, "decision": "compact",
            "unknowns": []}
        cf = context_replay.replay_turns([line], candidates(**WINDOW))[0]["counterfactual"]
        self.assertEqual(cf["decision"], "none")
        self.assertEqual(cf["window"]["W"], 1_000_000)

    def test_ordre_de_maybe_rotate_plafond_fenetre_puis_taille(self):
        base = resident(fixture("r4_cumulative_budget")[0])
        line = turn(base, "t1", C_last=600_000, S_hist=150_000)
        line["legacy"]["session_turns"] = 3
        reglages = dict(session_min_turns=3, session_max_tokens=150_000, **WINDOW)
        fenetre = context_replay.replay_turns([line], candidates(**reglages))[0]
        self.assertEqual(fenetre["counterfactual"]["decision"], "compaction")
        self.assertIsNone(fenetre["counterfactual"]["guard"])
        plafond = context_replay.replay_turns(
            [line], candidates(context_max_tokens=500_000, **reglages))[0]
        self.assertEqual(plafond["counterfactual"]["cause"], "context_cap")
        taille = turn(base, "t1", C_last=50_000, S_hist=150_000)
        taille["legacy"]["session_turns"] = 3
        sous_le_seuil = context_replay.replay_turns([taille], candidates(**reglages))[0]
        self.assertEqual(sous_le_seuil["counterfactual"]["cause"], "session_size")

    def test_plafond_repli_sur_r_turn_jamais_pour_codex(self):
        line = fixture("r4_cumulative_budget")[0]
        line["C_last"].update(value=None, quality="unknown")
        cf = context_replay.replay_turns(
            [line], candidates(context_max_tokens=40_000))[0]["counterfactual"]
        self.assertEqual(cf["guard"]["measure"], "R_turn")
        codex = fixture("r3_codex_ten_calls")[0]
        codex["C_last"].update(value=None, quality="unknown")
        cf = context_replay.replay_turns(
            [codex], candidates(context_max_tokens=40_000, session_min_turns=3))[0]["counterfactual"]
        self.assertIsNone(cf["guard"])
        self.assertIn(context_replay.UNKNOWN_C_LAST, cf["unknowns"])

    def test_jugeable_ne_depend_pas_des_reglages(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        turn_report = context_replay.replay_turns([line], candidates())[0]
        self.assertEqual(turn_report["judgeable"], {"C_last": True, "window": True})

    def test_les_fonctions_livrees_sont_appelees(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        with mock.patch.object(resident_runner, "window_maintenance",
                               wraps=resident_runner.window_maintenance) as fenetre, \
                mock.patch.object(runner.AgentWorker, "reread_budget_due",
                                  wraps=runner.AgentWorker.reread_budget_due) as budget:
            context_replay.replay_turns([line], candidates(turn_reread_budget=1, **WINDOW))
        fenetre.assert_called_once()
        budget.assert_called_once()

    def test_ecarts_de_rotation_par_session(self):
        base = fixture("r5_session_guard")[0]
        evitee = turn(base, "a1", S_hist=10)
        evitee["session"] = "sha256:" + "a" * 32
        suivante = turn(base, "b1", S_hist=10)
        suivante["session"] = "sha256:" + "b" * 32
        suivante["decision"].update(
            observed="rotation", cause="session_size",
            rotation={"kind": "session_size", "previous_session": evitee["session"]})
        ajoutee = turn(base, "c1")
        ajoutee["session"] = "sha256:" + "c" * 32
        report = context_replay.replay([evitee, suivante, ajoutee],
                                       candidates(session_min_turns=3))
        self.assertEqual(report["synthesis"][0]["rotations"],
                         {"avoided": 1, "added": 1, "both": 0, "unknown": 0})


    def _session_tournee(self, *premiers):
        """Ces tours en session A, puis un tour de B qui dit la rotation de A."""
        suivante = copy.deepcopy(premiers[-1])
        suivante["turn_id"] = "b1"
        suivante["session"] = "sha256:" + "b" * 32
        suivante["decision"].update(
            observed="rotation", cause="session_size",
            rotation={"kind": "session_size", "previous_session": premiers[0]["session"]})
        return list(premiers) + [suivante]

    def _rotations(self, lines, settings):
        return context_replay.replay(lines, settings)["synthesis"][0]["rotations"]

    def test_fenetre_inconnue_ne_prouve_pas_une_rotation_evitee(self):
        line = resident(fixture("r4_cumulative_budget")[0])
        line["W"].update(value=None, quality="unknown")
        rotations = self._rotations(self._session_tournee(line),
                                    candidates(session_min_turns=3, **WINDOW))
        self.assertEqual(rotations["avoided"], 0)
        self.assertEqual(rotations["unknown"], 2)

    def test_divergence_apres_compaction_ne_prouve_pas_une_rotation_evitee(self):
        base = resident(fixture("r4_cumulative_budget")[0])
        lines = self._session_tournee(turn(base, "t1", C_last=480_000),
                                      turn(base, "t2", C_last=490_000))
        report = context_replay.replay(lines, candidates(session_min_turns=3, **WINDOW))
        self.assertEqual(report["turns"][1]["counterfactual"]["decision"], None)
        rotations = report["synthesis"][0]["rotations"]
        self.assertEqual(rotations["avoided"], 0)
        self.assertEqual(rotations["unknown"], 1)

    def test_garde_inconnue_ne_prouve_pas_une_rotation_evitee(self):
        line = fixture("r4_cumulative_budget")[0]
        rotations = self._rotations(self._session_tournee(line), candidates())
        self.assertEqual(rotations["avoided"], 0)

    def test_inconnue_de_budget_ou_hors_residence_n_empeche_pas_de_conclure(self):
        line = fixture("r4_cumulative_budget")[0]
        line["R_turn"].update(value=None, quality="unknown")
        line["W"].update(value=None, quality="unknown")
        rotations = self._rotations(self._session_tournee(line), candidates(
            session_min_turns=3, turn_reread_budget=1, **WINDOW))
        self.assertEqual(rotations["avoided"], 1)
        self.assertEqual(rotations["unknown"], 0)

    def test_rotation_observee_alignee_sur_la_fin_de_sa_session(self):
        base = fixture("r5_session_guard")[0]
        fin = turn(base, "a1")
        fin["session"] = "sha256:" + "a" * 32
        suivante = turn(base, "b1", S_hist=10)
        suivante["session"] = "sha256:" + "b" * 32
        suivante["decision"].update(
            observed="rotation", cause="session_size",
            rotation={"kind": "session_size", "previous_session": fin["session"]})
        report = context_replay.replay([fin, suivante], candidates(session_min_turns=3))
        # la ligne brute reste celle du corpus ; la comparaison, elle, est alignée
        self.assertEqual(report["turns"][1]["observed"]["decision"], "rotation")
        self.assertEqual(report["turns"][0]["observed"]["decision"], "none")
        entry = report["synthesis"][0]
        self.assertEqual((entry["same"], entry["different"]), (2, 0))
        self.assertEqual(entry["rotations"], {"avoided": 0, "added": 0, "both": 1, "unknown": 0})


class DeterminismeEtSortieTest(unittest.TestCase):
    def _corpus(self):
        return [line for name in ("r1_model_usage_order", "r2_cache_creation",
                                  "r3_codex_ten_calls", "r4_cumulative_budget",
                                  "r5_session_guard") for line in fixture(name)]

    def test_deux_rejeux_identiques(self):
        settings = candidates(turn_reread_budget=40_000, session_min_turns=3, **WINDOW)
        self.assertEqual(
            context_replay.dumps_report(context_replay.replay(self._corpus(), settings)),
            context_replay.dumps_report(context_replay.replay(self._corpus(), settings)))

    def test_l_ordre_de_session_suit_session_seq(self):
        lines = fixture("r4_cumulative_budget")
        settings = candidates(session_reread_budget=400_000)
        self.assertEqual(context_replay.replay_turns(list(reversed(lines)), settings),
                         context_replay.replay_turns(lines, settings))

    def test_le_corpus_n_est_pas_modifie(self):
        lines = self._corpus()
        avant = copy.deepcopy(lines)
        context_replay.replay(lines, candidates(turn_reread_budget=1, **WINDOW))
        self.assertEqual(lines, avant)

    def test_reglages_refuses(self):
        for raw in ({}, {"candidates": []},
                    {"candidates": [{"id": "a", "settings": {"inconnu": 1}}]},
                    {"candidates": [{"id": "a", "settings": {"turn_reread_budget": -1}}]},
                    {"candidates": [{"id": "a", "settings": {"turn_reread_budget": True}}]},
                    {"candidates": [{"id": "a", "settings": {"window_maintenance_share": 2}}]},
                    {"candidates": [{"id": "a", "match": {"host": "x"}, "settings": {}}]},
                    {"candidates": [{"id": "a", "settings": {}}, {"id": "a", "settings": {}}]}):
            with self.subTest(raw=raw), self.assertRaises(context_replay.ReplayError):
                context_replay.load_candidates(raw)

    def test_commande_rapport_hors_depot_0600(self):
        corpus = os.path.join(FIXTURES, "r4_cumulative_budget.jsonl")
        with tempfile.TemporaryDirectory() as tmp:
            reglages = os.path.join(tmp, "c.json")
            with open(reglages, "w", encoding="utf-8") as handle:
                json.dump({"candidates": [{"id": "a", "settings": {"turn_reread_budget": 1}}]},
                          handle)
            out = os.path.join(tmp, "r.json")
            argv = ["--corpus", corpus, "--candidates", reglages, "--out", out]
            with mock.patch("sys.stdout"):
                self.assertEqual(context_replay.main(argv), 0)
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
            with open(out, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["schema"], context_replay.SCHEMA)
            with mock.patch("sys.stderr"):
                self.assertEqual(context_replay.main(argv), 1)  # déjà présent

    def test_rapport_refuse_dans_un_depot(self):
        depot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with self.assertRaises(context_replay.ReplayError):
            context_replay.write_report(os.path.join(depot, "r.json"), {})


if __name__ == "__main__":
    unittest.main()
