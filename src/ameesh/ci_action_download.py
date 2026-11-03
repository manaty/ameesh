# SPDX-License-Identifier: AGPL-3.0-only
"""#5655: distinguish action bootstrap infrastructure failures from tests."""
import re


def failed_action_download(job, text):
    """Require both a failed initialization and an archive download error.

    Never hide a failure after a workflow step ran, even if a preceding
    download warning recovered. The raw job steps are the execution proof.
    """
    if job.get('conclusion') != 'failure':
        return False
    steps = job.get('steps') or []
    if not steps or steps[0].get('name') != 'Set up job' or steps[0].get('conclusion') != 'failure':
        return False
    if any(s.get('status') == 'in_progress' or s.get('conclusion') not in ('skipped', None)
           for s in steps[1:] if s.get('name') != 'Complete job'):
        return False
    # Negative test evidence always wins, including a malformed job listing.
    if re.search(r'(?m)(?:^|\s)(?:not ok \d+|FAIL: |ERROR: |FAILED \(|"statut"\s*:\s*"(?:echec|erreur)")', text):
        return False
    return bool(re.search(
        r"(?im)##\[error\]Failed to download archive 'https://codeload\.github\.com/"
        r"[\w.-]+/[\w.-]+/tar\.gz/[0-9a-f]{40}' after \d+ attempts\.", text))
