#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H8: one root-owned, socket-activated handoff request, no command language.

systemd supplies a connected Unix socket on stdin (Accept=yes). Peer identity
comes from the kernel, never the JSON. Root enrollment and the H7 executor own
all paths, units, accounts, fencing, intents and receipts. Every connection
reloads policy, so removing an admission revokes subsequent calls immediately.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys

from ameesh import handoff_contract

POLICY = Path('/etc/ameesh-handoff/local.json')
LIMIT = 65536
SECONDS = 55


class Refused(ValueError):
    pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('duplicate')
        result[key] = value
    return result


def protected(path, *, private=False):
    """Check the entire root-owned chain before reading or importing code."""
    path = Path(path)
    if not path.is_absolute():
        raise Refused('path')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise Refused('parent')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or info.st_mode & (0o077 if private else 0o022)):
        raise Refused('file')
    return path


def policy(path=POLICY):
    data = protected(path, private=True).read_bytes()
    if len(data) > LIMIT:
        raise Refused('policy')
    value = json.loads(data, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {'uid', 'host', 'agents'}
            or type(value['uid']) is not int or value['uid'] <= 0
            or not isinstance(value['host'], str)
            or not isinstance(value['agents'], list) or not value['agents']
            or any(not isinstance(a, str) or not a for a in value['agents'])
            or len(set(value['agents'])) != len(value['agents'])):
        raise Refused('policy')
    return value


def consistent_policy(rules, enrollment):
    if (rules['host'] != enrollment['host']
            or not set(rules['agents']).issubset(enrollment['accounts'])):
        raise Refused('execution_policy')


def admitted(request, rules, uid):
    if uid != rules['uid']:
        raise Refused('caller')
    if request.get('step') == 'inventory':
        request = handoff_contract.validate_inventory_request(request)
    else:
        request = handoff_contract.validate_effect_request(request)
        if request['agent'] not in rules['agents']:
            raise Refused('agent')
    if request['host'] != rules['host']:
        raise Refused('host')
    return request


def executor():
    path = protected(Path(__file__).absolute().with_name('hote-handoff.py'))
    spec = importlib.util.spec_from_file_location('local_handoff_effects', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(channel, rules, execute):
    """Half-close frames one bounded request; no second request is accepted."""
    channel.settimeout(SECONDS)
    _, uid, _ = struct.unpack('3i', channel.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
    if uid != rules['uid']:
        raise Refused('caller')
    data = bytearray()
    while True:
        chunk = channel.recv(min(8192, LIMIT + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > LIMIT:
            raise Refused('request')
    request = json.loads(data, object_pairs_hook=unique)
    if not isinstance(request, dict):
        raise Refused('request')
    request = admitted(request, rules, uid)
    receipt = execute(request)
    if request['step'] == 'inventory':
        handoff_contract.validate_inventory_receipt(receipt, request)
    else:
        handoff_contract.validate_effect_receipt(receipt, request)
    output = json.dumps(receipt, sort_keys=True).encode()
    if len(output) > LIMIT:
        raise Refused('receipt')
    channel.sendall(output)


def main(argv=None):
    def expired(signum, frame):
        raise module.Uncertain('deadline')
    channel = None
    module = None
    pid = uid = -1
    try:
        if (sys.argv[1:] if argv is None else argv) or os.geteuid() != 0:
            raise Refused('authority')
        module = executor()
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(SECONDS)
        rules = policy()
        consistent_policy(rules, module.load_execution())
        channel = socket.socket(fileno=os.dup(0))
        with open(os.devnull, 'rb') as empty:
            os.dup2(empty.fileno(), 0)
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
            raise Refused('channel')
        pid, uid, _ = struct.unpack('3i', channel.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        serve(channel, rules, module.execute)
        return 0
    except Exception as exc:
        # May follow an effect. No forged negative receipt: client reconciles.
        category = ('uncertain' if module is not None
                    and isinstance(exc, module.Uncertain) else
                    'refused' if isinstance(exc, (Refused, handoff_contract.ContractError)) else 'unavailable')
        print(f'local_handoff_{category} pid={pid} uid={uid}', file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        if channel is not None:
            channel.close()


if __name__ == '__main__':
    sys.exit(main())
