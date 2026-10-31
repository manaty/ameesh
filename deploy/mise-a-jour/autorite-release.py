#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only revalidation of a scoped, owner-signed decide answer (L232b).

This never signs, registers keys, answers a decision or executes a release.
An agent's mail, unsigned answer or policy name cannot supply authority.

The signature must cover the whole declared window (`expires_at`): a release
fence is never committed under an authority that would die before the window
it approved, recovery included. The one-hour `decide <id> <answer> --key` path
therefore fits windows up to one hour; a longer window is signed deliberately
with `ameesh approve --expires …` plus `decide <id> <answer> --signed`. After
the window, resuming the same manifest needs a fresh signed decision.
"""
import hashlib
import json
import time

from ameesh import authority, decisions, signing, storage

PREFIX = 'Approve the exact host release and bounded rollback below:\n'
OPTIONS = [{'key': 'apply', 'label': 'Apply this exact release and its bounded rollback'},
           {'key': 'leave', 'label': 'Leave this host unchanged'}]
STEPS = ('prepare', 'backup', 'pin', 'fence', 'migrate', 'configure', 'activate',
         'restart', 'health', 'accept', 'unfence', 'recovery_fence', 'rollback',
         'recovery_restart', 'recovery_health', 'recovery_unfence')


def digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def question(operation, manifest, *, expires_at):
    """Prepared question for decide ask --needs-signature; no request is sent."""
    if type(expires_at) is not int or expires_at <= manifest['rollback_until']:
        raise ValueError('authority expiry must cover the rollback window')
    scope = {'v': 1, 'operation': operation, 'host': manifest['host'],
             'manifest_digest': digest(manifest), 'manifest': manifest,
             'steps': list(STEPS), 'expires_at': expires_at}
    return PREFIX + json.dumps(scope, sort_keys=True, separators=(',', ':'))


class SignedDecisionAuthority:
    """Bind one decision to one prepared operation; re-read it for each step.

    db is a separately enrolled controller connection, never runner credentials
    silently promoted to controller authority. Existing decide proof verification
    checks the currently enrolled owner key and its revocation/fingerprint.
    Only the signed question carries execution expiry; mutable due_ts is ignored.
    """
    def __init__(self, db, decision_id, operation, manifest, *, clock=time.time):
        if type(decision_id) is not int or decision_id <= 0:
            raise ValueError('decision id required')
        self.db = db
        self.decision_id = decision_id
        self.operation = operation
        # Detach the approved payload from subsequent caller mutations.
        self.manifest = json.loads(json.dumps(manifest))
        self.digest = digest(self.manifest)
        self.clock = clock

    def validate(self, operation, manifest_digest, step):
        self.denial_reason = None
        if operation != self.operation or manifest_digest != self.digest or step not in STEPS:
            return False
        view = decisions.get(self.db, self.decision_id)
        if view and not self._current(view):
            return False
        if not view or view.get('state') != 'answered' or view.get('needs_signature') is not True:
            return False
        answer = view.get('answer') or {}
        # #4788: the signed text names the option through the bounded native
        # forms of decisions.match_option (raw key or the two signer-owner.sh
        # mentions). The full text stays recorded and digested; a free prefix
        # or an extra explanation never becomes the option.
        if (view.get('options') != OPTIONS or answer.get('option') != 'apply'
                or answer.get('channel') != 'signed'
                or decisions.match_option(view, answer.get('text') or '') != 'apply'):
            return False
        text = view.get('question') or ''
        if not text.startswith(PREFIX):
            return False
        try:
            scope = json.loads(text[len(PREFIX):])
            expires = scope['expires_at']
            if type(expires) is not int or text != question(operation, self.manifest, expires_at=expires):
                return False
        except (KeyError, TypeError, ValueError):
            return False
        now = self.clock()
        if now >= expires:
            return False
        return self._proof_live(view, now, expires)

    def _current(self, view):
        self.denial_reason = None
        try:
            decisions.require_current(self.db, view)
        except decisions.DecisionError as error:
            self.denial_reason = str(error)
            return False
        return True

    def _proof_live(self, view, now, expires):
        """Shared proof check after a subclass verifies its exact signed scope.

        The signed approval must cover the whole declared window: a fence is
        never committed under an authority that will die before its own
        approved rollback/recovery window ends. An expired approval is then
        refused at the first step (never after the fence), and a later resume
        of the same manifest needs a fresh signed decision, not a manual
        unfence.
        """
        answer = view.get('answer') or {}
        if not decisions.verify_proof(self.db, view)[0]:
            return False
        proof = answer['proof']
        rows = storage.of(self.db).approvals.recent(
            action=decisions.APPROVAL_ACTION, artifact_hash=proof['digest'], limit=20)
        row = next((item for item in rows if item['id'] == proof['approval_id']), None)
        if not row:
            return False
        try:
            parts = row['signed_payload'].split('|', 2)
            if parts[:2] != [authority.DOMAIN, 'approval']:
                return False
            signed = json.loads(parts[2])
            for key in ('approver', 'action', 'artifact_kind', 'artifact_hash', 'decision', 'nonce'):
                if signed.get(key) != row.get(key):
                    return False
            if (signed['artifact_kind'] != decisions.APPROVAL_KIND
                    or signed['decision'] != 'approved'
                    or type(signed['expires']) is not int or type(signed['ts']) is not int
                    or signed['expires'] <= now * 1_000_000
                    or signed['expires'] < expires * 1_000_000
                    or signed['ts'] > (now + 120) * 1_000_000
                    or signed['expires'] <= signed['ts']
                    or int(round(float(row['expires_ts']) * 1_000_000)) != signed['expires']
                    or int(round(float(row['created_ts']) * 1_000_000)) != signed['ts']):
                return False
            # Verify THIS captured row as well: a row replaced between the
            # generic proof check and this read must not extend signed expiry.
            key = authority.key_info(self.db, row['approver']) or {}
            if (key.get('key_role') != 'owner' or key.get('key_revoked_ts')
                    or key.get('public_key_fingerprint') != row.get('signature_key')
                    or row.get('consumed_by') != 'decision:%d' % self.decision_id
                    or not signing.verify(authority.unb64(key['public_key']),
                                          row['signed_payload'].encode('utf-8'),
                                          authority.unb64(row['signature']))):
                return False
            nonce = storage.of(self.db).nonces.state(row['approver'], row['nonce'])
            return bool(nonce and nonce.get('consumed_by') == 'decision:%d' % self.decision_id)
        except (KeyError, TypeError, ValueError, OverflowError, authority.AuthorityError):
            return False
