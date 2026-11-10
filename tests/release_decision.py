# SPDX-License-Identifier: AGPL-3.0-only
"""Install the controller read gate in disposable fixture schemas only."""
import hashlib
import os
from pathlib import Path
import subprocess
import uuid
from tests.support import TEST_DSN


def decision_owner(role):
    """The isolated NOLOGIN definer owner derived by _release-decision.sql (#6377)."""
    return 'ameesh_decision_owner_' + hashlib.md5(role.encode()).hexdigest()[:12]


def install_decision_gate(cls):
    role = 't_decision_' + uuid.uuid4().hex[:10]
    writer = decision_owner(role)
    root = Path(__file__).resolve().parents[1]
    sql = ("BEGIN; CREATE ROLE \"%s\" NOLOGIN; "
           "SELECT set_config('ameesh.release_role','%s',true);\n" % (role, role)
           + 'GRANT USAGE ON SCHEMA "%s" TO "%s";\n' % (cls.schema,role)
           + '\\i ' + str(root / 'deploy/sql/_release-decision.sql') + '\n\\i ' + str(root / 'deploy/sql/_revoke-release-mailbox.sql') + '\nCOMMIT;\n')
    result = subprocess.run(['psql', '-X', '-w', TEST_DSN, '-q', '-v', 'ON_ERROR_STOP=1'],
                            input=sql, env=dict(os.environ, PGOPTIONS='-c search_path='+cls.schema),
                            capture_output=True, text=True, timeout=60)
    def cleanup():
        sql = ''.join('DROP OWNED BY "%s" CASCADE; DROP ROLE "%s";' % (r,r)
                      for r in (role,writer))
        subprocess.run(['psql','-X','-w',TEST_DSN,'-q','-c',sql],check=True,
                       capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise AssertionError(result.stderr)
    cls.addClassCleanup(cleanup)
    cls.controller_role = role
