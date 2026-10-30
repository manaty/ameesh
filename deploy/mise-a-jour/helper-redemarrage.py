#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow host restart primitive; enrollment/CLI transport is separate.

Never execute untrusted shell text or a caller-supplied unit, path or program.
The independently enrolled helper supplies authority, database fence and policy.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time

_spec = importlib.util.spec_from_file_location('restart_protocol', Path(__file__).with_name('controleur-release.py'))
protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(protocol)


class HelperError(ValueError):
    pass


def policy_id(policy):
    return 'policy-' + protocol.identity(policy)


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {'host','units','controller_unit','restart_seconds','manager'}:
        raise HelperError('invalid restart policy')
    if not isinstance(policy['host'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', policy['host']):
        raise HelperError('invalid enrolled host')
    validate_manager(policy['manager'])
    units = policy['units']
    if (not isinstance(units, list) or not 1 <= len(units) <= 64 or len(set(units)) != len(units)
            or any(not isinstance(u,str) or not re.fullmatch(r'ameesh-runner(?:-agent)?@[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.service',u) for u in units)):
        raise HelperError('invalid enrolled runner units')
    if (policy['controller_unit'] != 'ameesh-release-controller.service'
            or policy['controller_unit'] in units or type(policy['restart_seconds']) is not int
            or not 5 <= policy['restart_seconds'] <= 300):
        raise HelperError('invalid independent restart controller')


def validate_manager(manager):
    """One explicitly enrolled manager; never inferred from a runner name."""
    if not isinstance(manager, dict):
        raise HelperError('invalid enrolled manager')
    if manager == {'kind': 'system'}:
        return
    if (set(manager) == {'kind', 'account'} and manager['kind'] == 'user'
            and isinstance(manager['account'], str)
            and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', manager['account'])):
        return
    raise HelperError('invalid enrolled manager')


def runner_systemctl(manager):
    validate_manager(manager)
    args = ['/usr/bin/systemctl']
    if manager['kind'] == 'user':
        args += ['--machine=' + manager['account'] + '@.host', '--user']
    return args


class Systemd:
    """Root-owned fixed executables, scrubbed environment and bounded output.

    No program/argument/configuration is supplied by the SSH request. Transient
    action services remain loaded after success/failure until a durable helper
    receipt exists. Do not collect them or use an age-based cleanup policy.
    """
    def __init__(self, *, cgroups='/sys/fs/cgroup', boot_file='/proc/sys/kernel/random/boot_id'):
        self.cgroups = Path(cgroups)
        self.boot_file = Path(boot_file)

    @staticmethod
    def run(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                                    check=True, env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
            if len(result.stdout) > 65536 or len(result.stderr) > 65536:
                raise HelperError('systemd output limit exceeded')
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise HelperError('systemd transport unavailable') from None

    def boot(self):
        value = self.boot_file.read_text().strip()
        if not re.fullmatch(r'[0-9a-f-]{36}',value):
            raise HelperError('boot identity unavailable')
        return value

    def quiet(self, units, *, manager):
        # A runner daemon may remain, but neither a harness child nor an orphan
        # in its cgroup subtree may survive a restart boundary.
        for unit in units:
            text = self.run(runner_systemctl(manager) + ['show',unit,'--property=LoadState,MainPID,ControlGroup'])
            fields = dict(line.split('=',1) for line in text.splitlines() if '=' in line)
            if fields.get('LoadState') != 'loaded' or not fields.get('MainPID','').isdigit():
                raise HelperError('runner cgroup identity unavailable')
            group = fields.get('ControlGroup','')
            if not group:
                if fields['MainPID'] == '0':
                    continue
                raise HelperError('runner cgroup identity unavailable')
            path = Path(group)
            if not path.is_absolute() or '..' in path.parts or group == '/':
                raise HelperError('unsafe runner cgroup')
            root = self.cgroups / group.lstrip('/')
            if root.is_symlink() or any(p.is_symlink() for p in root.parents):
                raise HelperError('linked runner cgroup')
            if not root.is_dir():
                raise HelperError('runner cgroup unavailable')
            for directory, links, files in os.walk(root, followlinks=False):
                if any((Path(directory)/name).is_symlink() for name in links):
                    raise HelperError('linked runner cgroup')
                if 'cgroup.procs' not in files:
                    raise HelperError('runner process inventory unavailable')
                processes = (Path(directory)/'cgroup.procs').read_text().split()
                if any(not pid.isdigit() for pid in processes):
                    raise HelperError('invalid runner process inventory')
                if any(pid != fields['MainPID'] for pid in processes):
                    return False
        return True

    def state(self, job):
        text = self.run(['/usr/bin/systemctl','show',job,
                         '--property=LoadState,ActiveState,Result,ExecMainStatus,InvocationID'])
        fields = dict(line.split('=',1) for line in text.splitlines() if '=' in line)
        if fields.get('LoadState') == 'not-found':
            return 'absent'
        if fields.get('LoadState') != 'loaded':
            raise HelperError('restart action unit unavailable')
        if fields.get('ActiveState') == 'activating':
            return 'pending'
        if fields.get('ActiveState') in ('active','failed','inactive'):
            if not re.fullmatch(r'[0-9a-f]{32}',fields.get('InvocationID','')):
                raise HelperError('restart invocation receipt unavailable')
            if fields.get('Result') == 'success' and fields.get('ExecMainStatus') == '0' and fields['ActiveState'] == 'active':
                return 'success'
            if fields.get('Result') and fields['Result'] != 'success':
                return 'failure'
        raise HelperError('restart action outcome uncertain')

    def submit(self, job, units, seconds, *, manager):
        self.run(['/usr/bin/systemd-run','--quiet','--no-block','--unit='+job,
                  '--property=Type=oneshot','--property=RemainAfterExit=yes',
                  '--property=TimeoutStartSec='+str(seconds)+'s',
                  '--property=Slice=system.slice','--',*runner_systemctl(manager),'restart','--',*units])


class RestartHelper:
    def __init__(self, root, policy, *, authority, fence, system):
        validate_policy(policy)
        self.root = Path(root)
        self.policy = json.loads(json.dumps(policy))
        self.authority, self.fence, self.system = authority, fence, system

    def _request(self, step, request):
        if (not isinstance(request,dict) or set(request) != {'operation','manifest_digest','action_id','manifest','step'}
                or step not in ('restart','recovery_restart') or request.get('step') != step):
            raise HelperError('invalid helper request')
        operation = request['operation']
        if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',operation):
            raise HelperError('invalid helper operation')
        protocol.validate(request['manifest'])
        manifest = request['manifest']
        if (request['action_id'] != operation+':'+step or request['manifest_digest'] != protocol.identity(manifest)
                or manifest['host'] != self.policy['host'] or manifest['policy'] != policy_id(self.policy)
                or any(unit not in self.policy['units'] for unit in manifest['units'])):
            raise HelperError('helper scope mismatch')

    def _authority(self, request):
        if self.authority.validate(request['operation'],request['manifest_digest'],request['step']) is not True:
            raise HelperError('helper authority denied')

    def _boundary(self, request, *, guarded=False):
        self._authority(request)
        if ((not guarded and self.fence.fenced(request) is not True)
                or self.system.quiet(request['manifest']['units'], manager=self.policy['manager']) is not True):
            raise HelperError('live drained host boundary required')
        # A slow database/cgroup observation cannot extend execution authority.
        self._authority(request)

    def perform(self, step, request):
        request = json.loads(json.dumps(request))
        self._request(step,request)
        try:
            with protocol.locked(self.root,self.policy['host']) as root:
                self._authority(request)
                action_hash = protocol.identity(request)
                path = root/(action_hash+'.json')
                if path.exists() or path.is_symlink():
                    record = protocol.load(path)
                    if (set(record) != {'request','job','boot','receipt'} or record.get('request') != request
                            or record.get('job') != 'ameesh-release-action-'+action_hash+'.service'):
                        raise HelperError('helper action receipt mismatch')
                    receipt = record.get('receipt')
                    if receipt is not None:
                        if (not isinstance(receipt,dict) or type(receipt.get('ok')) is not bool
                                or receipt != dict(request,ok=receipt.get('ok'))):
                            raise HelperError('helper completion receipt mismatch')
                        return receipt
                    if record.get('boot') != self.system.boot():
                        raise HelperError('restart intent crossed reboot; reconciliation required')
                else:
                    with self.fence.guard(request):
                        self._boundary(request,guarded=True)
                        job = 'ameesh-release-action-'+action_hash+'.service'
                        if self.system.state(job) != 'absent':
                            raise HelperError('unowned restart action unit exists')
                        record = {'request':request,'job':job,'boot':self.system.boot(),'receipt':None}
                        protocol.save(path,record)
                        self._boundary(request,guarded=True)
                        self.system.submit(job,request['manifest']['units'],self.policy['restart_seconds'],
                                           manager=self.policy['manager'])
                # Never resubmit an existing intent. An absent service can mean
                # loss before launch OR removal after completion; neither proves
                # it is safe to restart twice. Root reconciliation is required.
                deadline = time.monotonic()+self.policy['restart_seconds']
                while True:
                    self._authority(request)
                    state = self.system.state(record['job'])
                    if state in ('success','failure'):
                        record['receipt'] = dict(request,ok=state=='success')
                        protocol.save(path,record)
                        return record['receipt']
                    if state == 'absent' or time.monotonic() >= deadline:
                        raise HelperError('restart outcome uncertain; reconciliation required')
                    time.sleep(min(0.2,max(0,deadline-time.monotonic())))
        except HelperError:
            raise
        except Exception:
            raise HelperError('helper outcome unavailable; reconciliation required') from None
