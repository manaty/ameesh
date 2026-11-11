"""Read-only guard for every elected executor service invocation."""
import hashlib
import os
from pathlib import Path
import stat
import sys

HOME = Path('/home/ameesh-staging')
ACTOR = 'ameesh:staging'
PUBLIC_ENV = b'VM=10.77.0.1\nPERSONAS_VM=\nHOTES_STAGING="ameesh-agents-1=root@78.232.56.22 ameesh-agents-2=root@78.232.60.1 ameesh-agents-3=root@51.15.137.245 manaty-mesh-1=root@10.77.0.1"\nAMEESH_OPERATEUR=ameesh:staging\n'
ENV_SHA = hashlib.sha256(PUBLIC_ENV).hexdigest()


def validate(home, uid, actor):
    if actor != ACTOR:
        raise ValueError('Wrong actor')
    metadata = home.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError('Wrong dedicated home')
    for parent in [home / '.config', home / '.config/ameesh']:
        metadata = parent.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise ValueError('Unsafe configuration parent')
    path = home / '.config/ameesh/bascule.env'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or metadata.st_size != len(PUBLIC_ENV)):
            raise ValueError('Wrong public environment metadata')
        data = stream.read(len(PUBLIC_ENV) + 1)
    # Pin the complete public file: duplicates, shell indirection, secrets,
    # different targets and altered actor assignments are all rejected.
    if hashlib.sha256(data).hexdigest() != ENV_SHA:
        raise ValueError('Changed public environment')


def main():
    try:
        if (os.getuid() != 980 or os.geteuid() != 980 or not sys.flags.isolated
                or len(sys.argv) != 1
                or os.uname().nodename not in ('ameesh-agents-1', 'ameesh-agents-2')):
            raise ValueError('Wrong protected executor context')
        validate(HOME, 980, os.environ.get('AMEESH_OPERATEUR'))
    except BaseException:
        sys.stderr.write('Elected staging startup refused: actor or protected configuration\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
