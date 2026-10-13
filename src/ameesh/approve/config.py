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
import socket
from dataclasses import dataclass, fields, replace
from urllib.parse import urlsplit

from .. import httpkit, receipts

DEFAULT_CONFIG = "~/.config/ameesh-approve/config.json"
DEFAULT_STATE = "~/.local/state/ameesh-approve"
DEFAULT_TOKEN_FILE = "~/.config/ameesh-approve/service-token"
DEFAULT_PORT = 8765

#: une demande d'approbation vit au plus 15 minutes (exp court, spec §8.1)
MAX_REQUEST_TTL = 900
MIN_REQUEST_TTL = 60
MIN_LINK_TTL = 30
MAX_ENROLL_TTL = 86400
MIN_TOKEN_LENGTH = httpkit.MIN_TOKEN_LENGTH
MAX_TOKEN_FILE = httpkit.MAX_TOKEN_FILE

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
        raise ApproveConfigError("empty origin")
    parts = urlsplit(origin)
    if parts.scheme not in ("https", "http"):
        raise ApproveConfigError("origin %r: https:// expected" % origin)
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ApproveConfigError("origin %r: no path, query or credentials" % origin)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError as exc:
        raise ApproveConfigError("origin %r: unreadable port" % origin) from exc
    if port is not None and str(port) == _default_ports(parts.scheme):
        raise ApproveConfigError("origin %r: omit the default port" % origin)
    canonical = "%s://%s%s" % (parts.scheme, host, ":%d" % port if port is not None else "")
    if canonical != origin:
        raise ApproveConfigError("origin %r not canonical (expected %r)" % (origin, canonical))
    if parts.scheme == "http" and host not in _LOCAL_DEV_HOSTS:
        raise ApproveConfigError("origin %r: HTTPS required except on localhost (spec §9)" % origin)
    return parts.scheme, host, "" if port is None else str(port)


def is_loopback(host: str) -> bool:
    return httpkit.is_loopback(host)


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
                raise ApproveConfigError("unreadable reserved name: %r (lowercase domain)"
                                         % (name,))
            if self.rp_id == name:
                raise ApproveConfigError(
                    "RP ID %r refused: it is %s; each team has its host H, and its RP ID "
                    "is exactly H (decision 0026)"
                    % (self.rp_id, "a reserved zone (reserved_zones)" if name in zones
                       else "a reserved host (reserved_hosts)"))
            if name.endswith("." + self.rp_id):
                raise ApproveConfigError(
                    "RP ID %r refused: parent of the reserved name %r; a parent RP ID would cover "
                    "the hosts of every team in that zone (decision 0026)"
                    % (self.rp_id, name))

    def _check_strict(self, origins: tuple) -> None:
        if self.rp_id == "localhost" or _is_ip(self.rp_id):
            raise ApproveConfigError(
                "strict profile: RP ID %r refused (neither localhost nor an IP address); for a "
                "local trial, set the \"compatible\" profile explicitly" % (self.rp_id,))
        if len(origins) != 1:
            raise ApproveConfigError(
                "strict profile: exactly ONE origin, https://%s (got %d: %s)"
                % (self.rp_id, len(origins), ", ".join(origins)))
        expected = "https://" + self.rp_id
        if origins[0] != expected:
            raise ApproveConfigError(
                "strict profile: the origin must be exactly %r (RP ID = origin host, "
                "no port), got %r" % (expected, origins[0]))
        if self.public_url and self.public_url.rstrip("/") != expected:
            raise ApproveConfigError(
                "strict profile: public_url must be %r (same host as the RP ID, no path "
                "prefix: no host shared by path), got %r"
                % (expected, self.public_url))

    def validate(self) -> "ApproveConfig":
        if not isinstance(self.rp_id, str) or not _RP_ID_RE.fullmatch(self.rp_id):
            raise ApproveConfigError("RP ID invalid or missing: %r (lowercase domain)"
                                     % (self.rp_id,))
        if self.profile not in PROFILES:
            raise ApproveConfigError("unknown profile: %r (%s)" % (self.profile,
                                                                  ", ".join(PROFILES)))
        origins = (self.origins,) if isinstance(self.origins, str) else tuple(self.origins or ())
        if not origins:
            raise ApproveConfigError("at least one allowed origin is required")
        self._check_reserved()
        for origin in origins:
            _scheme, host, _port = parse_origin(origin)
            if host != self.rp_id and not host.endswith("." + self.rp_id):
                raise ApproveConfigError("origin %r outside the RP ID %r" % (origin, self.rp_id))
        if self.public_url:
            if not any(self.public_url == o or self.public_url.startswith(o + "/")
                       for o in origins):
                raise ApproveConfigError("public_url %r must start with an allowed origin"
                                         % self.public_url)
            if any(c in self.public_url for c in "?#\"'<> "):
                raise ApproveConfigError("public_url: forbidden characters")
        if self.profile == PROFILE_STRICT:
            self._check_strict(origins)
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ApproveConfigError("local TLS: tls_cert and tls_key (--tls-cert, --tls-key) "
                                     "go together")
        if not is_loopback(self.bind):
            raise ApproveConfigError(
                "listen address %r refused: the service only listens on loopback "
                "(127.0.0.1 or ::1); HTTPS comes from the reverse proxy, or from local TLS "
                "(--tls-cert/--tls-key) behind a passthrough gateway"
                % (self.bind,))
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ApproveConfigError("invalid port: %r" % (self.port,))
        if self.level not in receipts.LEVELS:
            raise ApproveConfigError("unknown level: %r (%s)" % (self.level,
                                                                   ", ".join(receipts.LEVELS)))
        if not MIN_REQUEST_TTL <= int(self.request_ttl) <= MAX_REQUEST_TTL:
            raise ApproveConfigError("request_ttl between %d and %d s" % (MIN_REQUEST_TTL,
                                                                        MAX_REQUEST_TTL))
        if not MIN_LINK_TTL <= int(self.link_ttl) <= int(self.request_ttl):
            raise ApproveConfigError("link_ttl between %d s and request_ttl" % MIN_LINK_TTL)
        if not MIN_REQUEST_TTL <= int(self.enroll_ttl) <= MAX_ENROLL_TTL:
            raise ApproveConfigError("enroll_ttl between %d and %d s" % (MIN_REQUEST_TTL,
                                                                       MAX_ENROLL_TTL))
        if not 1024 <= int(self.max_body) <= 1024 * 1024:
            raise ApproveConfigError("max_body between 1 KiB and 1 MiB")
        for name in ("rate_ip_per_minute", "rate_token_per_minute", "max_connections"):
            if int(getattr(self, name)) < 1:
                raise ApproveConfigError("%s must be positive" % name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", self.action_table or ""):
            raise ApproveConfigError("action_table: simple SQL identifier expected")
        if not isinstance(self.api_via_public, bool):
            raise ApproveConfigError("api_via_public: JSON boolean expected (true | false)")
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
            raise ApproveConfigError("%s: list of host names expected" % name)
        return _names(value)
    if name == "api_via_public" and not isinstance(value, bool):
        # jamais « "false" » interprété : un booléen JSON, sans ambiguïté
        raise ApproveConfigError("api_via_public: JSON boolean expected (true | false)")
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
        raise ApproveConfigError("%s: unreadable value %r" % (name, value)) from exc
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
            raise ApproveConfigError("unreadable config %s: %s" % (cfg_path, exc)) from exc
        if not isinstance(raw, dict):
            raise ApproveConfigError("config %s: JSON object expected" % cfg_path)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ApproveConfigError("config %s: unknown keys %s" % (cfg_path, unknown))
        values.update(raw)
    elif path:
        raise ApproveConfigError("config not found: %s" % cfg_path)
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
    httpkit.check_private(st, what, path, error=ApproveConfigError)


def ensure_private_dir(path: str) -> str:
    """Crée (0700) ou contrôle un dossier privé : ni lien, ni ouvert aux autres."""
    return httpkit.ensure_private_dir(path, error=ApproveConfigError)


def read_service_token(path: str) -> str:
    """Lit le jeton de service ; refuse un fichier lisible par d'autres."""
    return httpkit.read_token_file(path, what="service token",
                                   hint="ameesh-approve gen-token", error=ApproveConfigError)


def write_service_token(path: str) -> str:
    """Crée un jeton de service neuf (256 bits) en 0600 ; refuse d'écraser."""
    return httpkit.write_token_file(path, error=ApproveConfigError)
