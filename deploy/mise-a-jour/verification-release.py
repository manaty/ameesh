#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Effective configuration verification before a release receipt (L232b).

`configuration-release.py` owns bytes and recovery journals. This module
reconciles the systemd view after those bytes are published: enrolled unit
syntax, daemon reload, effective fragment/drop-ins and a protected host card's
declared capabilities, state and guard. It has no CLI, no enrollment and no
job-supplied command: the pinned host factory supplies the card and injects the
system adapter. Nothing here decides authority or touches a database.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess


def module(name):
    spec=importlib.util.spec_from_file_location('verification_'+name.replace('-','_'),
        Path(__file__).with_name(name+'.py'))
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


runtime=module('runtime-release')
fences=module('fence-release')

UNIT=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service\Z')
UNIT_SUFFIX=('.service','.socket','.timer','.target','.slice','.mount','.path',
             '.automount','.swap','.device')
BASE=('FragmentPath','DropInPaths','LoadState','NeedDaemonReload','ActiveState')
OUTPUT=65536
# Declared properties are compared byte for byte with the effective value.
DECLARED={
    'ActiveState':re.compile(r'(active|inactive|failed|activating|deactivating|reloading)\Z'),
    'AmbientCapabilities':re.compile(r'[A-Za-z0-9_~ ]{0,4096}\Z'),
    'CapabilityBoundingSet':re.compile(r'[A-Za-z0-9_~ ]{0,4096}\Z'),
    'CPUQuota':re.compile(r'[0-9A-Za-z.%: ]{0,64}\Z'),
    'Group':re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z|[0-9]{1,10}\Z'),
    'MemoryMax':re.compile(r'(infinity|[0-9]+(\.[0-9]+)?[KMGTPE]?)\Z'),
    'NoNewPrivileges':re.compile(r'(yes|no)\Z'),
    'PrivateTmp':re.compile(r'(yes|no|true|false)\Z'),
    'ProtectHome':re.compile(r'(yes|no|read-only|tmpfs)\Z'),
    'ProtectSystem':re.compile(r'(yes|no|full|strict)\Z'),
    'Restart':re.compile(r'[A-Za-z][A-Za-z0-9-]{0,31}\Z'),
    'RestrictSUIDSGID':re.compile(r'(yes|no|true|false)\Z'),
    'Slice':re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.slice\Z'),
    'SystemCallFilter':re.compile(r'[\x20-\x7e]{1,4096}\Z'),
    'Type':re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,31}\Z'),
    'User':re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z|[0-9]{1,10}\Z'),
}


class VerificationError(ValueError):
    pass


def validate_card(card):
    """Bounded protected host card; every key is administrator-enrolled."""
    if not isinstance(card,dict) or set(card)!={'units','properties','guard','seconds'}:
        raise VerificationError('invalid host card')
    units=card['units']
    if (not isinstance(units,list) or not 1<=len(units)<=64 or len(set(units))!=len(units)
            or any(not isinstance(unit,str) or UNIT.fullmatch(unit) is None for unit in units)):
        raise VerificationError('invalid enrolled unit list')
    guard=card['guard']
    if guard is not None and (not isinstance(guard,str) or UNIT.fullmatch(guard) is None):
        raise VerificationError('invalid enrolled guard unit')
    properties=card['properties']
    if not isinstance(properties,dict) or len(properties)>65:
        raise VerificationError('invalid declared properties')
    declared={}
    for unit,values in properties.items():
        if unit not in units and unit!=guard:
            raise VerificationError('property outside enrolled units')
        if not isinstance(values,dict) or not 1<=len(values)<=16:
            raise VerificationError('bounded declared properties required')
        declared[unit]={}
        for name,value in values.items():
            pattern=DECLARED.get(name)
            if (pattern is None or not isinstance(value,str) or not value
                    or pattern.fullmatch(value) is None):
                raise VerificationError('declared property refused')
            declared[unit][name]=value
    seconds=card['seconds']
    if type(seconds) is not int or not 5<=seconds<=120:
        raise VerificationError('bounded verification deadline required')
    return dict(units=list(units),guard=guard,properties=declared,seconds=seconds)


class Systemd:
    """Root-owned fixed executables, scrubbed environment, bounded output.

    The pinned factory validates these paths at admission; no request, job or
    enrollment document can supply one. Output never enters a receipt or error.
    """
    def __init__(self, *, systemctl='/usr/bin/systemctl', analyze='/usr/bin/systemd-analyze'):
        if (not Path(systemctl).is_absolute() or not Path(analyze).is_absolute()
                or '..' in Path(systemctl).parts or '..' in Path(analyze).parts):
            raise VerificationError('absolute system executables required')
        self.systemctl,self.analyze=str(Path(systemctl)),str(Path(analyze))

    @staticmethod
    def run(args, seconds):
        try:
            result=subprocess.run(args,capture_output=True,text=True,timeout=seconds,check=False,
                env={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
        except (OSError,subprocess.SubprocessError):
            raise VerificationError('configuration system command unavailable') from None
        if len(result.stdout)>OUTPUT or len(result.stderr)>OUTPUT:
            raise VerificationError('configuration system output limit exceeded')
        return result.returncode,result.stdout

    def verify(self,files,seconds):
        if not files:
            return 0
        return self.run([self.analyze,'verify',*[str(path) for path in files]],seconds)[0]

    def reload(self,seconds):
        return self.run([self.systemctl,'daemon-reload'],seconds)[0]

    def show(self,unit,properties,seconds):
        args=[self.systemctl,'show',unit]
        for name in BASE:
            args.extend(['-p',name])
        for name in sorted(set(properties)-set(BASE)):
            args.extend(['-p',name])
        code,text=self.run(args,seconds)
        if code!=0:
            raise VerificationError('effective configuration unavailable')
        values={}
        for line in text.splitlines():
            if '=' not in line:
                continue
            name,value=line.split('=',1)
            if name in values:
                raise VerificationError('ambiguous effective configuration')
            values[name]=value
        if any(name not in values for name in BASE):
            raise VerificationError('incomplete effective configuration')
        return values


class HostConfiguration:
    """Verify the effective view of files published by ConfigurationFiles.

    The card lists the units whose effective state must hold after any
    configuration change; every release manifest unit must belong to it.
    """
    def __init__(self,files,card, *, system):
        if not callable(getattr(system,'verify',None)) or not callable(getattr(system,'reload',None)) \
                or not callable(getattr(system,'show',None)):
            raise VerificationError('enrolled system adapter required')
        if not callable(getattr(files,'checked',None)) or not callable(getattr(files,'observed',None)) \
                or not callable(getattr(files,'approved',None)) or not hasattr(files,'destinations'):
            raise VerificationError('configuration files adapter required')
        self.files,self.card,self.system=files,validate_card(card),system

    def _safe(self,logical):
        if (not isinstance(logical,str) or not logical.startswith('/') or '\x00' in logical
                or '..' in Path(logical).parts):
            raise VerificationError('unsafe effective path')
        path=self.files.root/logical.lstrip('/')
        try:
            runtime.trusted(path)
            info=path.lstat()
        except Exception:
            raise VerificationError('effective path unavailable') from None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_mode & 0o022
                or info.st_uid not in (0,os.geteuid())):
            raise VerificationError('unsafe effective file')
        return logical

    def _show(self,unit):
        return self.system.show(unit,dict(self.card['properties'].get(unit,{})),self.card['seconds'])

    def _expected_fragment(self,unit):
        """The enrolled destination that must be the effective fragment, if any."""
        if unit.endswith('.service') and '@' in unit:
            candidate='/etc/systemd/system/'+unit.split('@',1)[0]+'@.service'
        else:
            candidate='/etc/systemd/system/'+unit
        return candidate if candidate in self.files.destinations.values() else None

    def _effective(self,unit,info):
        declared=self.card['properties'].get(unit,{})
        if not isinstance(info,dict) or set(info)!=set(BASE)|set(declared):
            raise VerificationError('effective configuration mismatch')
        if info.get('LoadState')!='loaded':
            raise VerificationError('enrolled unit not loaded')
        if info.get('NeedDaemonReload')!='no':
            raise VerificationError('daemon reload not reconciled')
        if DECLARED['ActiveState'].fullmatch(str(info.get('ActiveState',''))) is None:
            raise VerificationError('unexpected effective unit state')
        self._safe(info.get('FragmentPath',''))
        expected=self._expected_fragment(unit)
        if expected is not None and info.get('FragmentPath')!=expected:
            raise VerificationError('enrolled unit is not the effective fragment')
        directories={'/etc/systemd/system/'+unit+'.d','/usr/lib/systemd/system/'+unit+'.d',
                     '/run/systemd/system/'+unit+'.d'}
        for drop in str(info.get('DropInPaths','')).split():
            path=self._safe(drop)
            if str(Path(path).parent) not in directories:
                raise VerificationError('unexpected effective drop-in')
        for name,value in declared.items():
            if info.get(name)!=value:
                raise VerificationError('declared effective property mismatch')
        return True

    def _published(self,step,wanted):
        """Return (syntax files, json documents) after exact byte reconciliation."""
        units,documents=[],[]
        for name,logical in sorted(self.files.destinations.items()):
            if logical.startswith('/etc/systemd/system/') and logical.endswith(UNIT_SUFFIX):
                kind='unit'
            elif logical.endswith('.json'):
                kind='json'
            else:
                kind='other'
            observed=self.files.observed(self.files.path(name))
            if observed is None:
                if step=='configure':
                    raise VerificationError('published configuration file missing')
                continue
            if step=='configure' and observed!=wanted[name]:
                raise VerificationError('published configuration file changed')
            if kind=='unit':
                units.append(str(self.files.path(name)))
            elif kind=='json':
                try:
                    parsed=json.loads(base64.b64decode(observed['content'],validate=True))
                except (ValueError,TypeError):
                    raise VerificationError('enrolled JSON configuration invalid') from None
                if not isinstance(parsed,dict):
                    raise VerificationError('enrolled JSON configuration must be an object')
                documents.append(name)
        return units,documents

    def perform(self,request,release):
        request=json.loads(json.dumps(request)); release=json.loads(json.dumps(release))
        fences.validate(request)
        step=request['step']
        if step not in ('configure','rollback'):
            raise VerificationError('configuration verification step required')
        wanted=self.files.checked(request,release)
        if any(unit not in self.card['units'] for unit in request['manifest']['units']):
            raise VerificationError('manifest unit outside enrolled host card')
        self.files.approved(request)
        units,documents=self._published(step,wanted)
        if units and self.system.verify(units,self.card['seconds'])!=0:
            raise VerificationError('unit syntax verification failed')
        if self.system.reload(self.card['seconds'])!=0:
            raise VerificationError('daemon reload failed')
        # Authority may expire during the slow reload; recheck before effects'
        # effective verification is reported as complete.
        self.files.approved(request)
        for unit in self.card['units']:
            self._effective(unit,self._show(unit))
        guard=self.card['guard']
        if guard is not None:
            info=self._show(guard)
            if info.get('ActiveState')!='active':
                raise VerificationError('guard service is not active')
            self._effective(guard,info)
        return dict(units=len(self.card['units']),guard=guard or '',reloaded=True,
                    syntax_checked=len(units),documents_checked=len(documents))
