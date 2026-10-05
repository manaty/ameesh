# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration d'ameesh-approve (spec §9).

Lue dans cet ordre, la dernière source l'emporte :

1. le fichier JSON `~/.config/ameesh-approve/config.json` (ou `--config`,
   ou `AMEESH_APPROVE_CONFIG`) ;
2. les variables d'environnement `AMEESH_APPROVE_*` (`RP_ID`, `ORIGINS` —
   séparées par des virgules, comme pour `ameesh receipt verify` —,
   `PUBLIC_URL`, `BIND`, `PORT`, `TOKEN_FILE`, `STATE`, `PROPOSALS`, `LEVEL`,
   `PROFILE`, `RESERVED_ZONES`, `RESERVED_HOSTS` — séparées par des virgules —,
   `TLS_CERT`, `TLS_KEY`) ;
3. les options de la ligne de commande.

`api_via_public` n'existe que dans le fichier JSON : ouvrir l'API de service
sous l'hôte public est un choix écrit, jamais une option d'environnement.

Règles de sûreté vérifiées ici, au démarrage :

* le service n'écoute **que sur la boucle locale** (127.0.0.1, ::1). Par
  défaut il parle HTTP et un mandataire inverse expose HTTPS ; avec
  `tls_cert`/`tls_key` (lot L27), il parle TLS lui-même sur la boucle locale
  (passerelle en passthrough TLS : le tunnel de l'appareil lui livre le flux
  chiffré). Une autre interface est refusée ;
* **profil strict** (défaut, décision 0026) : le RP ID est EXACTEMENT l'hôte
  `H` de l'équipe — une seule origine, `https://H` (sans port), et
  `public_url` vide ou `https://H` (sans préfixe de chemin : pas d'hôte
  partagé par chemin) ; ni `localhost` ni adresse IP ;
* **profil compatible** (`profile: "compatible"`, explicite, pour les essais
  locaux) : chaque origine est exactement `https://hôte[:port]` (ou
  `http://localhost`, contexte sûr pour WebAuthn), et son hôte est le RP ID
  ou un de ses sous-domaines ; plusieurs origines admises ;
* dans les deux profils, le RP ID n'est jamais une zone ou un hôte réservés
  (`reserved_zones`, `reserved_hosts`, plus ceux de la décision 0026, toujours
  réservés), ni un de leurs parents : un RP ID parent couvrirait les hôtes de
  toutes les équipes de la zone ;
* le jeton de service est lu dans un fichier régulier, appartenant à
  l'utilisateur du service, en `0600` (refusé s'il est lisible par d'autres) ;
* le dossier d'état est privé (`0700`, même propriétaire).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import stat
from dataclasses import dataclass, fields, replace
from urllib.parse import urlsplit

from .. import receipts

DEFAULT_CONFIG = "~/.config/ameesh-approve/config.json"
DEFAULT_STATE = "~/.local/state/ameesh-approve"
DEFAULT_TOKEN_FILE = "~/.config/ameesh-approve/service-token"
DEFAULT_PORT = 8765

#: une demande d'approbation vit au plus 15 minutes (exp court, spec §8.1)
MAX_REQUEST_TTL = 900
MIN_REQUEST_TTL = 60
MIN_LINK_TTL = 30
MAX_ENROLL_TTL = 86400
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_FILE = 4096

#: profils de validation (lot L27)
PROFILE_STRICT = "strict"
PROFILE_COMPATIBLE = "compatible"
PROFILES = (PROFILE_STRICT, PROFILE_COMPATIBLE)
#: toujours réservés, quelle que soit la configuration (décision 0026) : la
#: zone des hôtes d'équipe et son apex d'approbation ne sont jamais un RP ID
BUILTIN_RESERVED_ZONES = ("nexlink.ph",)
BUILTIN_RESERVED_HOSTS = ("ameesh.nexlink.ph",)

_RP_ID_RE = re.compile(
    r"^(localhost|[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+)$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
_LOCAL_DEV_HOSTS = ("localhost",)


class ApproveConfigError(ValueError):
    """Configuration refusée — toujours avec une raison lisible."""


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _default_ports(scheme: str) -> str:
    return "443" if scheme == "https" else "80"


def parse_origin(origin: str) -> tuple[str, str, str]:
    """Origine sérialisée → (schéma, hôte, port explicite ou « »).

    L'origine doit être exactement ce qu'un navigateur met dans
    `clientDataJSON.origin` : minuscules, sans chemin, sans port par défaut.
    """
    if not isinstance(origin, str) or not origin:
        raise ApproveConfigError("origine vide")
    parts = urlsplit(origin)
    if parts.scheme not in ("https", "http"):
        raise ApproveConfigError("origine %r : https:// attendu" % origin)
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ApproveConfigError("origine %r : ni chemin, ni requête, ni identifiants" % origin)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError as exc:
        raise ApproveConfigError("origine %r : port illisible" % origin) from exc
    if port is not None and str(port) == _default_ports(parts.scheme):
        raise ApproveConfigError("origine %r : port par défaut à omettre" % origin)
    canonical = "%s://%s%s" % (parts.scheme, host, ":%d" % port if port is not None else "")
    if canonical != origin:
        raise ApproveConfigError("origine %r non canonique (attendu %r)" % (origin, canonical))
    if parts.scheme == "http" and host not in _LOCAL_DEV_HOSTS:
        raise ApproveConfigError("origine %r : HTTPS obligatoire hors localhost (spec §9)" % origin)
    return parts.scheme, host, "" if port is None else str(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ApproveConfig:
    rp_id: str = ""
    rp_name: str = "ameesh"
    origins: tuple = ()
    #: URL publique de base des liens (défaut : la première origine) ; peut
    #: porter un préfixe de chemin si le mandataire publie le service sous un
    #: sous-chemin.
    public_url: str = ""
    bind: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token_file: str = _expand(DEFAULT_TOKEN_FILE)
    state_dir: str = _expand(DEFAULT_STATE)
    proposals_dir: str = ""
    #: niveau exigé des authentificateurs (standard | eleve)
    level: str = "standard"
    #: durée de vie de la demande signée (exp - iat), 15 min au plus
    request_ttl: int = MAX_REQUEST_TTL
    #: durée de validité du lien ; le reste (request_ttl - link_ttl) laisse à
    #: ameesh le temps de récupérer et de consommer le reçu avant `exp`
    link_ttl: int = 600
    enroll_ttl: int = 900
    max_body: int = 64 * 1024
    rate_ip_per_minute: int = 120
    rate_token_per_minute: int = 20
    max_connections: int = 64
    socket_timeout: float = 15.0
    #: l'API de service (POST /requests, GET /receipts) n'est servie par défaut
    #: que sous un Host local (127.0.0.1:port) : ameesh l'appelle sur la boucle
    #: locale, le mandataire public ne l'expose pas.
    api_via_public: bool = False
    #: utiliser le dernier élément de X-Forwarded-For comme IP du client (à
    #: n'activer que derrière un mandataire qui pose cet en-tête)
    trust_forwarded: bool = False
    #: table des actions (lot L5) lue par la source par défaut
    action_table: str = "actions"
    #: profil de validation : strict (exploitation, défaut) | compatible
    #: (essais locaux : localhost, plusieurs origines, sous-domaines)
    profile: str = PROFILE_STRICT
    #: zones d'hôtes d'équipe (ex. une zone de pages) et hôtes réservés (ex.
    #: l'apex d'approbation) : jamais RP ID, ni eux ni leurs parents
    reserved_zones: tuple = ()
    reserved_hosts: tuple = ()
    #: TLS local facultatif (passthrough) : certificat et clé de l'hôte H,
    #: fichiers privés de l'utilisateur du service, relus à chaque changement
    tls_cert: str = ""
    tls_key: str = ""

    # ------------------------------------------------------------------
    @property
    def proposals_path(self) -> str:
        return self.proposals_dir or os.path.join(self.state_dir, "proposals")

    @property
    def base_url(self) -> str:
        return (self.public_url or (self.origins[0] if self.origins else "")).rstrip("/")

    @property
    def public_hosts(self) -> frozenset:
        """Valeurs d'en-tête Host admises pour les pages humaines."""
        hosts = set()
        for origin in self.origins:
            scheme, host, port = parse_origin(origin)
            if port:
                hosts.add("%s:%s" % (host, port))
            else:
                hosts.add(host)
                hosts.add("%s:%s" % (host, _default_ports(scheme)))
        return frozenset(hosts)

    @property
    def reserved(self) -> tuple:
        """Noms jamais admis comme RP ID (ni leurs parents) : configurés + 0026."""
        names: list = []
        for name in (BUILTIN_RESERVED_ZONES + BUILTIN_RESERVED_HOSTS
                     + _names(self.reserved_zones) + _names(self.reserved_hosts)):
            if name not in names:
                names.append(name)
        return tuple(names)

    @property
    def tls(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    def _check_reserved(self) -> None:
        zones = set(BUILTIN_RESERVED_ZONES + _names(self.reserved_zones))
        for name in self.reserved:
            if not _RP_ID_RE.fullmatch(name):
                raise ApproveConfigError("nom réservé illisible : %r (domaine en minuscules)"
                                         % (name,))
            if self.rp_id == name:
                raise ApproveConfigError(
                    "RP ID %r refusé : c'est %s ; chaque équipe a son hôte H, et son RP ID "
                    "est exactement H (décision 0026)"
                    % (self.rp_id, "une zone réservée (reserved_zones)" if name in zones
                       else "un hôte réservé (reserved_hosts)"))
            if name.endswith("." + self.rp_id):
                raise ApproveConfigError(
                    "RP ID %r refusé : parent du nom réservé %r ; un RP ID parent couvrirait "
                    "les hôtes de toutes les équipes de cette zone (décision 0026)"
                    % (self.rp_id, name))

    def _check_strict(self, origins: tuple) -> None:
        if self.rp_id == "localhost" or _is_ip(self.rp_id):
            raise ApproveConfigError(
                "profil strict : RP ID %r refusé (ni localhost ni adresse IP) ; pour un essai "
                "local, profil « compatible » explicite" % (self.rp_id,))
        if len(origins) != 1:
            raise ApproveConfigError(
                "profil strict : exactement UNE origine, https://%s (reçu %d : %s)"
                % (self.rp_id, len(origins), ", ".join(origins)))
        expected = "https://" + self.rp_id
        if origins[0] != expected:
            raise ApproveConfigError(
                "profil strict : l'origine doit être exactement %r (RP ID = hôte de l'origine, "
                "sans port), reçu %r" % (expected, origins[0]))
        if self.public_url and self.public_url.rstrip("/") != expected:
            raise ApproveConfigError(
                "profil strict : public_url doit être %r (même hôte que le RP ID, sans préfixe "
                "de chemin : pas d'hôte partagé par chemin), reçu %r"
                % (expected, self.public_url))

    def validate(self) -> "ApproveConfig":
        if not isinstance(self.rp_id, str) or not _RP_ID_RE.fullmatch(self.rp_id):
            raise ApproveConfigError("RP ID invalide ou absent : %r (domaine en minuscules)"
                                     % (self.rp_id,))
        if self.profile not in PROFILES:
            raise ApproveConfigError("profil inconnu : %r (%s)" % (self.profile,
                                                                  ", ".join(PROFILES)))
        origins = (self.origins,) if isinstance(self.origins, str) else tuple(self.origins or ())
        if not origins:
            raise ApproveConfigError("au moins une origine autorisée est requise")
        self._check_reserved()
        for origin in origins:
            _scheme, host, _port = parse_origin(origin)
            if host != self.rp_id and not host.endswith("." + self.rp_id):
                raise ApproveConfigError("origine %r hors du RP ID %r" % (origin, self.rp_id))
        if self.public_url:
            if not any(self.public_url == o or self.public_url.startswith(o + "/")
                       for o in origins):
                raise ApproveConfigError("public_url %r doit commencer par une origine autorisée"
                                         % self.public_url)
            if any(c in self.public_url for c in "?#\"'<> "):
                raise ApproveConfigError("public_url : caractères interdits")
        if self.profile == PROFILE_STRICT:
            self._check_strict(origins)
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ApproveConfigError("TLS local : tls_cert et tls_key (--tls-cert, --tls-key) "
                                     "vont ensemble")
        if not is_loopback(self.bind):
            raise ApproveConfigError(
                "adresse d'écoute %r refusée : le service n'écoute que sur la boucle locale "
                "(127.0.0.1 ou ::1) ; HTTPS est assuré par le mandataire, ou par le TLS local "
                "(--tls-cert/--tls-key) derrière une passerelle en passthrough"
                % (self.bind,))
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ApproveConfigError("port invalide : %r" % (self.port,))
        if self.level not in receipts.LEVELS:
            raise ApproveConfigError("niveau inconnu : %r (%s)" % (self.level,
                                                                   ", ".join(receipts.LEVELS)))
        if not MIN_REQUEST_TTL <= int(self.request_ttl) <= MAX_REQUEST_TTL:
            raise ApproveConfigError("request_ttl entre %d et %d s" % (MIN_REQUEST_TTL,
                                                                        MAX_REQUEST_TTL))
        if not MIN_LINK_TTL <= int(self.link_ttl) <= int(self.request_ttl):
            raise ApproveConfigError("link_ttl entre %d s et request_ttl" % MIN_LINK_TTL)
        if not MIN_REQUEST_TTL <= int(self.enroll_ttl) <= MAX_ENROLL_TTL:
            raise ApproveConfigError("enroll_ttl entre %d et %d s" % (MIN_REQUEST_TTL,
                                                                       MAX_ENROLL_TTL))
        if not 1024 <= int(self.max_body) <= 1024 * 1024:
            raise ApproveConfigError("max_body entre 1 Kio et 1 Mio")
        for name in ("rate_ip_per_minute", "rate_token_per_minute", "max_connections"):
            if int(getattr(self, name)) < 1:
                raise ApproveConfigError("%s doit être positif" % name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", self.action_table or ""):
            raise ApproveConfigError("action_table : identifiant SQL simple attendu")
        if not isinstance(self.api_via_public, bool):
            raise ApproveConfigError("api_via_public : booléen JSON attendu (true | false)")
        return replace(self, origins=origins, reserved_zones=_names(self.reserved_zones),
                       reserved_hosts=_names(self.reserved_hosts))


_NUMERIC_LABEL_RE = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")


def _is_ip(host: str) -> bool:
    """Adresse IP sous TOUTE forme qu'un résolveur ou un navigateur lirait
    comme telle (verdict codex2) : `ipaddress`, `inet_aton` (127.1, 0x7f.1,
    0177.0.0.1…), et la règle WHATWG — un dernier segment numérique
    (décimal, hexadécimal ou octal) fait de l'hôte une adresse IPv4."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError):
        pass
    labels = host.rstrip(".").split(".")
    return bool(_NUMERIC_LABEL_RE.fullmatch(labels[-1])) or all(
        _NUMERIC_LABEL_RE.fullmatch(label) for label in labels)


def _names(value) -> tuple:
    """Liste de noms d'hôte (texte séparé par des virgules ou liste), normalisée."""
    if isinstance(value, str):
        value = value.split(",")
    return tuple(str(v).strip().lower().rstrip(".") for v in (value or ()) if str(v).strip())


_ENV = {
    "AMEESH_APPROVE_RP_ID": "rp_id",
    "AMEESH_APPROVE_ORIGINS": "origins",
    "AMEESH_APPROVE_PUBLIC_URL": "public_url",
    "AMEESH_APPROVE_BIND": "bind",
    "AMEESH_APPROVE_PORT": "port",
    "AMEESH_APPROVE_TOKEN_FILE": "token_file",
    "AMEESH_APPROVE_STATE": "state_dir",
    "AMEESH_APPROVE_PROPOSALS": "proposals_dir",
    "AMEESH_APPROVE_LEVEL": "level",
    "AMEESH_APPROVE_PROFILE": "profile",
    "AMEESH_APPROVE_RESERVED_ZONES": "reserved_zones",
    "AMEESH_APPROVE_RESERVED_HOSTS": "reserved_hosts",
    "AMEESH_APPROVE_TLS_CERT": "tls_cert",
    "AMEESH_APPROVE_TLS_KEY": "tls_key",
}
_PATHS = ("token_file", "state_dir", "proposals_dir", "tls_cert", "tls_key")


def _coerce(name: str, value):
    kinds = {f.name: f.type for f in fields(ApproveConfig)}
    if name == "origins":
        if isinstance(value, str):
            return tuple(o.strip() for o in value.split(",") if o.strip())
        return tuple(value or ())
    if name in ("reserved_zones", "reserved_hosts"):
        if not isinstance(value, (str, list, tuple)):
            raise ApproveConfigError("%s : liste de noms d'hôte attendue" % name)
        return _names(value)
    if name == "api_via_public" and not isinstance(value, bool):
        # jamais « "false" » interprété : un booléen JSON, sans ambiguïté
        raise ApproveConfigError("api_via_public : booléen JSON attendu (true | false)")
    if name in _PATHS:
        return _expand(str(value)) if value else ""
    kind = kinds.get(name)
    try:
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "oui", "on")
            return bool(value)
    except (TypeError, ValueError) as exc:
        raise ApproveConfigError("%s : valeur illisible %r" % (name, value)) from exc
    return value


def load(env: dict | None = None, *, path: str | None = None,
         overrides: dict | None = None) -> ApproveConfig:
    """Fichier JSON, puis environnement, puis options ; puis validation."""
    env = dict(os.environ if env is None else env)
    cfg_path = _expand(path or env.get("AMEESH_APPROVE_CONFIG") or DEFAULT_CONFIG)
    values: dict = {}
    known = {f.name for f in fields(ApproveConfig)}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ApproveConfigError("config illisible %s : %s" % (cfg_path, exc)) from exc
        if not isinstance(raw, dict):
            raise ApproveConfigError("config %s : objet JSON attendu" % cfg_path)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ApproveConfigError("config %s : clés inconnues %s" % (cfg_path, unknown))
        values.update(raw)
    elif path:
        raise ApproveConfigError("config introuvable : %s" % cfg_path)
    for var, name in _ENV.items():
        if env.get(var):
            values[name] = env[var]
    for name, value in (overrides or {}).items():
        if value is not None and value != () and value != []:
            values[name] = value
    cfg = ApproveConfig(**{name: _coerce(name, value) for name, value in values.items()})
    return cfg.validate()


# --------------------------------------------------------------------------
# fichiers privés
# --------------------------------------------------------------------------

def _check_private(st: os.stat_result, what: str, path: str) -> None:
    if st.st_uid != os.geteuid():
        raise ApproveConfigError("%s %s : n'appartient pas à l'utilisateur du service" % (what, path))
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ApproveConfigError(
            "%s %s : permissions trop ouvertes (%04o) — chmod %s"
            % (what, path, stat.S_IMODE(st.st_mode), "700" if stat.S_ISDIR(st.st_mode) else "600"))


def ensure_private_dir(path: str) -> str:
    """Crée (0700) ou contrôle un dossier privé : ni lien, ni ouvert aux autres."""
    path = _expand(path)
    if not os.path.lexists(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ApproveConfigError("dossier %s : dossier réel attendu (pas un lien)" % path)
    _check_private(st, "dossier", path)
    return path


def read_service_token(path: str) -> str:
    """Lit le jeton de service ; refuse un fichier lisible par d'autres."""
    path = _expand(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise ApproveConfigError("jeton de service introuvable : %s "
                                 "(ameesh-approve gen-token)" % path) from exc
    except OSError as exc:
        raise ApproveConfigError("jeton de service illisible %s : %s" % (path, exc)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ApproveConfigError("jeton de service %s : fichier régulier attendu" % path)
        _check_private(st, "jeton de service", path)
        if st.st_size > MAX_TOKEN_FILE:
            raise ApproveConfigError("jeton de service %s : fichier trop gros" % path)
        raw = os.read(fd, MAX_TOKEN_FILE + 1)
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ApproveConfigError("jeton de service %s : ASCII attendu" % path) from exc
    if len(token) < MIN_TOKEN_LENGTH or not _TOKEN_RE.fullmatch(token):
        raise ApproveConfigError("jeton de service %s : au moins %d caractères imprimables, "
                                 "sans espace" % (path, MIN_TOKEN_LENGTH))
    return token


def write_service_token(path: str) -> str:
    """Crée un jeton de service neuf (256 bits) en 0600 ; refuse d'écraser."""
    path = _expand(path)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ApproveConfigError("%s existe déjà : on n'écrase pas un jeton" % path) from exc
    try:
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return path
