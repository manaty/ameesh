"""#2698 : le train lit le canon compilé quel que soit le schéma que
`mechanics.load` accepte.

Incident du 19/10 12:08 (bureau, ameesh-agents-2) : depuis home#120 le canon
main est en `ameesh-mechanics/2` (bloc prevalidation) ; `train_policy.Compiled`
n'acceptait que /1 et le plan d'ombre échouait (« exact compiled canon
mechanics required ») pour tout canon_commit ≥ 946011a3.
"""
import types
import unittest

from ameesh import mechanics, train_policy
from ameesh.train import TrainError

COMMIT = "946011a30d29ecb3fdefb94177c9b64559ab10c3"


def rules(schema, commit=COMMIT):
    return types.SimpleNamespace(schema=schema, commit=commit, project="ameesh",
                                 review_policy={})


class CompiledSchemaTest(unittest.TestCase):
    def test_both_mechanics_schemas_are_read(self):
        for schema in (mechanics.SCHEMA, mechanics.SCHEMA_PREVALIDATION):
            with self.subTest(schema=schema):
                compiled = train_policy.Compiled(rules(schema))
                self.assertEqual(compiled.commit, COMMIT)
                self.assertEqual(compiled.project, "ameesh")

    def test_unknown_schema_or_inexact_commit_refused(self):
        for bad in (rules("ameesh-mechanics/3"), rules("ameesh-train-reviews-policy/1"),
                    rules(mechanics.SCHEMA_PREVALIDATION, commit="946011a3"),
                    rules(mechanics.SCHEMA_PREVALIDATION, commit="main")):
            with self.subTest(schema=bad.schema, commit=bad.commit):
                with self.assertRaisesRegex(TrainError, "exact compiled canon mechanics"):
                    train_policy.Compiled(bad)

    def test_train_follows_every_schema_of_mechanics(self):
        """Un nouveau schéma de mechanics sans le train referait l'incident."""
        declared = {value for name, value in vars(mechanics).items()
                    if name.startswith("SCHEMA") and isinstance(value, str)
                    and value.startswith("ameesh-mechanics/")}
        self.assertEqual(set(train_policy.Compiled.SCHEMAS), declared)


if __name__ == "__main__":
    unittest.main()
