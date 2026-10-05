# SPDX-License-Identifier: AGPL-3.0-only
"""TLS local facultatif d'ameesh-approve (lot L27).

Mode « passthrough » : la passerelle route par SNI sans rien déchiffrer vers
un tunnel sortant de l'appareil, qui livre le flux TLS chiffré au service sur
la boucle locale. Le certificat de l'hôte `H` vit donc SUR l'appareil ; il est
obtenu et renouvelé par un autre (ACME DNS-01 piloté hors d'ameesh) et déposé
dans deux fichiers que ce module se contente de lire :

* fichiers RÉGULIERS (pas de lien), appartenant à l'utilisateur du service,
  sans aucun droit pour le groupe ni les autres (`0600` ou `0400`) : la clé
  privée de `H` ne doit être lisible par aucun agent ;
* lecture SANS course (verdict codex2, L27 B1) : chaque fichier est ouvert
  une seule fois (`O_NOFOLLOW`), contrôlé SUR LE DESCRIPTEUR (fichier
  régulier, propriétaire, permissions, taille), lu ; ce sont exactement ces
  octets qui sont chargés — recopiés dans un dossier temporaire privé créé
  par le service (`0700`, fichiers `0600`, effacés aussitôt), car
  `ssl.SSLContext.load_cert_chain` ne prend que des chemins. Le chemin
  déposé n'est jamais rouvert : un remplacement (lien, autre inode, autres
  permissions) entre contrôle et chargement ne peut pas substituer une
  autre paire ;
* rechargement sans redémarrage : à CHAQUE connexion, l'identité des fichiers
  (périphérique, inode, date, taille) est comparée à celle du certificat
  chargé ; un dépôt par renommage atomique est pris à la connexion suivante.
  `SIGHUP` force une relecture. Un fichier neuf refusé (permissions, clé qui
  ne correspond pas, PEM illisible) laisse le certificat précédent en service
  et le journal le dit ;
* TLS 1.2 au minimum, ALPN `http/1.1`.

Ce module ne parle jamais à une autorité de certification.
"""
from __future__ import annotations

import logging
import os
import shutil
import ssl
import stat
import tempfile
import threading

from .config import ApproveConfigError, _check_private, _expand

log = logging.getLogger("ameesh.approve")


#: un certificat (chaîne comprise) ou une clé PEM n'a rien à faire au-delà
MAX_PEM = 256 * 1024


def _read_private(path: str, what: str) -> tuple[bytes, tuple]:
    """Ouvre UNE fois (sans suivre de lien), contrôle le descripteur, lit.
    Rend (octets, identité du fichier effectivement lu)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError as exc:
        raise ApproveConfigError("TLS local : %s introuvable : %s" % (what, path)) from exc
    except OSError as exc:
        # ELOOP : le chemin est un lien symbolique
        raise ApproveConfigError("TLS local : %s %s illisible ou lien symbolique (fichier "
                                 "régulier attendu, pas un lien) : %s"
                                 % (what, path, exc.strerror)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ApproveConfigError("TLS local : %s %s : fichier régulier attendu (pas un "
                                     "lien ; déposer par renommage atomique)" % (what, path))
        _check_private(st, "TLS local : " + what, path)
        if st.st_size > MAX_PEM:
            raise ApproveConfigError("TLS local : %s %s : fichier trop gros" % (what, path))
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PEM:
                raise ApproveConfigError("TLS local : %s %s : fichier trop gros" % (what, path))
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks), (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _context_from_bytes(cert: bytes, key: bytes, workdir: str | None = None
                        ) -> ssl.SSLContext:
    """Contexte serveur chargé depuis ces octets exactement (copie privée
    temporaire, effacée avant de rendre la main)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    # 0700, à nous, dans le dossier d'état privé du service (ou le dossier
    # temporaire du système, à bit collant : nul autre ne peut le déplacer)
    directory = tempfile.mkdtemp(prefix=".tls-", dir=workdir)
    try:
        paths = []
        for name, data in (("cert.pem", cert), ("key.pem", key)):
            path = os.path.join(directory, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            paths.append(path)
        try:
            context.load_cert_chain(paths[0], paths[1])
        except (ssl.SSLError, OSError, ValueError) as exc:
            raise ApproveConfigError("TLS local : certificat ou clé inutilisable (%s)"
                                     % type(exc).__name__) from exc
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return context


def _identity(path: str, what: str) -> tuple:
    """Contrôle un fichier de certificat ou de clé ; rend son identité."""
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise ApproveConfigError("TLS local : %s introuvable : %s" % (what, path)) from exc
    except OSError as exc:
        raise ApproveConfigError("TLS local : %s illisible %s : %s" % (what, path, exc)) from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ApproveConfigError("TLS local : %s %s : fichier régulier attendu (pas un lien ; "
                                 "déposer par renommage atomique)" % (what, path))
    _check_private(st, "TLS local : " + what, path)
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


class TlsReloader:
    """Contexte TLS serveur, relu quand le certificat ou la clé change."""

    def __init__(self, cert: str, key: str, *, workdir: str | None = None):
        self.cert = _expand(cert)
        self.key = _expand(key)
        self.workdir = workdir
        self._lock = threading.Lock()
        self._force = False
        self._signature = None
        self._context = None
        self.loads = 0
        self._load()            # au démarrage, tout refus est fatal

    def _identities(self) -> tuple:
        return (_identity(self.cert, "certificat"), _identity(self.key, "clé privée"))

    def _load(self) -> None:
        cert, cert_id = _read_private(self.cert, "certificat")
        key, key_id = _read_private(self.key, "clé privée")
        context = _context_from_bytes(cert, key, self.workdir)
        self._context = context
        # identité des fichiers EFFECTIVEMENT lus : un fichier remplacé après
        # la lecture diffère et sera relu à la connexion suivante
        self._signature = (cert_id, key_id)
        self.loads += 1

    def request_reload(self) -> None:
        """Relecture forcée à la prochaine connexion (SIGHUP)."""
        self._force = True

    def context(self) -> ssl.SSLContext:
        with self._lock:
            try:
                changed = self._force or self._identities() != self._signature
            except ApproveConfigError as exc:
                log.warning("%s — certificat précédent conservé", exc)
                return self._context
            if changed:
                self._force = False
                try:
                    self._load()
                    log.info("TLS local : certificat rechargé")
                except ApproveConfigError as exc:
                    log.warning("%s — certificat précédent conservé", exc)
            return self._context

    def wrap(self, sock):
        """Socket acceptée → socket TLS ; la poignée de main se fait dans le
        fil de la requête (une poignée lente ne bloque pas l'acceptation)."""
        return self.context().wrap_socket(sock, server_side=True,
                                          do_handshake_on_connect=False)
