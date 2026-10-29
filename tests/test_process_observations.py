# SPDX-License-Identifier: AGPL-3.0-only
"""#4425 : journal d'observations IMA, projection derivee et lecteur fail-closed.

Aucune base et aucun reseau : le journal, la projection, les etats derives et
les refus stables sont prouves sur des fixtures. Le collecteur reel est
qualifie separement (test_process_observations_real), depuis bureau-goal.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from ameesh import process_observations as po

NOW = dt.datetime(2026, 10, 29, 12, 0, 0, tzinfo=dt.timezone.utc)
SHA = "a" * 40
OTHER = "b" * 40


def observation(
        ident: str = "obs-1", *, sha: str = SHA, lot: int | None = 7,
        kind: str = po.KIND_DEPLOYED, environment: str = po.ENVIRONMENT_STAGING,
        app: str = "ameesh", observed_at: dt.datetime = NOW,
        source_digest: str = "d" * 64, actor: str = "agent:qa",
        provenance: dict | None = None) -> po.Observation:
    if provenance is None:
        provenance = {"kind": po.PROVENANCE_STAGING, "ref": "actif-%s.json" % sha[:12],
                      "sha256": source_digest, "attestation": sha[:12],
                      "hosts": ["ameesh-agents-1"]}
    return po.Observation(
        id=ident, kind=kind, environment=environment,
        work_item_id=lot, app=app, artifact=sha,
        source_revision=sha[:12], actor=actor, observed_at=observed_at,
        provenance=provenance, source_digest=source_digest)


def identity(**overrides) -> po.ObservationIdentity:
    values = dict(authority="ima", journal_id="ima",
                  repos=("manaty/ameesh",), issues=())
    values.update(overrides)
    return po.ObservationIdentity(**values)


class Bench(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = self.temporary.name
        self.journal_path = os.path.join(self.directory, "journal.jsonl")
        self.projection_path = os.path.join(self.directory, "projection.json")
        self.source = po.Source(
            journal=self.journal_path, projection=self.projection_path,
            authority="ima", journal_id="ima",
            repos=("manaty/ameesh",), issues=(), staging_app="ameesh")

    def journal(self, *, write: bool = True) -> po.Journal:
        journal = po.Journal(self.journal_path, create=True, write=write)
        journal.reload()
        return journal

    def projection(self, journal: po.Journal, *, now: dt.datetime = NOW,
                   refusals=()) -> po.Projection:
        return po.build_projection(self.source, journal, now=now,
                                   refusals=refusals,
                                   collector={"host": "bureau-goal",
                                              "identity": "bureau",
                                              "reader": "bureau-goal",
                                              "version": "test"})

    def write_and_load(self, projection: po.Projection, *,
                       now: dt.datetime = NOW):
        written = po.write_projection(projection, self.projection_path)
        self.assertEqual(written, "written")
        return po.load_projection(
            {"observations": {
                "authority": "ima", "projection": self.projection_path,
                "max_age_s": 300,
                "source": {"journal_id": "ima",
                           "repos": ["manaty/ameesh"], "issues": []}}},
            now=now)


class JournalTest(Bench):
    def test_append_only_chained_and_idempotent(self):
        journal = self.journal()
        first, created = journal.record(observation("obs-1"))
        self.assertTrue(created)
        again, created = journal.record(observation("obs-1"))
        self.assertFalse(created)
        self.assertEqual(first["digest"], again["digest"])
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64))
        with open(self.journal_path, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["seq"], 1)
        self.assertEqual(lines[0]["prev"], "")
        self.assertEqual(lines[1]["prev"], lines[0]["digest"])
        self.assertEqual(stat.S_IMODE(os.stat(self.journal_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), 0o700)
        reloaded = self.journal()
        self.assertEqual([entry["digest"] for entry in reloaded.entries],
                         [entry["digest"] for entry in lines])

    def test_same_id_with_another_source_digest_is_edited(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        with self.assertRaises(po.JournalError) as raised:
            journal.record(observation("obs-1", source_digest="e" * 64))
        self.assertEqual(raised.exception.code, "observation-edited")

    def test_supersede_and_revoke_are_derived(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64,
                                   observed_at=NOW + dt.timedelta(minutes=5)))
        journal.supersede("obs-1", "obs-2", reason="next-deployment")
        journal.revoke("obs-2", reason="deploiement invalide", actor="bureau")
        derived, revocations = journal.derive()
        states = {item.id: item for item in derived}
        self.assertEqual(states["obs-1"].state, po.STATE_SUPERSEDED)
        self.assertEqual(states["obs-1"].superseded_by, "obs-2")
        self.assertEqual(states["obs-2"].state, po.STATE_REVOKED)
        self.assertEqual(states["obs-2"].revoked_by, "rev-4")
        self.assertEqual(len(revocations), 1)
        self.assertEqual(revocations[0]["target"], "obs-2")
        # idempotente, jamais une seconde ecriture
        before = len(journal.entries)
        journal.revoke("obs-2", reason="deploiement invalide")
        journal.supersede("obs-1", "obs-2", reason="next-deployment")
        self.assertEqual(len(journal.entries), before)

    def test_supersede_a_revoked_observation_is_refused(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64))
        journal.revoke("obs-1", reason="source supprimee")
        with self.assertRaises(po.JournalError) as raised:
            journal.supersede("obs-1", "obs-2")
        self.assertEqual(raised.exception.code, "observation-revoked")

    def test_supersede_by_an_unknown_or_unrelated_observation_is_refused(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        with self.assertRaises(po.JournalError) as raised:
            journal.supersede("obs-1", "obs-unknown")
        self.assertEqual(raised.exception.code, "observation-invalid")
        journal.record(observation("obs-2", lot=8, sha=OTHER,
                                   source_digest="e" * 64,
                                   observed_at=NOW + dt.timedelta(minutes=5)))
        with self.assertRaises(po.JournalError) as raised:
            journal.supersede("obs-1", "obs-2")
        self.assertEqual(raised.exception.code, "observation-invalid")
        # La preuve est toujours courante : un `by` forge ne l'a pas retiree.
        self.assertEqual(journal.derive()[0][0].state, po.STATE_CURRENT)
        journal.record(observation("obs-3", sha=OTHER,
                                   source_digest="f" * 64,
                                   observed_at=NOW + dt.timedelta(minutes=5)))
        journal.supersede("obs-1", "obs-3")
        self.assertEqual(journal.derive()[0][0].state, po.STATE_SUPERSEDED)

    def test_the_journal_refuses_qa_before_the_contract(self):
        """L'API d'ecriture ne contourne pas le refus QA (#796)."""
        journal = self.journal()
        with self.assertRaises(po.JournalError) as raised:
            journal.record(observation("qa-invented", kind=po.KIND_PASS,
                                       provenance={
                                           "kind": po.PROVENANCE_STAGING,
                                           "ref": "qa-a.json",
                                           "sha256": "d" * 64,
                                           "attestation": "qa-a",
                                           "hosts": ["ameesh-agents-1"]}))
        self.assertEqual(raised.exception.code, "observation-invalid")
        self.assertEqual(journal.entries, [])

    def test_an_entry_that_would_cross_the_byte_bound_is_refused(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        size = os.path.getsize(self.journal_path)
        with mock.patch.object(po, "MAX_JOURNAL_BYTES", size + 1):
            with self.assertRaises(po.JournalError) as raised:
                journal.record(observation("obs-2", sha=OTHER,
                                           source_digest="e" * 64))
            self.assertEqual(raised.exception.code, "journal-bounds")
            self.assertEqual(os.path.getsize(self.journal_path), size)
            reloaded = self.journal()
        self.assertEqual(len(reloaded.entries), 1)

    def test_a_unicode_line_separator_does_not_break_the_journal(self):
        """B2 : U+2028/U+0085 dans une raison ne coupent pas une entree."""
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.revoke("obs-1", reason="source\u2028corrompue\u0085ici")
        reloaded = self.journal()
        self.assertEqual(len(reloaded.entries), 2)
        self.assertEqual(reloaded.derive()[0][0].state, po.STATE_REVOKED)

    def test_tampered_journal_is_refused(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        with open(self.journal_path, encoding="utf-8") as handle:
            payload = json.loads(handle.readline())
        payload["observation"]["artifact"] = OTHER
        with open(self.journal_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        with self.assertRaises(po.JournalError) as raised:
            self.journal()
        self.assertEqual(raised.exception.code, "journal-invalid")

    def test_journal_permissions_are_enforced(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        os.chmod(self.journal_path, 0o644)
        with self.assertRaises(po.JournalError) as raised:
            self.journal()
        self.assertEqual(raised.exception.code, "journal-permissions")
        os.chmod(self.journal_path, 0o600)
        os.chmod(self.directory, 0o755)
        with self.assertRaises(po.JournalError) as raised:
            self.journal()
        self.assertEqual(raised.exception.code, "journal-permissions")


class ProjectionTest(Bench):
    def test_roundtrip_digest_and_counts(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64,
                                   observed_at=NOW + dt.timedelta(minutes=5)))
        journal.supersede("obs-1", "obs-2", reason="next-deployment")
        projection = self.projection(journal)
        loaded = self.write_and_load(projection)
        self.assertEqual(loaded.authority, "ima")
        self.assertEqual(loaded.digest, projection.digest)
        self.assertEqual(loaded.source["journal_id"], "ima")
        self.assertEqual(loaded.source["entries"], 3)
        self.assertEqual(loaded.collector["host"], "bureau-goal")
        self.assertEqual(len(loaded.observations), 2)
        summary = po.summary(loaded, now=NOW)
        self.assertEqual(summary["counts"]["current"], 1)
        self.assertEqual(summary["counts"]["superseded"], 1)
        self.assertEqual(summary["counts"]["expired"], 0)

    def test_missing_stale_future_and_invalid_projection(self):
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW)
        self.assertEqual(raised.exception.code, "projection-missing")
        journal = self.journal()
        journal.record(observation("obs-1"))
        projection = self.projection(journal)
        po.write_projection(projection, self.projection_path)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW + dt.timedelta(hours=1))
        self.assertEqual(raised.exception.code, "projection-stale")
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW - dt.timedelta(minutes=5))
        self.assertEqual(raised.exception.code, "projection-future")

    def test_projection_digest_and_identity_are_fail_closed(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        projection = self.projection(journal)
        po.write_projection(projection, self.projection_path)
        with open(self.projection_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["observations"][0]["artifact"] = OTHER
        with open(self.projection_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW)
        self.assertEqual(raised.exception.code, "projection-digest")
        os.unlink(self.projection_path)
        po.write_projection(projection, self.projection_path)
        for name, identity_value in (
                ("authority", identity(authority="other")),
                ("journal", identity(journal_id="other")),
                ("repos", identity(repos=("manaty/other",))),
                ("issues", identity(issues=("manaty/ameesh#1",)))):
            with self.subTest(name=name):
                with self.assertRaises(po.ProjectionError) as raised:
                    po.load_projection(
                        path=self.projection_path, identity=identity_value,
                        now=NOW)
                self.assertEqual(raised.exception.code, "projection-source")

    def test_projection_permissions_are_enforced(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        projection = self.projection(journal)
        po.write_projection(projection, self.projection_path)
        os.chmod(self.projection_path, 0o664)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW)
        self.assertEqual(raised.exception.code, "projection-permissions")
        os.chmod(self.projection_path, 0o644)
        os.chmod(self.directory, 0o775)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW)
        self.assertEqual(raised.exception.code, "projection-permissions")

    def test_same_second_publication_never_goes_backwards(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        old = self.projection(journal)
        self.assertEqual(po.write_projection(old, self.projection_path),
                         "written")
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64))
        new = self.projection(journal)
        self.assertEqual(po.write_projection(new, self.projection_path),
                         "written")
        # La projection en retard (une entree de journal en moins) ne peut pas
        # ecraser la plus fraiche dans la meme seconde.
        self.assertEqual(po.write_projection(old, self.projection_path),
                         "not-newer")
        self.assertEqual(po.load_projection(
            path=self.projection_path, identity=identity(), now=NOW
        ).source["entries"], 2)

    def test_projection_keeps_current_and_a_bounded_history(self):
        journal = self.journal()
        for index in range(4):
            journal.record(observation(
                "obs-%d" % index,
                observed_at=NOW + dt.timedelta(minutes=index),
                source_digest=str(index + 1) * 64))
        for index in range(3):
            journal.supersede("obs-%d" % index, "obs-3")
        with mock.patch.object(po, "MAX_HISTORY", 2):
            projection = self.projection(journal)
        ids = [item.id for item in projection.observations]
        self.assertIn("obs-3", ids)
        self.assertEqual(len(ids), 3)           # courant + 2 d'histoire
        self.assertNotIn("obs-0", ids)          # le plus ancien est hors fenetre
        self.assertEqual(po.read_sources(
            projection, work_item_id=7, app="ameesh",
            now=NOW + dt.timedelta(minutes=5))[0]["id"], "obs-3")

    def test_history_window_closes_supersede_references(self):
        journal = self.journal()
        for index, ident in enumerate(("z", "a", "c")):
            journal.record(observation(
                ident, observed_at=NOW, source_digest=str(index + 1) * 64))
        journal.supersede("z", "a")
        journal.supersede("a", "c")
        for index in (1, 2):
            ident = "rev-%d" % index
            journal.record(observation(
                ident, observed_at=NOW + dt.timedelta(seconds=index),
                source_digest=str(index + 4) * 64))
            journal.revoke(ident, reason="source revoquee")
        with mock.patch.object(po, "MAX_HISTORY", 3):
            projection = self.projection(journal)
        ids = [item.id for item in projection.observations]
        self.assertIn("c", ids)
        self.assertNotIn("z", ids)      # son successeur a est hors fenetre
        self.assertIn("rev-1", ids)
        self.assertIn("rev-2", ids)
        self.assertEqual(projection.by_id("z"), None)
        loaded = self.write_and_load(projection)
        self.assertEqual(len(loaded.observations), 3)

    def test_history_window_closes_references_of_revoked_records(self):
        """Un revoque garde son superseded_by : la reference doit rester close."""
        journal = self.journal()
        journal.record(observation("z", observed_at=NOW,
                                   source_digest="1" * 64))
        journal.record(observation("a", observed_at=NOW,
                                   source_digest="2" * 64))
        journal.supersede("z", "a")
        journal.revoke("z", reason="source revoquee")
        journal.revoke("a", reason="source revoquee")
        journal.record(observation("c", observed_at=NOW,
                                   source_digest="3" * 64))
        with mock.patch.object(po, "MAX_HISTORY", 1):
            projection = self.projection(journal)
        ids = [item.id for item in projection.observations]
        self.assertNotIn("z", ids)      # successeur a hors fenetre
        self.assertIn("c", ids)
        loaded = self.write_and_load(projection)
        self.assertIn("c", [item.id for item in loaded.observations])

    def test_history_window_total_agrees_with_the_reader(self):
        journal = self.journal()
        for index in range(3):
            journal.record(observation(
                "current-%d" % index,
                observed_at=NOW + dt.timedelta(minutes=index),
                source_digest=str(index + 1) * 64))
        for index in range(2):
            ident = "old-%d" % index
            journal.record(observation(
                ident, observed_at=NOW, source_digest=str(index + 5) * 64))
            journal.supersede(ident, "current-2")
        with mock.patch.object(po, "MAX_OBSERVATIONS", 4), \
                mock.patch.object(po, "MAX_HISTORY", 10):
            projection = self.projection(journal)
            self.assertLessEqual(len(projection.observations), 4)
            loaded = self.write_and_load(projection)
        self.assertLessEqual(len(loaded.observations), 4)

    def test_a_projection_with_fewer_entries_never_overwrites(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64))
        complete = self.projection(journal, now=NOW)
        self.assertEqual(po.write_projection(complete, self.projection_path),
                         "written")
        # Un journal different, plus court, produit une projection plus
        # tardive : elle ne doit pas ecraser les deux entrees deja en place.
        other_path = os.path.join(self.directory, "other.jsonl")
        other = po.Journal(other_path, create=True)
        other.reload()
        other.record(observation("obs-1"))
        source = po.Source(
            journal=other_path, projection=self.projection_path,
            authority="ima", journal_id="ima",
            repos=("manaty/ameesh",), issues=(), staging_app="ameesh")
        late = po.build_projection(
            source, other, now=NOW + dt.timedelta(minutes=1),
            collector={"host": "bureau-goal"})
        self.assertEqual(po.write_projection(late, self.projection_path),
                         "not-newer")

    def test_consumer_max_age_is_stricter(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        projection = self.projection(journal, now=NOW)
        po.write_projection(projection, self.projection_path)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "max_age_s": 1,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW + dt.timedelta(seconds=120))
        self.assertEqual(raised.exception.code, "projection-stale")

    def test_bad_revocation_reference_is_refused(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.revoke("obs-1", reason="source supprimee")
        projection = self.projection(journal)
        payload = projection.as_dict()
        payload["revocations"][0]["target"] = "obs-unknown"
        payload.pop("digest")
        payload["digest"] = po._sha256_json(payload)
        with open(self.projection_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(self.projection_path, 0o644)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW)
        self.assertEqual(raised.exception.code, "projection-invalid")

    def test_bad_supersede_reference_is_refused(self):
        journal = self.journal()
        journal.record(observation("obs-1"))
        journal.record(observation("obs-2", sha=OTHER,
                                   source_digest="e" * 64,
                                   observed_at=NOW + dt.timedelta(minutes=5)))
        journal.supersede("obs-1", "obs-2")
        payload = self.projection(journal).as_dict()
        record = payload["observations"][0]
        record["superseded_by"] = "obs-unknown"
        body = {key: value for key, value in record.items() if key != "digest"}
        record["digest"] = po._sha256_json(body)
        payload.pop("digest")
        payload["digest"] = po._sha256_json(payload)
        with open(self.projection_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(self.projection_path, 0o644)
        with self.assertRaises(po.ProjectionError) as raised:
            po.load_projection(
                {"observations": {"authority": "ima",
                                  "projection": self.projection_path,
                                  "source": {"journal_id": "ima",
                                             "repos": [], "issues": []}}},
                now=NOW)
        self.assertEqual(raised.exception.code, "projection-invalid")


class ReaderTest(Bench):
    def _projection(self) -> po.Projection:
        journal = self.journal()
        journal.record(observation("deployed-7"))
        journal.record(observation("deployed-8", lot=8,
                                   source_digest="e" * 64))
        journal.record(observation(
            "acceptance", kind=po.KIND_PASS, lot=None, app="",
            environment=po.ENVIRONMENT_ACCEPTANCE,
            provenance={"kind": po.PROVENANCE_GITHUB,
                        "repo": "manaty/ameesh", "comment_id": 1,
                        "url": "https://github.com/manaty/ameesh/issues/1#issuecomment-1",
                        "body_sha256": "a" * 64,
                        "path": "knowledge/contract.md"}))
        journal.record(observation("revoked", source_digest="1" * 64))
        journal.revoke("revoked", reason="source supprimee")
        journal.record(observation("superseded", source_digest="2" * 64))
        journal.supersede("superseded", "deployed-7")
        journal.record(observation(
            "future", lot=7, observed_at=NOW + dt.timedelta(hours=1),
            source_digest="3" * 64))
        projection = self.projection(journal)
        # Le journal refuse un pass/fail QA avant le contrat #796 ; le lecteur
        # est deja pret pour lui (age QA de 7 jours). On ajoute les deux
        # enregistrements directement a la vue, sans passer par l'API
        # d'ecriture.
        qa_old = po.Observation(
            id="qa-old", kind=po.KIND_PASS, environment=po.ENVIRONMENT_STAGING,
            work_item_id=7, app="ameesh", artifact=SHA,
            source_revision="qa-7", actor="agent:qa",
            observed_at=NOW - dt.timedelta(days=8),
            provenance={"kind": po.PROVENANCE_STAGING, "ref": "qa-7.json",
                        "sha256": "f" * 64, "attestation": "qa-7",
                        "hosts": ["ameesh-agents-1"]},
            source_digest="f" * 64)
        qa_current = replace(
            qa_old, id="qa-current", kind=po.KIND_FAIL, observed_at=NOW,
            source_digest="4" * 64)
        return replace(projection,
                       observations=projection.observations
                       + (qa_old, qa_current))

    def test_read_sources_lists_only_current_lot_and_app(self):
        projection = self._projection()
        records = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        ids = [record["id"] for record in records]
        self.assertIn("deployed-7", ids)
        self.assertIn("qa-current", ids)
        self.assertIn("acceptance", ids)       # acceptance globale
        self.assertNotIn("deployed-8", ids)    # autre lot
        self.assertNotIn("qa-old", ids)        # QA > 7 jours
        self.assertNotIn("revoked", ids)
        self.assertNotIn("superseded", ids)
        self.assertNotIn("future", ids)
        other = po.read_sources(projection, work_item_id=7, app="nexlink",
                                now=NOW)
        self.assertNotIn("deployed-7",
                         [record["id"] for record in other])

    def test_deployed_never_expires_by_age(self):
        journal = self.journal()
        journal.record(observation(
            "old-deployment", observed_at=NOW - dt.timedelta(days=365)))
        projection = self.projection(journal)
        records = po.read_sources(projection, work_item_id=7, app="ameesh",
                                  now=NOW)
        self.assertEqual([record["id"] for record in records],
                         ["old-deployment"])

    def test_require_observation_refusals(self):
        projection = self._projection()
        for ident, code in (
                ("unknown", "observation-missing"),
                ("revoked", "observation-revoked"),
                ("superseded", "observation-superseded"),
                ("qa-old", "observation-expired"),
                ("future", "observation-future"),
                ("deployed-8", "observation-source")):
            with self.subTest(ident=ident):
                with self.assertRaises(po.ObservationRefusal) as raised:
                    po.require_observation(projection, ident, work_item_id=7,
                                           app="ameesh", now=NOW)
                self.assertEqual(raised.exception.code, code)
        with self.assertRaises(po.ObservationRefusal) as raised:
            po.require_observation(projection, "deployed-7", work_item_id=7,
                                   app="ameesh", environment="preprod",
                                   now=NOW)
        self.assertEqual(raised.exception.code, "observation-source")
        with self.assertRaises(po.ObservationRefusal) as raised:
            po.require_observation(projection, "deployed-7", work_item_id=7,
                                   app="ameesh", artifact=OTHER, now=NOW)
        self.assertEqual(raised.exception.code, "observation-stale")
        record = po.require_observation(projection, "deployed-7",
                                        work_item_id=7, app="ameesh",
                                        environment="staging",
                                        kind="deployed", artifact=SHA, now=NOW)
        self.assertEqual(record.id, "deployed-7")


class ConfigurationTest(unittest.TestCase):
    def _section(self, **overrides):
        section = {
            "authority": "ima",
            "journal": "/var/lib/ameesh/observations/ima.jsonl",
            "projection": "/var/lib/ameesh/observations/ima.json",
            "source": {
                "journal_id": "ima",
                "repos": ["manaty/ameesh"],
                "issues": ["thepackengers/issue-manager-agent#704"],
                "staging": {"state_dir": "/home/agent/.local/state/ameesh",
                            "app": "ameesh",
                            "hosts": ["ameesh-agents-1", "ameesh-agents-2",
                                      "ameesh-agents-3", "pc-smichea"],
                            "reportable_hosts": ["pc-smichea"]},
                "acceptances": True,
                "ttl_s": 300,
            },
        }
        section.update(overrides)
        return section

    def test_source_from_config(self):
        source = po.source_from_config(self._section())
        self.assertEqual(source.authority, "ima")
        self.assertEqual(source.repos, ("manaty/ameesh",))
        self.assertEqual(source.issues,
                         ("thepackengers/issue-manager-agent#704",))
        self.assertTrue(source.acceptances)
        self.assertEqual(source.ttl_s, 300)

    def test_identity_requires_repos_and_issues_even_empty(self):
        section = self._section()
        del section["source"]["repos"]
        with self.assertRaises(po.ProjectionError) as raised:
            po.identity_from_config(section)
        self.assertEqual(raised.exception.code, "projection-source")

    def test_qa_contract_is_not_invented(self):
        with self.assertRaises(po.ReaderError) as raised:
            po.source_from_config(self._section(qa={"enabled": True}))
        self.assertEqual(raised.exception.code, "reader-config")
        # `qa: false` reste une absence explicite, admise.
        po.source_from_config(self._section(qa=False))

    def test_secret_values_are_refused(self):
        with self.assertRaises(po.ReaderError) as raised:
            po.source_from_config(self._section(token="ghp_secret"))
        self.assertEqual(raised.exception.code, "reader-config")

    def test_at_least_one_source_is_required(self):
        section = self._section()
        section["source"].pop("staging")
        section["source"]["acceptances"] = False
        with self.assertRaises(po.ReaderError) as raised:
            po.source_from_config(section)
        self.assertEqual(raised.exception.code, "reader-config")


if __name__ == "__main__":
    unittest.main()
