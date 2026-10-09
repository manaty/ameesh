# SPDX-License-Identifier: AGPL-3.0-only
"""L50 : les alertes `orphan_lot` d'un même agent partent en UNE notification."""
import unittest

from ameesh import notify

from tests.test_l38_notify import _Base, _alert, _desktop_log


def _orphelin(lot, agent="coord", reason="sans_tour", **extra):
    return _alert("orphan_lot", agent=agent, since=None, lot=lot, title="lot %s" % lot,
                  reason=reason, detail="lot #%s (build) orphelin" % lot, **extra)


class GroupementTest(unittest.TestCase):
    def test_un_groupe_par_agent_raison_et_destinataire(self):
        resolve = lambda a: ("human:" + ("b" if a.get("lot") == 9 else "a"), "agent")
        alertes = [_orphelin(i) for i in range(1, 8)] + [
            _orphelin(9), _orphelin(3, agent="autre"), _orphelin(4, reason="arrete"),
            _alert()]
        out = notify.group_orphan_lots(alertes, resolve)
        groupes = sorted((a["agent"], a["reason"], a.get("responsible"), a["value"])
                         for a in out if a["type"] == "orphan_lot")
        self.assertEqual(groupes, [("autre", "sans_tour", "human:a", 1),
                                   ("coord", "arrete", "human:a", 1),
                                   ("coord", "sans_tour", "human:a", 7),
                                   ("coord", "sans_tour", "human:b", 1)])
        sept = [a for a in out if a.get("value") == 7][0]
        self.assertNotIn("lot", sept)
        self.assertNotIn("since", sept)
        self.assertIn("7 lot(s) orphelin(s) (sans_tour) : #1 « lot 1 »", sept["detail"])
        self.assertIn("et 2 autre(s)", sept["detail"])
        self.assertEqual([a["type"] for a in out].count("stopped_with_mail"), 1)


class NotificationTest(_Base):
    def test_une_notification_et_une_seule_tant_que_le_groupe_dure(self):
        notifier = self.notifier(self.ncfg(default_human="human:proprio"))
        records = self.passe(notifier, [_orphelin(i) for i in (3, 9, 52)])
        self.assertEqual([(r["event"], r["type"]) for r in records],
                         [("raised", "orphan_lot")])
        self.assertIn("3 lot(s) orphelin(s)", records[0]["text"])
        # un lot de plus, un lot de moins : même situation, rien ne repart
        self.assertEqual(self.passe(notifier, [_orphelin(i) for i in (3, 9, 52, 54)]), [])
        self.assertEqual(self.passe(notifier, [_orphelin(i) for i in (9, 54)]), [])
        self.assertEqual(len(_desktop_log(self.desk)), 1)
        # plus aucun lot orphelin : une seule résolution
        self.now += 60
        records = self.passe(notifier, [])
        self.assertEqual([(r["event"], r["type"]) for r in records],
                         [("resolved", "orphan_lot")])


if __name__ == "__main__":
    unittest.main()
