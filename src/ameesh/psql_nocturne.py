# SPDX-License-Identifier: AGPL-3.0-only
"""#7483: dispatch daily psql under the elected role, recheck each admission.

Four waves of 60-minute jobs fit from 01:47Z to 06Z. Last admission at
06Z leaves admitted jobs finished before 07Z. A delayed part fails visibly
as incomplete; it never silently replaces a red result or cancels a sibling.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys


WORKFLOW = "psql-nocturne.yml"
START_HOUR = 0
END_HOUR = 6


def admitted(now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    return START_HOUR <= now.hour < END_HOUR


def main(argv=None, *, gh=None, cfg=None, now=None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh psql-nocturne")
    parser.add_argument("action", choices=["dispatch", "guard", "gate"])
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    allowed = admitted(now)
    reason = "within UTC window [00:00, 06:00)" if allowed else "outside UTC window [00:00, 06:00); psql suite deferred"
    if args.action in ("guard", "gate"):
        print(reason)
        if args.action == "gate":
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write("lancer=%s\n" % str(allowed).lower())
            if not allowed:
                print("::notice::Nocturne deferred: " + reason)
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                    stream.write("Nocturne deferred: " + reason + "\n")
            return 0
        if not allowed:
            print("::error::Incomplete psql nocturne: " + reason)
        return 0 if allowed else 1
    if not allowed:
        print("psql-nocturne: " + reason)
        return 0
    from . import carte, config, plan_github

    try:
        settings = plan_github.settings_of(config.load() if cfg is None else cfg)
        gh = gh or plan_github.Gh()
        targets = carte.cibles(settings, depots=args.repo)
        if not targets:
            print("psql-nocturne: no github.projects configured", file=sys.stderr)
            return 1
        failed = False
        for target in targets:
            repo, branch = target["depot"], target["branche"]
            try:
                if not carte.porte(gh, repo, branch, workflow=WORKFLOW):
                    print("psql-nocturne: %s — workflow absent" % repo)
                    continue
                if not args.dry_run:
                    code, out, err = gh.run([
                        "api", "-X", "POST",
                        "repos/%s/actions/workflows/%s/dispatches" % (repo, WORKFLOW),
                        "--input", "-"], json.dumps({"ref": branch}))
                    if code:
                        raise carte.CarteError(carte._erreur(code, out, err))
                print("psql-nocturne: %s@%s — %s" % (repo, branch, "dry-run" if args.dry_run else "dispatched"))
            except carte.CarteError as exc:
                print("psql-nocturne: %s — %s" % (repo, exc), file=sys.stderr)
                failed = True
        return int(failed)
    except (plan_github.SettingsError, plan_github.GithubError, carte.CarteError) as exc:
        print("psql-nocturne: %s" % exc, file=sys.stderr)
        return 1
