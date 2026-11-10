"""#6640 : la diffusion du canon n'envoie que les sections changées, avec la
référence `chemin@sha` de la fiche entière ; en information (`info`) sauf
pour le destinataire qu'une section changée nomme (nom, titre, alias, rôle)."""

from __future__ import annotations

from ameesh import canon, canon_sync, mail

from .support import PgTestCase
from .test_canon import _TmpMixin, commit_all, publish, write, EXAMPLE
from .test_l538_process_inject import RACINE, carte

SECTIONS = ("# Processus de lot\n\n"
            "## §1 Classes\n\n- une règle de classe\n\n"
            "## §2 Fusion\n\n- une règle de fusion\n\n"
            "## §3 Tests\n\n- une règle de test\n")




class CanonSectionsTest(_TmpMixin, PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.workspace = self.make_tmp()
        self.bare, self.clone = publish(self.workspace, "acme", EXAMPLE)
        write(self.clone, RACINE, carte(body=SECTIONS))
        commit_all(self.clone, "règles de travail")
        self.sync()

    def sync(self) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, canon.load(self.clone), "atelier")

    def change(self, body: str, **fields) -> None:
        write(self.clone, RACINE, carte(body=body, **fields))
        commit_all(self.clone, "règle changée")

    def recus(self, name: str) -> list[dict]:
        return mail.unread(self.db, name)

    def test_seule_la_section_changee_part_avec_chemin_sha(self):
        self.change(SECTIONS.replace("une règle de fusion", "une fusion neuve"))
        report = self.sync()
        head = canon.load(self.clone).sources[0].commit
        recu = self.recus("relecteur")
        self.assertEqual(len(recu), 1, recu)
        corps = recu[0]["body"]
        self.assertIn("une fusion neuve", corps)
        self.assertIn("§2 Fusion", corps)
        self.assertNotIn("une règle de classe", corps)
        self.assertNotIn("une règle de test", corps)
        self.assertIn("%s@%s" % (RACINE, head), corps)
        payload = mail.message_payload(recu[0])
        self.assertEqual(payload["sections"], ["§2 Fusion"])
        self.assertFalse(payload["full"])
        self.assertEqual(report.processes.sent, 2)

    def test_role_non_touche_en_information(self):
        self.change(SECTIONS.replace("une règle de fusion", "une fusion neuve"))
        report = self.sync()
        for name in ("orchestre", "relecteur"):
            recu = self.recus(name)[0]
            self.assertEqual(mail.passive_reason(recu), "info", name)
        self.assertEqual(report.processes.informed, 2)

    def test_destinataire_nomme_est_reveille(self):
        self.change(SECTIONS.replace("une règle de fusion",
                                     "le Relecteur rend son avis sous 1 h"))
        report = self.sync()
        self.assertEqual(mail.passive_reason(self.recus("relecteur")[0]), "")
        self.assertEqual(mail.passive_reason(self.recus("orchestre")[0]), "info")
        self.assertEqual(report.processes.informed, 1)

    def test_role_declare_touche_reveille(self):
        lu = canon.load(self.clone)
        agent = lu.agent("orchestre")
        agent.roles = ["implementation"]
        tokens = canon_sync._recipient_tokens(lu, "orchestre")
        self.assertIn("orchestre", tokens)
        self.assertIn("implementation", tokens)
        # un rôle déclaré se lit en mot entier, sans accents ni casse
        self.assertTrue(canon_sync._touches("- l'Implémentation suit §3", tokens))
        self.assertFalse(canon_sync._touches("- une réimplementation-test", tokens))

    def test_plusieurs_sections_changees(self):
        body = (SECTIONS.replace("une règle de classe", "une classe neuve")
                .replace("## §3 Tests\n\n- une règle de test\n", "")
                + "\n## §4 Files\n\n- une file neuve\n")
        self.change(body)
        self.sync()
        recu = self.recus("orchestre")
        self.assertEqual(len(recu), 1, recu)
        corps = recu[0]["body"]
        self.assertIn("une classe neuve", corps)
        self.assertIn("une file neuve", corps)
        self.assertIn("§3 Tests", corps)            # retirée : titre seul
        self.assertNotIn("une règle de fusion", corps)
        self.assertEqual(sorted(mail.message_payload(recu[0])["sections"]),
                         ["§1 Classes", "§3 Tests", "§4 Files"])

    def test_aucun_changement_aucun_courrier(self):
        # seul `updated:` bouge : aucune section changée, aucun courrier
        self.change(SECTIONS, updated="2026-11-10")
        report = self.sync()
        self.assertEqual(self.recus("orchestre"), [])
        self.assertEqual(self.recus("relecteur"), [])
        self.assertEqual(report.processes.sent, 0)

    def test_titres_homonymes_et_blocs_de_code(self):
        sections = canon_sync._process_sections(
            "# A\n\nx\n\n```\n# pas un titre\n```\n\n# A\n\ny\n")
        self.assertEqual([k for k, _ in sections], ["A", "A #2"])
        self.assertIn("# pas un titre", sections[0][1])
