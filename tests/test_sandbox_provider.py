# SPDX-License-Identifier: AGPL-3.0-only
"""Contract tests for the sandbox Job provider (#2202 S4a).

No database, no cluster and no secret: this file exercises the deterministic
identity, the spec validation and the in-memory fake only.
"""
from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from ameesh import sandbox_provider as provider

TURN = "0123456789abcdef0123456789abcdef"
IMAGE = "ghcr.io/thepackengers/ima-reviewer@sha256:" + "a" * 64


def identity(**overrides):
    values = dict(turn_id=TURN, agent="mty-amh-impl-03", epoch=7,
                  placement="tpk-stg-reviewers",
                  profile="tpk-reviewer-2026-10", namespace="reviewers")
    values.update(overrides)
    return provider.JobIdentity(**values)


def spec(**overrides):
    values = dict(identity=identity(), image=IMAGE,
                  active_deadline_seconds=1200,
                  ttl_seconds_after_finished=3600)
    values.update(overrides)
    return provider.JobSpec(**values)


class JobNameTest(unittest.TestCase):
    def test_name_is_the_designed_derivation(self):
        expected = "ameesh-" + hashlib.sha256(TURN.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(provider.job_name(TURN), expected)
        self.assertRegex(provider.job_name(TURN), r"^ameesh-[0-9a-f]{12}$")
        self.assertEqual(len(provider.job_name(TURN)), 19)

    def test_name_is_stable_and_turn_bound(self):
        self.assertEqual(provider.job_name(TURN), provider.job_name(TURN))
        self.assertNotEqual(provider.job_name(TURN),
                            provider.job_name(TURN + "0"))
        self.assertNotEqual(provider.job_name(TURN),
                            provider.job_name(TURN.upper()))

    def test_name_refuses_an_empty_turn(self):
        for value in ("", "   ", None, 42):
            with self.subTest(value=value):
                with self.assertRaises(provider.InvalidJob):
                    provider.job_name(value)


class IdentityTest(unittest.TestCase):
    def test_name_and_labels(self):
        value = identity()
        self.assertEqual(value.name, provider.job_name(TURN))
        self.assertEqual(value.labels, {
            "ameesh.dev/turn-id": TURN,
            "ameesh.dev/agent": "mty-amh-impl-03",
            "ameesh.dev/epoch": "7",
            "ameesh.dev/placement": "tpk-stg-reviewers",
            "ameesh.dev/profile": "tpk-reviewer-2026-10",
        })
        self.assertEqual(set(value.labels), {
            provider.LABEL_TURN_ID, provider.LABEL_AGENT,
            provider.LABEL_EPOCH, provider.LABEL_PLACEMENT,
            provider.LABEL_PROFILE,
        })

    def test_identity_is_deterministic(self):
        self.assertEqual(identity().name, identity().name)
        self.assertEqual(identity().labels, identity().labels)

    def test_namespace_does_not_change_the_turn_identity(self):
        self.assertEqual(identity(namespace="other").name, identity().name)
        self.assertEqual(identity(namespace="other").labels,
                         identity().labels)

    def test_each_label_value_is_validated(self):
        invalid = ("bad/value", "with space", "-leading", "trailing-",
                   "a" * 64)
        for field in ("turn_id", "agent", "placement", "profile"):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(provider.InvalidJob):
                        identity(**{field: value})

    def test_epoch_must_be_a_non_negative_integer(self):
        for value in (-1, 1.5, "7", True, None):
            with self.subTest(value=value):
                with self.assertRaises(provider.InvalidJob):
                    identity(epoch=value)

    def test_namespace_must_be_a_dns_label(self):
        for value in ("", "Reviewers", "-reviewers", "reviewers-",
                      "a_b", "a" * 64):
            with self.subTest(value=value):
                with self.assertRaises(provider.InvalidJob):
                    identity(namespace=value)


class JobSpecTest(unittest.TestCase):
    def test_defaults_and_argv_normalisation(self):
        value = spec(argv=["review", "--json"])
        self.assertEqual(value.argv, ("review", "--json"))
        self.assertEqual(value.active_deadline_seconds, 1200)
        self.assertEqual(value.ttl_seconds_after_finished, 3600)
        self.assertEqual(spec().argv, ())

    def test_identity_is_required(self):
        with self.assertRaises(provider.InvalidJob):
            spec(identity="not-an-identity")

    def test_image_must_be_pinned_by_digest(self):
        for image in ("ghcr.io/x:latest", "ghcr.io/x@sha256:abc",
                      "ghcr.io/x@sha256:" + "A" * 64, "ghcr.io/x", ""):
            with self.subTest(image=image):
                with self.assertRaises(provider.InvalidJob):
                    spec(image=image)

    def test_argv_must_be_a_sequence_of_non_empty_strings(self):
        for argv in ("review", ["ok", ""], ["ok", 3], [None], 42, None):
            with self.subTest(argv=argv):
                with self.assertRaises(provider.InvalidJob):
                    spec(argv=argv)

    def test_limits_are_strictly_positive_integers(self):
        for field in ("active_deadline_seconds", "ttl_seconds_after_finished"):
            for value in (0, -1, True, 1.5, "60", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(provider.InvalidJob):
                        spec(**{field: value})


class JobTest(unittest.TestCase):
    def test_terminal_phases(self):
        self.assertEqual(provider.TERMINAL_PHASES, frozenset({
            provider.PHASE_SUCCEEDED, provider.PHASE_FAILED}))
        base = dict(namespace="reviewers", name="ameesh-0123456789ab",
                    uid="uid-1", labels={}, phase=provider.PHASE_PENDING,
                    image=IMAGE, created_at=1.0)
        self.assertFalse(provider.Job(**base).terminal)
        self.assertFalse(provider.Job(**dict(base, phase=None)).terminal)
        self.assertTrue(provider.Job(
            **dict(base, phase=provider.PHASE_SUCCEEDED)).terminal)
        self.assertTrue(provider.Job(
            **dict(base, phase=provider.PHASE_FAILED)).terminal)

    def test_unknown_phase_is_refused(self):
        base = dict(namespace="reviewers", name="ameesh-0123456789ab",
                    uid="uid-1", labels={}, phase="Complete",
                    image=IMAGE, created_at=1.0)
        with self.assertRaises(provider.InvalidJob):
            provider.Job(**base)


class InterfaceTest(unittest.TestCase):
    def test_provider_is_abstract_and_fake_implements_it(self):
        with self.assertRaises(TypeError):
            provider.JobProvider()
        self.assertIsInstance(provider.FakeJobProvider(), provider.JobProvider)


class FakeProviderTest(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        self.provider = provider.FakeJobProvider(clock=lambda: self.clock[0])
        self.identity = identity()
        self.spec = spec(identity=self.identity)

    def test_create_and_get(self):
        job = self.provider.create(self.spec)
        self.assertIsInstance(job, provider.Job)
        self.assertEqual(job.namespace, "reviewers")
        self.assertEqual(job.name, self.identity.name)
        self.assertEqual(job.labels, self.identity.labels)
        self.assertEqual(job.image, IMAGE)
        self.assertEqual(job.phase, provider.PHASE_PENDING)
        self.assertEqual(job.created_at, 100.0)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.completed_at)
        self.assertFalse(job.terminal)
        self.assertEqual(self.provider.get("reviewers", job.name), job)

    def test_duplicate_create_does_not_replace(self):
        first = self.provider.create(self.spec)
        with self.assertRaises(provider.JobAlreadyExists) as caught:
            self.provider.create(self.spec)
        self.assertEqual(caught.exception.name, self.identity.name)
        self.assertEqual(self.provider.get("reviewers", self.identity.name),
                         first)

    def test_get_missing_is_none(self):
        self.assertIsNone(self.provider.get("reviewers", "nope"))
        self.assertIsNone(self.provider.get("other", self.identity.name))

    def test_list_filters_namespace_and_every_label(self):
        first = self.provider.create(self.spec)
        second = self.provider.create(spec(identity=identity(
            turn_id="b" * 32, agent="other-agent")))
        third = self.provider.create(spec(identity=identity(
            turn_id="c" * 32, profile="other-profile")))
        fourth = self.provider.create(spec(identity=identity(
            turn_id="d" * 32, namespace="elsewhere")))

        self.assertEqual(
            [job.name for job in self.provider.list("reviewers")],
            sorted([first.name, second.name, third.name]))
        found = self.provider.list(
            "reviewers", {provider.LABEL_AGENT: "other-agent"})
        self.assertEqual([job.name for job in found], [second.name])
        found = self.provider.list("reviewers", {
            provider.LABEL_AGENT: "mty-amh-impl-03",
            provider.LABEL_PROFILE: "other-profile",
        })
        self.assertEqual([job.name for job in found], [third.name])
        self.assertEqual(
            self.provider.list("reviewers", {provider.LABEL_AGENT: "nobody"}),
            [])
        self.assertEqual([job.name for job in self.provider.list("elsewhere")],
                         [fourth.name])

    def test_list_is_sorted_by_name(self):
        names = []
        for turn in ("a" * 32, "b" * 32, "c" * 32):
            names.append(self.provider.create(
                spec(identity=identity(turn_id=turn))).name)
        self.assertEqual([job.name for job in self.provider.list("reviewers")],
                         sorted(names))

    def test_logs(self):
        job = self.provider.create(self.spec)
        self.assertEqual(self.provider.logs("reviewers", job.name), "")
        self.assertEqual(
            self.provider.append_log("reviewers", job.name, "line 1\n"),
            "line 1\n")
        self.provider.append_log("reviewers", job.name, '{"result": 1}\n')
        self.assertEqual(self.provider.logs("reviewers", job.name),
                         'line 1\n{"result": 1}\n')
        with self.assertRaises(provider.JobNotFound):
            self.provider.logs("reviewers", "nope")

    def test_delete_is_idempotent_and_returns_whether_a_job_existed(self):
        job = self.provider.create(self.spec)
        self.assertTrue(self.provider.delete("reviewers", job.name))
        self.assertIsNotNone(self.provider.get("reviewers", job.name).deletion_timestamp)
        self.assertTrue(self.provider.delete("reviewers", job.name))
        with self.assertRaises(provider.JobAlreadyExists):
            self.provider.create(self.spec)
        self.provider.finish_delete("reviewers", job.name)
        self.assertIsNone(self.provider.get("reviewers", job.name))
        self.assertFalse(self.provider.delete("reviewers", job.name))
        with self.assertRaises(provider.JobNotFound):
            self.provider.logs("reviewers", job.name)

    def test_recreated_job_gets_a_new_uid(self):
        first = self.provider.create(self.spec)
        self.provider.delete("reviewers", first.name)
        self.provider.finish_delete("reviewers", first.name)
        second = self.provider.create(self.spec)
        self.assertEqual(first.name, second.name)
        self.assertNotEqual(first.uid, second.uid)

    def test_lifecycle_and_timestamps(self):
        job = self.provider.create(self.spec)
        self.clock[0] = 101.0
        running = self.provider.set_phase(
            "reviewers", job.name, provider.PHASE_RUNNING)
        self.assertEqual(running.phase, provider.PHASE_RUNNING)
        self.assertEqual(running.started_at, 101.0)
        self.assertIsNone(running.completed_at)
        self.assertFalse(running.terminal)

        self.clock[0] = 102.0
        done = self.provider.set_phase(
            "reviewers", job.name, provider.PHASE_SUCCEEDED, message="ok")
        self.assertEqual(done.started_at, 101.0)
        self.assertEqual(done.completed_at, 102.0)
        self.assertEqual(done.message, "ok")
        self.assertTrue(done.terminal)

    def test_direct_terminal_phase_sets_both_timestamps(self):
        job = self.provider.create(self.spec)
        self.clock[0] = 101.0
        failed = self.provider.set_phase(
            "reviewers", job.name, provider.PHASE_FAILED, message="boom")
        self.assertEqual(failed.started_at, 101.0)
        self.assertEqual(failed.completed_at, 101.0)
        self.assertEqual(failed.message, "boom")
        self.assertTrue(failed.terminal)

    def test_set_phase_keeps_the_message_when_omitted(self):
        job = self.provider.create(self.spec)
        self.provider.set_phase(
            "reviewers", job.name, provider.PHASE_RUNNING, message="started")
        running = self.provider.set_phase(
            "reviewers", job.name, provider.PHASE_RUNNING)
        self.assertEqual(running.message, "started")

    def test_set_phase_refuses_unknown_phase_and_missing_job(self):
        job = self.provider.create(self.spec)
        with self.assertRaises(provider.InvalidJob):
            self.provider.set_phase("reviewers", job.name, "Complete")
        with self.assertRaises(provider.JobNotFound):
            self.provider.set_phase("reviewers", "nope", provider.PHASE_RUNNING)

    def test_terminal_phase_is_final(self):
        job = self.provider.create(self.spec)
        self.provider.set_phase("reviewers", job.name, provider.PHASE_SUCCEEDED)
        # Updating the message on the same terminal phase is allowed; moving
        # back to a live phase is not, as in Kubernetes.
        updated = self.provider.set_phase(
            "reviewers", job.name, provider.PHASE_SUCCEEDED, message="done")
        self.assertEqual(updated.message, "done")
        with self.assertRaises(provider.InvalidJob):
            self.provider.set_phase(
                "reviewers", job.name, provider.PHASE_RUNNING)

    def test_snapshot_is_isolated_from_the_store(self):
        job = self.provider.create(self.spec)
        labels = job.labels
        labels[provider.LABEL_AGENT] = "tampered"
        again = self.provider.get("reviewers", job.name)
        self.assertEqual(again.labels[provider.LABEL_AGENT],
                         "mty-amh-impl-03")
        again.labels[provider.LABEL_AGENT] = "tampered-again"
        self.assertEqual(
            self.provider.get("reviewers", job.name).labels[
                provider.LABEL_AGENT],
            "mty-amh-impl-03")

    def test_concurrent_duplicate_create_is_serialized(self):
        workers = 8
        barrier = Barrier(workers)

        def attempt(_):
            barrier.wait(timeout=5)
            try:
                self.provider.create(self.spec)
                return True
            except provider.JobAlreadyExists:
                return False

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(attempt, range(workers)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.provider.list("reviewers")), 1)


class ProviderErrorTest(unittest.TestCase):
    def test_stable_codes_and_retryability(self):
        self.assertEqual(provider.JobAlreadyExists("x").code, "already-exists")
        self.assertEqual(provider.JobNotFound("x").code, "not-found")
        self.assertEqual(provider.ProviderForbidden("x").code, "forbidden")
        self.assertEqual(provider.ProviderUnavailable("x").code, "unavailable")
        self.assertFalse(provider.JobAlreadyExists("x").retryable)
        self.assertFalse(provider.JobNotFound("x").retryable)
        self.assertFalse(provider.ProviderForbidden("x").retryable)
        self.assertTrue(provider.ProviderUnavailable("x").retryable)
        self.assertEqual(provider.JobNotFound("x", name="job-1").name, "job-1")
        self.assertEqual(str(provider.JobNotFound("")), "not-found")


if __name__ == "__main__":
    unittest.main()
