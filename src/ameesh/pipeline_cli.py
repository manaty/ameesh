# SPDX-License-Identifier: AGPL-3.0-only
"""Commands for declarative Pipeline modules, resolved from pinned git objects."""
from __future__ import annotations

import argparse
from functools import lru_cache
import json
from pathlib import PurePosixPath
import re
import sys
import uuid

from . import canon, process_card, process_registry as registry, work
from .process_workflow import WorkflowRefusal

#: Evidence keys the guards read as integers (the closure's replacement lot,
#: `_close_facts`). Everything else stays the string the caller typed.
EVIDENCE_INT_KEYS = frozenset({"superseded_by"})


def _evidence_value(key, value):
    """Type a known numeric fact; every other reference remains a string.

    The CLI is an adapter over the guards' evidence contracts: a lot number is
    an integer (``superseded_by``), and `_close_facts` also accepts its decimal
    spelling for older callers. The receipt and the replay intent carry the
    typed value."""
    if key in EVIDENCE_INT_KEYS and re.fullmatch(r'[0-9]+', value):
        return int(value)
    return value


@lru_cache(maxsize=128)
def _compiled(repo, ref, path, commit, version):
    try:
        content = canon.git_read(repo, commit, path)
        if content is None:
            raise ValueError('pinned card is unreadable')
        text = content.decode('utf-8')
        block = process_card.pipeline_of_page(text)
        spec, encoding = process_card.compile_block(block, ref=ref, version=version)
        data = canon.load_yaml(canon.split_frontmatter(text))
        name = canon._text(data.get('name')) or PurePosixPath(path).stem
        return name, spec, encoding
    except (OSError, canon.GitError, UnicodeError, ValueError) as exc:
        raise work.WorkError('pipeline-unreadable: %s' % ref) from exc


def pipeline_at(ref, *, cfg, version=0):
    """Compile the exact card object; configured sources only, never local files.

    A source may have moved since adoption. Its repository remains the lookup
    location for the old immutable commit; no branch or working tree is read.
    """
    parsed = canon.split_ref(ref)
    if not parsed:
        raise work.WorkError('pipeline-ref: expected a pinned card reference')
    prefix, member, path, commit = parsed
    if (not canon._SHA_RE.fullmatch(commit)
            or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts):
        raise work.WorkError('pipeline-ref: immutable commit and relative path required')
    for loaded in canon.load_configured(cfg):
        if (prefix is None and not loaded.is_default) or (prefix and prefix != loaded.id):
            continue
        for source in loaded.sources:
            if source.member == member and source.mode == 'git':
                return _compiled(source.repo, ref, path, commit, version)
    raise work.WorkError('pipeline-source: configured git source missing for %s' % ref)


def main(db, argv, *, actor, module, cfg=None):
    """Serve one canon module through the native registry and its verifiers."""
    parser = argparse.ArgumentParser(
        prog='ameesh module %s' % module.NAME,
        description=("Pipeline commands are signed by a registered agent, the lot's "
                     "assignee or a pilot; a human pilot acts through its agent."),
        epilog=("Evidence: --ref KEY=VALUE, repeatable. The guard decides the keys; "
                "stop_work takes reason=abandoned|superseded (close_reason is "
                "accepted) and superseded_by=LOT for a supersession."))
    commands = parser.add_subparsers(dest='action', required=True)
    adopt = commands.add_parser('adopt', help='pin an unpinned work item')
    adopt.add_argument('lot', type=int)
    for action in ('advance', 'hold', 'release'):
        command = commands.add_parser(action)
        command.add_argument('lot', type=int)
        command.add_argument('--expected-version', type=int)
        command.add_argument('--command-id', default=None)
        command.add_argument('--ref', action='append', default=[],
                             metavar='KEY=VALUE',
                             help='guard evidence fact, repeatable (strings, except '
                                  'superseded_by)')
        if action == 'advance':
            command.add_argument('--to', required=True)
            command.add_argument('--guard', required=True)
        elif action == 'hold':
            command.add_argument('--hold', choices=registry.HOLDS, required=True)
    args = parser.parse_args(argv)
    if db is None:
        return 0
    cfg = cfg or db.cfg
    try:
        if str(actor or '').startswith(work.HUMAN_PREFIX):
            # The Process registry signs commands of registered agents only:
            # a human actor is refused before it reads as a dead authority.
            raise work.WorkError(
                'pipeline-authority: a Pipeline command is signed by a registered '
                'agent (the lot assignee or a pilot); a human pilot acts through '
                'its agent')
        if args.action == 'adopt':
            # The discovered card is a merged, trusted canon object. Resolve it
            # again from git rather than trusting its in-memory compilation.
            if module.pipeline.fiche.untrusted:
                raise work.WorkError('pipeline-untrusted: adoption needs a git card')
            name, spec, _encoding = pipeline_at(module.CARD_REF, cfg=cfg)
            if name != module.NAME:
                raise work.WorkError('pipeline-module: card name mismatch')
            result = work.adopt_workflow(db, args.lot, module=module.NAME,
                                         spec=spec, actor=actor)
        else:
            state = registry.registry_state(db, args.lot)
            previous = (registry.receipt_of(db, args.lot, args.command_id)
                        if args.command_id else None)
            accepted = ((previous or {}).get('evidence') or {}).get('command') or {}
            ref = accepted.get('workflow_ref', state.workflow_ref)
            revision = accepted.get('workflow_version', state.workflow_version)
            name, spec, _encoding = pipeline_at(ref, cfg=cfg, version=revision)
            if name != module.NAME:
                raise work.WorkError('pipeline-module: work item belongs to another module')
            evidence = {}
            for reference in args.ref:
                key, sep, value = reference.partition('=')
                if not sep or not key or key in evidence:
                    raise work.WorkError('pipeline-reference: unique KEY=VALUE required')
                evidence[key] = _evidence_value(key, value)
            if getattr(args, 'guard', '') == 'stop_work' and 'close_reason' in evidence:
                # The native closure key is `close_reason` (work.close); the
                # stop_work verifier names the same fact `reason` (#5000).
                # Accept both spellings, never two contradictory reasons.
                reason = evidence.pop('close_reason')
                if 'reason' in evidence and evidence['reason'] != reason:
                    raise work.WorkError(
                        'pipeline-reference: close_reason and reason disagree')
                evidence['reason'] = reason
            from . import process_verifiers
            process_verifiers.install_verifiers()
            command = registry.Command(
                work_item_id=args.lot, command_id=args.command_id or str(uuid.uuid4()),
                expected_version=(accepted.get('expected_version', state.version)
                                  if args.expected_version is None else args.expected_version),
                authority=registry.Authority(actor, source='explicit', epoch=0),
                workflow_ref=ref, workflow_version=revision,
                action=args.action, from_stage=accepted.get('from', state.workflow_stage) or '',
                to_stage=getattr(args, 'to', ''), guard=getattr(args, 'guard', ''),
                hold=getattr(args, 'hold', ''), evidence=evidence)
            result = registry.apply_command(db, command, spec=spec).as_dict()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (work.WorkError, registry.RegistryRefusal, WorkflowRefusal) as exc:
        print(str(exc), file=sys.stderr)
        return 1
