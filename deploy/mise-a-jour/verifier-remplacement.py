#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Independent check of a runtime replacement question before signing (#5166).

    verifier-remplacement.py --clone REPOSITORY --output DIRECTORY --question FILE

Rebuilds outside root at the SHA the question displays, in a new directory and
never from the stage, then compares the runtime, sources and code digests to
the signed values. Publishes nothing; exit 2 on any difference.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PREFIX = 'Approve this exact sealed runtime replacement:\n'
FIELDS = ('runtime', 'lib', 'code_sha256')


def module(name):
    spec = importlib.util.spec_from_file_location('check_' + name.replace('-', '_'), HERE / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def signed(question):
    if not question.startswith(PREFIX):
        raise ValueError('not a runtime replacement question')
    value = json.loads(question[len(PREFIX):])
    return dict(sha=value['next']['sha'], runtime=value['next']['runtime'], lib=value['next']['lib'],
                code_sha256=value['code_sha256'])


def measure(runtime_tree):
    # System Python, without ameesh: only the stdlib-only companions load here.
    tree = module('runtime-release')
    sources = Path(runtime_tree) / 'share/ameesh/mise-a-jour'
    return dict(runtime=tree.inventory(runtime_tree)['digest'], lib=tree.inventory(sources)['digest'],
                code_sha256=tree.code_digest(sources))


def differences(question, measured):
    expected = signed(question)
    return [field for field in FIELDS if expected[field] != measured[field]]


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ('--clone', '--output', '--question'):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    question = Path(args.question).read_text()
    subprocess.run([sys.executable, '-I', '-B', str(HERE / 'construction-runtime.py'), '--clone', args.clone,
                    '--sha', signed(question)['sha'], '--output', args.output], check=True)
    measured = measure(Path(args.output) / 'runtime')
    different = differences(question, measured)
    print(json.dumps(dict(signed(question), different=different), sort_keys=True))
    return 2 if different else 0


if __name__ == '__main__':
    sys.exit(main())
