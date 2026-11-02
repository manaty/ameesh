# SPDX-License-Identifier: AGPL-3.0-only
"""#5499 : la fusion hors API ne dépend pas de l'identité git ambiante.

Le guest de prévalidation n'a pas de `user.email` global. `git merge
--no-ff --no-commit` lit pourtant le committer (le commit de fusion est écrit
juste après) : sur un tel guest, la fusion échouait avant d'atteindre le
commit explicite, alors qu'un poste avec une identité configurée passait.
Le banc retire les `GIT_AUTHOR_*`/`GIT_COMMITTER_*` et la configuration
globale : la construction de la fusion doit aboutir comme sur un poste.
"""
from __future__ import annotations

import os
from unittest import mock

from .test_1003_merge_pr import _Base

GIT_IDENTITY = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


class GuestWithoutGitIdentityTest(_Base):
    """Même banc que #1003, sans l'identité git que le guest n'a pas."""

    def test_dry_run_builds_the_merge_commit_without_ambient_identity(self):
        env = {key: value for key, value in os.environ.items()
               if key not in GIT_IDENTITY}
        home = os.path.join(self.tmp, "empty-home")
        os.makedirs(home, exist_ok=True)
        env.update(HOME=home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        with mock.patch.dict(os.environ, env, clear=True):
            out = self.merge(self.gh(), dry_run=True)
        self.assertEqual(out["state"], "dry-run", out)
        self.assertEqual(self.tip(), self.base, "rien n'a été écrit")
        self.assertEqual(out["tree"], out["frozen_tree"])
