"""#6002 v4 — secret du rôle vm_probe, généré SUR AG3 (root), jamais affiché ni transféré.
Écrit le passfile libpq (vm-operator 0600, O_EXCL) et le vérificateur SCRAM-SHA-256
(root 0600) ; seul le vérificateur quitte AG3 pour ALTER ROLE … PASSWORD '<vérificateur>'.
Usage : python3 -I secret-vm-probe.py <passfile> <fichier vérificateur> <uid> <gid>"""
import base64, hashlib, hmac, os, secrets, sys

passfile, verifier, uid, gid = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
secret = secrets.token_urlsafe(32)          # [A-Za-z0-9_-] : ni « : » ni « \ » pour libpq, SASLprep neutre
salt, it = secrets.token_bytes(16), 4096
salted = hashlib.pbkdf2_hmac('sha256', secret.encode(), salt, it)
client = hmac.new(salted, b'Client Key', 'sha256').digest()
server = hmac.new(salted, b'Server Key', 'sha256').digest()
b64 = lambda b: base64.b64encode(b).decode()
scram = 'SCRAM-SHA-256$%d:%s$%s:%s' % (it, b64(salt), b64(hashlib.sha256(client).digest()), b64(server))

def write(path, data, owner):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, *owner); os.write(fd, data.encode()); os.fsync(fd)
    finally:
        os.close(fd)

write(passfile, '10.77.0.1:5432:ameesh:vm_probe:%s\n' % secret, (uid, gid))
write(verifier, scram + '\n', (0, 0) if os.geteuid() == 0 else (os.geteuid(), os.getegid()))
print('passfile et vérificateur écrits (aucune valeur affichée)')
