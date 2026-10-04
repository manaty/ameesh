# SPDX-License-Identifier: AGPL-3.0-only
"""Reçus d'approbation `ameesh-receipt/1` (spec §8, R4, R8, R13).

L'autorité d'un humain se **prouve** : un reçu est valide seulement si

1. sa forme est exactement celle de la spec §8.1 (aucun champ inconnu, types
   stricts, JSON sans clé en double) ;
2. la façade est admise par la politique (`ed25519` ne l'est pas par défaut :
   une clé Ed25519 peut dormir sur un hôte d'agents, elle ne fait pas autorité
   humaine) ;
3. il porte sur ce qui est attendu (empreinte d'action, `action_id`,
   décision) ;
4. il n'est pas échu (`exp` > maintenant), `iat` n'est pas dans le futur
   au-delà de la tolérance d'horloge (120 s), et sa durée de vie est bornée ;
5. l'approbateur est connu, et le credential est un authentificateur **actif**
   enrôlé pour CET approbateur et CETTE façade (registre de confiance, copie de
   travail du canon), au niveau exigé ;
6. la preuve vérifie : pour WebAuthn, `clientDataJSON` (type, challenge,
   origine, ni `crossOrigin` ni `topOrigin`), `authenticatorData` (rpIdHash,
   UP, UV, BE/BS) et la signature ES256 ou EdDSA sur
   `authenticatorData ‖ SHA-256(clientDataJSON)` ; pour `ed25519` et
   `device-es256`, la signature sur le challenge lui-même ;
7. son nonce n'a jamais été consommé — et, si on le consomme, la consommation
   est atomique (`INSERT … ON CONFLICT DO NOTHING` sur la clé primaire
   (approver, nonce) de `mesh_consumed_nonces`).

    challenge = SHA-256("ameesh-approval/1\\0" ‖ JCS(request))
    empreinte = SHA-256("ameesh-action/1\\0" ‖ JCS({action_id, project, connector,
                operation, target, args, amount, currency, policy_version}))

Les heures (`iat`, `exp`, `standing.until`) sont des secondes Unix entières ;
les montants (`amount`, `max_amount`) des entiers en unités mineures (JCS
refuse les nombres non entiers).

Approbations permanentes bornées (§8.3) : un reçu `standing` crée un grant
(`register_standing`) ; son nonce est consommé une fois, à l'enregistrement.
Chaque tentative couverte réserve ensuite son montant (`standing_reserve`,
`standing_cover`) ; une réservation n'est libérée que sur un échec certain
(`standing_release`).
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import re
import time
from dataclasses import dataclass, field

from . import authority, cose, jcs, p256
from .db import Db, DbError

RECEIPT_VERSION = "ameesh-receipt/1"
REQUEST_VERSION = 1
APPROVAL_DOMAIN = b"ameesh-approval/1\x00"
ACTION_DOMAIN = b"ameesh-action/1\x00"

FACADES = ("webauthn", "ed25519", "device-es256")
#: façades admises par défaut pour une autorité humaine (spec §8.1)
HUMAN_FACADES = frozenset({"webauthn", "device-es256"})
LEVELS = ("standard", "eleve")
DECISIONS = ("approve", "deny")
ACTION_CLASSES = ("read", "reversible", "irreversible", "costly")
ACTION_DIGEST_FIELDS = ("action_id", "project", "connector", "operation", "target",
                        "args", "amount", "currency", "policy_version")

DEFAULT_CLOCK_SKEW = 120
DEFAULT_MAX_TTL = 24 * 3600
DEFAULT_MAX_STANDING = 30 * 86400

# drapeaux d'authenticatorData (WebAuthn §6.1)
FLAG_UP, FLAG_UV, FLAG_BE, FLAG_BS, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80

# codes de refus (stables, pour les programmes ; la raison est pour les humains)
OK = "ok"
FORMAT = "format"
FACADE = "facade_refused"
KIND = "kind"
UNBOUND = "unbound"
ACTION_MISMATCH = "action_mismatch"
DIGEST_MISMATCH = "digest_mismatch"
DECISION = "decision"
EXPIRED = "expired"
NOT_YET = "iat_future"
TTL = "ttl"
UNKNOWN_APPROVER = "unknown_approver"
UNKNOWN_AUTHENTICATOR = "unknown_authenticator"
FOREIGN_AUTHENTICATOR = "foreign_authenticator"
REVOKED = "revoked_authenticator"
LEVEL = "level"
CANON = "canon_ref"
KEY = "key"
POLICY = "policy"
CLIENT_DATA = "client_data"
CLIENT_TYPE = "client_data_type"
CHALLENGE = "challenge_mismatch"
ORIGIN = "origin"
CROSS_ORIGIN = "cross_origin"
AUTH_DATA = "authenticator_data"
RP_ID = "rp_id_mismatch"
USER_PRESENCE = "user_presence"
USER_VERIFICATION = "user_verification"
BACKUP = "backup_eligible"
SIGNATURE = "bad_signature"
REPLAY = "replay"

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_HUMAN_RE = re.compile(r"^human:%s$" % _NAME)
_MEMBER_RE = re.compile(r"^(human|agent):%s$" % _NAME)
_ACTION_ID_RE = re.compile(r"^act_[0-9A-Za-z]{26}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9_:.=-]{1,1024}$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CANON_REF_RE = re.compile(r"^\S+@([0-9a-f]{40}|[0-9a-f]{64})$")
_COMMIT_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
_AAGUID_RE = re.compile(r"^[0-9a-f]{32}$")
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")

_RECEIPT_FIELDS = frozenset({"v", "request", "facade", "credential_id", "proof"})
_COMMON_FIELDS = frozenset({"v", "approver", "decision", "summary_digest", "requested_by",
                            "nonce", "iat", "exp"})
_ACTION_FIELDS = _COMMON_FIELDS | {"action_id", "digest"}
_STANDING_FIELDS = _COMMON_FIELDS | {"standing"}
_STANDING_KEYS = frozenset({"connector", "operations", "class", "max_amount", "currency", "until"})
_PROOF_FIELDS = {
    "webauthn": (frozenset({"authenticatorData", "clientDataJSON", "signature"}),
                 frozenset({"userHandle"})),
    "ed25519": (frozenset({"signature"}), frozenset()),
    "device-es256": (frozenset({"signature"}), frozenset()),
}


class ReceiptError(ValueError):
    """Reçu, clé ou entrée de registre refusés — toujours avec une raison lisible."""


# --------------------------------------------------------------------------
# encodages
# --------------------------------------------------------------------------

def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def b64u_decode(text, what: str = "valeur") -> bytes:
    """base64url sans remplissage, canonique (un seul texte par valeur)."""
    if not isinstance(text, str) or not _B64U_RE.fullmatch(text) or len(text) % 4 == 1:
        raise ReceiptError("%s : base64url sans remplissage attendu" % what)
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ReceiptError("%s : base64url illisible" % what) from exc
    if b64u_encode(data) != text:
        raise ReceiptError("%s : base64url non canonique" % what)
    return data


def _b64_lenient(text, what: str) -> bytes:
    """Clé lue dans le canon : base64 ou base64url, remplissage facultatif."""
    if not isinstance(text, str) or not text.strip():
        raise ReceiptError("%s : texte base64 attendu" % what)
    cleaned = "".join(text.split()).rstrip("=").replace("+", "-").replace("/", "_")
    if not _B64U_RE.fullmatch(cleaned) or len(cleaned) % 4 == 1:
        raise ReceiptError("%s : base64 illisible" % what)
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ReceiptError("%s : base64 illisible" % what) from exc


def _int(value, what: str) -> int:
    if type(value) is not int or abs(value) > jcs.MAX_SAFE_INTEGER:
        raise ReceiptError("%s : entier attendu (intervalle sûr)" % what)
    return value


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --------------------------------------------------------------------------
# empreintes
# --------------------------------------------------------------------------

def challenge(request: dict) -> bytes:
    """SHA-256("ameesh-approval/1\\0" ‖ JCS(request)) — 32 octets."""
    try:
        return hashlib.sha256(APPROVAL_DOMAIN + jcs.canonicalize(request)).digest()
    except jcs.JcsError as exc:
        raise ReceiptError("demande non canonisable : %s" % exc) from exc


def action_digest(fields: dict | None = None, **kwargs) -> str:
    """Empreinte d'une action (spec §7.1), « sha256:<hex> ».

    Exactement les neuf champs de la spec : un champ manquant ou en trop est une
    erreur (une empreinte qui ignorerait un champ ne lierait pas l'approbation à
    ce champ). `amount` et `currency` peuvent valoir None (null en JCS).
    """
    data = dict(fields or {})
    data.update(kwargs)
    missing = set(ACTION_DIGEST_FIELDS) - set(data)
    extra = set(data) - set(ACTION_DIGEST_FIELDS)
    if missing or extra:
        raise ReceiptError("empreinte d'action : champs manquants %s, en trop %s"
                           % (sorted(missing), sorted(extra)))
    if not isinstance(data["args"], dict):
        raise ReceiptError("empreinte d'action : args doit être un objet JSON")
    if data["amount"] is not None:
        if _int(data["amount"], "amount") < 0:
            raise ReceiptError("empreinte d'action : montant négatif")
    try:
        canonical = jcs.canonicalize(data)
    except jcs.JcsError as exc:
        raise ReceiptError("empreinte d'action : %s" % exc) from exc
    return "sha256:" + hashlib.sha256(ACTION_DOMAIN + canonical).hexdigest()


# --------------------------------------------------------------------------
# politique, verdict
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """Ce que le vérificateur exige.

    `rp_id` et `origins` sont obligatoires pour la façade WebAuthn (une origine
    exacte, ex. « https://approve.example.org »). `level='eleve'` exige un
    authentificateur enrôlé au niveau élevé et, en WebAuthn, BE=0.
    `canon_commit`, s'il est posé, exige que la ligne du registre vienne de ce
    commit du canon.
    """

    rp_id: str = ""
    origins: tuple = ()
    allow_facades: frozenset = HUMAN_FACADES
    level: str = "standard"
    clock_skew: int = DEFAULT_CLOCK_SKEW
    max_ttl: int = DEFAULT_MAX_TTL
    max_standing: int = DEFAULT_MAX_STANDING
    canon_commit: str = ""

    def __post_init__(self):
        origins = (self.origins,) if isinstance(self.origins, str) else tuple(self.origins or ())
        object.__setattr__(self, "origins", origins)
        object.__setattr__(self, "allow_facades", frozenset(self.allow_facades or ()))
        unknown = self.allow_facades - set(FACADES)
        if unknown:
            raise ReceiptError("façades inconnues dans la politique : %s" % sorted(unknown))
        if self.level not in LEVELS:
            raise ReceiptError("niveau inconnu : %r (%s)" % (self.level, ", ".join(LEVELS)))
        if self.canon_commit and not _COMMIT_RE.fullmatch(self.canon_commit):
            raise ReceiptError("canon_commit : SHA de commit en hexadécimal attendu")


@dataclass(frozen=True)
class ReceiptVerdict:
    """Résultat d'une vérification, à la manière d'`authority.Verdict`.

    `ok` : tout est vérifié (et le nonce consommé si on l'a demandé). `code`
    est stable (constantes du module) ; `reason` est lisible.
    """

    ok: bool
    code: str
    reason: str
    kind: str = ""
    approver: str = ""
    facade: str = ""
    credential_id: str = ""
    authenticator_id: int = 0
    level: str = ""
    decision: str = ""
    action_id: str = ""
    digest: str = ""
    nonce: str = ""
    challenge: str = ""
    iat: int = 0
    exp: int = 0
    standing: dict | None = None
    flags: dict = field(default_factory=dict)
    consumed: bool = False

    def __bool__(self) -> bool:
        return self.ok

    @property
    def authority(self) -> bool:
        """Approbation humaine prouvée (décision `approve`)."""
        return self.ok and self.decision == "approve"

    def short(self) -> str:
        if self.ok:
            return "reçu valide : %s par %s (%s)" % (self.decision, self.approver, self.facade)
        return self.reason

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Receipt:
    """Un reçu dont la FORME est valide (rien n'est encore vérifié)."""

    document: dict
    request: dict
    kind: str
    facade: str
    credential_id: str
    proof: dict
    proof_bytes: dict
    challenge: bytes


# --------------------------------------------------------------------------
# forme
# --------------------------------------------------------------------------

def _check_standing(standing, iat: int) -> None:
    if not isinstance(standing, dict):
        raise ReceiptError("standing : objet attendu")
    keys = set(standing)
    if keys != _STANDING_KEYS:
        raise ReceiptError("standing : champs manquants %s, en trop %s"
                           % (sorted(_STANDING_KEYS - keys), sorted(keys - _STANDING_KEYS)))
    if not isinstance(standing["connector"], str) or not _IDENT_RE.fullmatch(standing["connector"]):
        raise ReceiptError("standing.connector invalide")
    operations = standing["operations"]
    if (not isinstance(operations, list) or not operations or len(operations) > 64
            or any(not isinstance(op, str) or not _IDENT_RE.fullmatch(op) for op in operations)
            or len(set(operations)) != len(operations)):
        raise ReceiptError("standing.operations : liste non vide d'opérations distinctes attendue")
    if standing["class"] not in ACTION_CLASSES:
        raise ReceiptError("standing.class inconnue : %r" % (standing["class"],))
    if _int(standing["max_amount"], "standing.max_amount") < 0:
        raise ReceiptError("standing.max_amount négatif")
    currency = standing["currency"]
    if currency is not None and (not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency)):
        raise ReceiptError("standing.currency : code ISO 4217 (3 majuscules) ou null attendu")
    if standing["max_amount"] > 0 and currency is None:
        raise ReceiptError("standing : un plafond non nul exige une devise")
    if _int(standing["until"], "standing.until") <= iat:
        raise ReceiptError("standing.until doit suivre iat")


def check_request(request) -> str:
    """Valide la forme d'une demande ; renvoie sa nature (`action` | `standing`)."""
    if not isinstance(request, dict):
        raise ReceiptError("request : objet attendu")
    keys = set(request)
    kind = "standing" if "standing" in keys else "action"
    expected = _STANDING_FIELDS if kind == "standing" else _ACTION_FIELDS
    if keys != expected:
        raise ReceiptError("request (%s) : champs manquants %s, en trop %s"
                           % (kind, sorted(expected - keys), sorted(keys - expected)))
    if type(request["v"]) is not int or request["v"] != REQUEST_VERSION:
        raise ReceiptError("request.v : %d attendu" % REQUEST_VERSION)
    if not isinstance(request["approver"], str) or not _HUMAN_RE.fullmatch(request["approver"]):
        raise ReceiptError("request.approver : « human:<id> » attendu (un agent n'approuve jamais)")
    if request["decision"] not in DECISIONS:
        raise ReceiptError("request.decision : approve ou deny")
    if not isinstance(request["summary_digest"], str) or not _DIGEST_RE.fullmatch(request["summary_digest"]):
        raise ReceiptError("request.summary_digest : « sha256:<hex> » attendu")
    if not isinstance(request["requested_by"], str) or not _MEMBER_RE.fullmatch(request["requested_by"]):
        raise ReceiptError("request.requested_by : « agent:<id> » ou « human:<id> » attendu")
    nonce = b64u_decode(request["nonce"], "request.nonce")
    if not 16 <= len(nonce) <= 64:
        raise ReceiptError("request.nonce : au moins 128 bits (16 à 64 octets)")
    iat = _int(request["iat"], "request.iat")
    exp = _int(request["exp"], "request.exp")
    if exp <= iat:
        raise ReceiptError("request.exp doit suivre iat")
    if kind == "action":
        if not isinstance(request["action_id"], str) or not _ACTION_ID_RE.fullmatch(request["action_id"]):
            raise ReceiptError("request.action_id : « act_ » + 26 caractères attendu")
        if not isinstance(request["digest"], str) or not _DIGEST_RE.fullmatch(request["digest"]):
            raise ReceiptError("request.digest : « sha256:<hex> » attendu")
    else:
        _check_standing(request["standing"], iat)
    return kind


def parse_receipt(data) -> Receipt:
    """Lit un reçu (dict, ou texte JSON strict) et en valide la forme."""
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = jcs.loads(data)
        except jcs.JcsError as exc:
            raise ReceiptError("reçu illisible : %s" % exc) from exc
    if not isinstance(data, dict):
        raise ReceiptError("reçu : objet JSON attendu")
    keys = set(data)
    if keys != _RECEIPT_FIELDS:
        raise ReceiptError("reçu : champs manquants %s, en trop %s"
                           % (sorted(_RECEIPT_FIELDS - keys), sorted(keys - _RECEIPT_FIELDS)))
    if data["v"] != RECEIPT_VERSION:
        raise ReceiptError("reçu : version %r attendue" % RECEIPT_VERSION)
    facade = data["facade"]
    if facade not in FACADES:
        raise ReceiptError("reçu : façade inconnue %r" % (facade,))
    credential_id = data["credential_id"]
    if not isinstance(credential_id, str) or not _CREDENTIAL_RE.fullmatch(credential_id):
        raise ReceiptError("reçu : credential_id invalide")
    kind = check_request(data["request"])
    proof = data["proof"]
    if not isinstance(proof, dict):
        raise ReceiptError("reçu : proof doit être un objet")
    required, optional = _PROOF_FIELDS[facade]
    keys = set(proof)
    if not required <= keys or keys - required - optional:
        raise ReceiptError("proof (%s) : champs attendus %s" % (facade, sorted(required | optional)))
    proof_bytes = {name: b64u_decode(proof[name], "proof.%s" % name) for name in sorted(keys)}
    try:
        jcs.dumps(data)  # tout le reçu doit être du JSON canonisable (il est archivé en JCS)
    except jcs.JcsError as exc:
        raise ReceiptError("reçu non canonisable : %s" % exc) from exc
    return Receipt(
        document=data, request=data["request"], kind=kind, facade=facade,
        credential_id=credential_id, proof=proof, proof_bytes=proof_bytes,
        challenge=challenge(data["request"]),
    )


# --------------------------------------------------------------------------
# clés
# --------------------------------------------------------------------------

def parse_public_key(facade: str, blob: bytes) -> cose.PublicKey:
    """Clé enregistrée → clé prête à vérifier, selon la façade.

    * webauthn : COSE_Key (ES256/P-256 ou EdDSA/Ed25519) ;
    * ed25519 : 32 octets bruts, ou COSE_Key OKP ;
    * device-es256 : point SEC1 (65 octets 04…, 33 octets 02/03…), ou COSE_Key EC2.
    """
    blob = bytes(blob)
    try:
        if facade == "webauthn":
            return cose.parse_cose_key(blob)
        if facade == "ed25519":
            key = cose.ed25519_key(blob) if len(blob) == 32 else cose.parse_cose_key(blob)
            if key.alg != "EdDSA":
                raise ReceiptError("façade ed25519 : clé Ed25519 attendue")
            return key
        if facade == "device-es256":
            if (len(blob) == 65 and blob[0] == 0x04) or (len(blob) == 33 and blob[0] in (2, 3)):
                return cose.es256_key(p256.decode_point(blob))
            key = cose.parse_cose_key(blob)
            if key.alg != "ES256":
                raise ReceiptError("façade device-es256 : clé ES256 attendue")
            return key
    except (cose.CborError, p256.P256Error) as exc:
        raise ReceiptError("clé publique %s invalide : %s" % (facade, exc)) from exc
    raise ReceiptError("façade inconnue : %r" % (facade,))


# --------------------------------------------------------------------------
# registre des authentificateurs (lecture)
# --------------------------------------------------------------------------

_AUTH_COLUMNS = """
    id, approver, facade, credential_id, public_key, key_fingerprint, aaguid, level,
    canon_ref,
    extract(epoch from enrolled_at)::float8 AS enrolled_ts,
    extract(epoch from updated_at)::float8  AS updated_ts,
    extract(epoch from revoked_at)::float8  AS revoked_ts,
    revoked_reason
"""


def list_authenticators(db: Db, *, approver: str | None = None,
                        include_revoked: bool = False) -> list[dict]:
    sql = "SELECT %s FROM authenticators WHERE true" % _AUTH_COLUMNS
    params: list = []
    if approver:
        sql += " AND approver = %s"
        params.append(approver)
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY approver, facade, credential_id, id"
    return db.query(sql, tuple(params))


def mismatched_authenticators(db: Db, expected, *, include_revoked: bool = False) -> list[dict]:
    """Authentificateurs dont la référence au canon n'est pas celle attendue.

    `expected` : un SHA de commit (chaque ligne doit venir de ce commit), ou un
    dict {(approver, facade, credential_id): canon_ref}. Une ligne ajoutée en
    base hors synchronisation, ou restée d'un ancien canon, ressort ici.
    """
    rows = list_authenticators(db, include_revoked=include_revoked)
    if isinstance(expected, str):
        if not _COMMIT_RE.fullmatch(expected):
            raise ReceiptError("commit attendu : SHA hexadécimal (40 ou 64 caractères)")
        return [row for row in rows if not (row.get("canon_ref") or "").endswith("@" + expected)]
    mapping = dict(expected)
    out = []
    for row in rows:
        key = (row["approver"], row["facade"], row["credential_id"])
        if mapping.get(key) != row.get("canon_ref"):
            out.append(row)
    return out


def _find_authenticator(db: Db, approver: str, facade: str,
                        credential_id: str) -> tuple[dict | None, str, str]:
    rows = db.query(
        "SELECT %s FROM authenticators WHERE approver = %%s ORDER BY id" % _AUTH_COLUMNS,
        (approver,))
    if not rows:
        return None, UNKNOWN_APPROVER, (
            "approbateur %s inconnu du registre de confiance (aucun authentificateur)" % approver)
    matching = [row for row in rows
                if row["facade"] == facade and row["credential_id"] == credential_id]
    live = [row for row in matching if row.get("revoked_ts") is None]
    if live:
        return live[0], "", ""
    if matching:
        return None, REVOKED, "authentificateur %s/%s de %s révoqué" % (
            facade, credential_id[:16], approver)
    other = db.query(
        "SELECT approver FROM authenticators WHERE facade = %s AND credential_id = %s "
        "AND revoked_at IS NULL", (facade, credential_id))
    if other:
        return None, FOREIGN_AUTHENTICATOR, (
            "le credential %s/%s n'est pas enrôlé pour %s (il appartient à un autre approbateur)"
            % (facade, credential_id[:16], approver))
    return None, UNKNOWN_AUTHENTICATOR, "aucun authentificateur %s/%s enrôlé pour %s" % (
        facade, credential_id[:16], approver)


# --------------------------------------------------------------------------
# vérification
# --------------------------------------------------------------------------

def _verify_webauthn(parsed: Receipt, key: cose.PublicKey, policy: Policy,
                     enrolled_level: str) -> tuple[str, str, dict]:
    if not policy.rp_id or not policy.origins:
        return POLICY, "politique WebAuthn incomplète : rp_id et origines autorisées requis", {}
    auth_data = parsed.proof_bytes["authenticatorData"]
    client_json = parsed.proof_bytes["clientDataJSON"]
    signature = parsed.proof_bytes["signature"]

    try:
        client = jcs.loads(client_json)
    except jcs.JcsError as exc:
        return CLIENT_DATA, "clientDataJSON illisible : %s" % exc, {}
    if not isinstance(client, dict):
        return CLIENT_DATA, "clientDataJSON : objet attendu", {}
    if client.get("type") != "webauthn.get":
        return CLIENT_TYPE, "clientDataJSON.type %r : « webauthn.get » attendu" % (
            client.get("type"),), {}
    try:
        signed_challenge = b64u_decode(client.get("challenge"), "clientDataJSON.challenge")
    except ReceiptError as exc:
        return CHALLENGE, str(exc), {}
    if not hmac.compare_digest(signed_challenge, parsed.challenge):
        return CHALLENGE, "le challenge signé n'est pas celui de cette demande", {}
    origin = client.get("origin")
    if not isinstance(origin, str) or origin not in policy.origins:
        return ORIGIN, "origine %r non autorisée" % (origin,), {}
    if "crossOrigin" in client and client["crossOrigin"] is not False:
        return CROSS_ORIGIN, "clientDataJSON.crossOrigin doit être faux ou absent", {}
    if "topOrigin" in client:
        return CROSS_ORIGIN, "clientDataJSON.topOrigin présent : contexte inter-origines refusé", {}

    if len(auth_data) < 37:
        return AUTH_DATA, "authenticatorData trop court", {}
    flags = auth_data[32]
    info = {
        "up": bool(flags & FLAG_UP), "uv": bool(flags & FLAG_UV),
        "be": bool(flags & FLAG_BE), "bs": bool(flags & FLAG_BS),
        "sign_count": int.from_bytes(auth_data[33:37], "big"),
    }
    expected_rp = hashlib.sha256(policy.rp_id.encode("utf-8")).digest()
    if not hmac.compare_digest(auth_data[:32], expected_rp):
        return RP_ID, "rpIdHash différent de SHA-256(%s)" % policy.rp_id, info
    if flags & FLAG_AT:
        return AUTH_DATA, "données d'attestation dans une assertion (AT=1)", info
    if info["bs"] and not info["be"]:
        return AUTH_DATA, "drapeaux incohérents : BS=1 sans BE", info
    if flags & FLAG_ED:
        try:
            extensions, _end = cose.decode(auth_data, 37)
        except cose.CborError as exc:
            return AUTH_DATA, "extensions illisibles : %s" % exc, info
        if not isinstance(extensions, dict):
            return AUTH_DATA, "extensions : table CBOR attendue", info
    elif len(auth_data) != 37:
        return AUTH_DATA, "authenticatorData : octets en trop", info
    if not info["up"]:
        return USER_PRESENCE, "présence de l'utilisateur non attestée (UP=0)", info
    if not info["uv"]:
        return USER_VERIFICATION, "utilisateur non vérifié (UV=0)", info
    if info["be"] and (policy.level == "eleve" or enrolled_level == "eleve"):
        return BACKUP, "credential synchronisable (BE=1) refusé au niveau élevé", info
    signed = auth_data + hashlib.sha256(client_json).digest()
    if not key.verify(signed, signature):
        return SIGNATURE, "signature %s invalide" % key.alg, info
    return "", "", info


def _verify(db: Db, receipt, policy: Policy | None, *, kind: str | None,
            expected_digest: str | None, expected_action_id: str | None,
            expect_decision: str | None, consume_by: str | None,
            now: float | None) -> tuple[ReceiptVerdict, Receipt | None]:
    policy = policy or Policy()
    moment = time.time() if now is None else float(now)
    ctx: dict = {}

    def refuse(code: str, reason: str) -> tuple[ReceiptVerdict, Receipt | None]:
        return ReceiptVerdict(False, code, reason, **ctx), None

    try:
        parsed = parse_receipt(receipt)
    except ReceiptError as exc:
        return refuse(FORMAT, str(exc))
    request = parsed.request
    ctx.update(
        kind=parsed.kind, approver=request["approver"], facade=parsed.facade,
        credential_id=parsed.credential_id, decision=request["decision"],
        action_id=request.get("action_id", ""), digest=request.get("digest", ""),
        nonce=request["nonce"], challenge=parsed.challenge.hex(),
        iat=request["iat"], exp=request["exp"], standing=request.get("standing"),
    )

    # 1. façade, nature, liaison
    if parsed.facade not in policy.allow_facades:
        return refuse(FACADE, "façade %s non admise pour cette autorité (admises : %s)"
                      % (parsed.facade, ", ".join(sorted(policy.allow_facades)) or "aucune"))
    if kind is not None and parsed.kind != kind:
        return refuse(KIND, "reçu de nature %s, %s attendu" % (parsed.kind, kind))
    if parsed.kind == "standing" and consume_by:
        return refuse(KIND, "un reçu standing se consomme en enregistrant le grant "
                            "(register_standing), pas ici")
    if consume_by and (expected_digest is None or expected_action_id is None):
        return refuse(UNBOUND, "consommer un reçu exige l'empreinte et l'action_id attendus "
                               "(sinon il autoriserait n'importe quelle action)")
    if expected_action_id is not None and request.get("action_id") != expected_action_id:
        return refuse(ACTION_MISMATCH, "le reçu porte sur %s, pas sur %s"
                      % (request.get("action_id") or "un grant", expected_action_id))
    if expected_digest is not None and not _same(expected_digest, request.get("digest") or ""):
        return refuse(DIGEST_MISMATCH, "l'empreinte approuvée n'est pas celle de l'action")
    if expect_decision is not None and request["decision"] != expect_decision:
        return refuse(DECISION, "décision %s, %s attendue" % (request["decision"], expect_decision))

    # 2. temps
    iat, exp = request["iat"], request["exp"]
    if exp <= moment:
        return refuse(EXPIRED, "reçu expiré (%s)" % time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(exp)))
    if iat > moment + policy.clock_skew:
        return refuse(NOT_YET, "iat dans le futur (au-delà de %d s de tolérance)" % policy.clock_skew)
    if exp - iat > policy.max_ttl:
        return refuse(TTL, "durée de validité trop longue (%d s, maximum %d s)"
                      % (exp - iat, policy.max_ttl))
    if parsed.kind == "standing":
        until = request["standing"]["until"]
        if until <= moment:
            return refuse(EXPIRED, "grant échu (until dépassé)")
        if until - iat > policy.max_standing:
            return refuse(TTL, "grant trop long (%d s, maximum %d s)"
                          % (until - iat, policy.max_standing))

    # 3. registre de confiance
    row, code, reason = _find_authenticator(
        db, request["approver"], parsed.facade, parsed.credential_id)
    if row is None:
        return refuse(code, reason)
    ctx.update(authenticator_id=int(row["id"]), level=row["level"])
    if policy.level == "eleve" and row["level"] != "eleve":
        return refuse(LEVEL, "authentificateur de niveau %s, niveau élevé exigé" % row["level"])
    if policy.canon_commit and not (row.get("canon_ref") or "").endswith("@" + policy.canon_commit):
        return refuse(CANON, "authentificateur hors du canon attendu (%s)" % row.get("canon_ref"))
    try:
        key = parse_public_key(parsed.facade, b64u_decode(row["public_key"], "clé enregistrée"))
    except ReceiptError as exc:
        return refuse(KEY, "clé enregistrée inutilisable : %s" % exc)

    # 4. preuve
    if parsed.facade == "webauthn":
        code, reason, flags = _verify_webauthn(parsed, key, policy, row["level"])
        ctx["flags"] = flags
        if code:
            return refuse(code, reason)
    elif not key.verify(parsed.challenge, parsed.proof_bytes["signature"]):
        return refuse(SIGNATURE, "signature %s invalide" % key.alg)

    # 5. nonce
    approver, nonce = request["approver"], request["nonce"]
    if consume_by:
        if not consume_nonce(db, approver, nonce, by=consume_by,
                             challenge=parsed.challenge.hex(), authenticator_id=int(row["id"])):
            state = authority.nonce_state(db, approver, nonce)
            if state:
                return refuse(REPLAY, "nonce déjà consommé (%s)" % state)
            return refuse(REVOKED, "authentificateur révoqué pendant la vérification")
        ctx["consumed"] = True
    else:
        state = authority.nonce_state(db, approver, nonce)
        if state:
            return refuse(REPLAY, "nonce déjà consommé (%s)" % state)

    return ReceiptVerdict(True, OK, "reçu valide : %s par %s (%s)" % (
        request["decision"], approver, parsed.facade), **ctx), parsed


def verify_receipt(db: Db, receipt, policy: Policy | None = None, *,
                   kind: str | None = "action", expected_digest: str | None = None,
                   expected_action_id: str | None = None,
                   expect_decision: str | None = "approve", consume_by: str | None = None,
                   now: float | None = None) -> ReceiptVerdict:
    """Vérifie un reçu ; le consomme (une fois, atomiquement) si `consume_by`.

    Sans `consume_by`, le nonce est seulement contrôlé (non encore consommé) :
    le verdict n'autorise alors rien à lui seul — la porte consomme. Consommer
    exige `expected_digest` ET `expected_action_id` (recalculés par la porte
    depuis l'action, jamais lus dans le reçu).
    `kind=None` accepte les deux natures ; `expect_decision=None` les deux
    décisions.
    """
    verdict, _parsed = _verify(
        db, receipt, policy, kind=kind, expected_digest=expected_digest,
        expected_action_id=expected_action_id, expect_decision=expect_decision,
        consume_by=consume_by, now=now)
    return verdict


def consume_nonce(db: Db, approver: str, nonce: str, *, by: str, challenge: str = "",
                  authenticator_id: int | None = None) -> bool:
    """Consomme (approver, nonce) une seule fois. Faux si déjà consommé.

    La décision est l'`INSERT … ON CONFLICT DO NOTHING` sur la clé primaire :
    deux consommations simultanées ne peuvent pas réussir toutes les deux. Si
    `authenticator_id` est donné, l'authentificateur doit être encore actif au
    moment de l'insertion.
    """
    rows = db.query(
        """
        INSERT INTO mesh_consumed_nonces (approver, nonce, consumed_by, challenge)
        SELECT %s, %s, %s, %s
         WHERE %s::bigint IS NULL
            OR EXISTS (SELECT 1 FROM authenticators a
                        WHERE a.id = %s::bigint AND a.revoked_at IS NULL)
        ON CONFLICT (approver, nonce) DO NOTHING
        RETURNING nonce
        """,
        (approver, nonce, by, challenge or None, authenticator_id, authenticator_id),
    )
    return bool(rows)


# --------------------------------------------------------------------------
# approbations permanentes bornées (§8.3)
# --------------------------------------------------------------------------

def register_standing(db: Db, receipt, policy: Policy | None = None, *, registered_by: str,
                      now: float | None = None) -> tuple[ReceiptVerdict, int | None]:
    """Vérifie un reçu `standing` et enregistre le grant ; consomme son nonce.

    Consommation du nonce et insertion du grant sont une seule opération
    (fonction `ameesh_standing_register`). Renvoie (verdict, id du grant).
    """
    verdict, parsed = _verify(
        db, receipt, policy, kind="standing", expected_digest=None, expected_action_id=None,
        expect_decision="approve", consume_by=None, now=now)
    if not verdict.ok or parsed is None:
        return verdict, None
    standing = parsed.request["standing"]
    rows = db.query(
        "SELECT ameesh_standing_register(%s::text, %s::text, %s::text, %s::bigint, %s::text, "
        "%s::text, %s::jsonb, %s::text, %s::bigint, %s::text, %s::double precision, %s::text) "
        "AS id",
        (verdict.approver, verdict.nonce, verdict.challenge, verdict.authenticator_id,
         jcs.dumps(parsed.document), standing["connector"], jcs.dumps(standing["operations"]),
         standing["class"], standing["max_amount"], standing["currency"], standing["until"],
         registered_by),
    )
    grant_id = rows[0].get("id") if rows else None
    if grant_id is None:
        state = authority.nonce_state(db, verdict.approver, verdict.nonce)
        if state:
            return dataclasses.replace(verdict, ok=False, code=REPLAY,
                                       reason="nonce déjà consommé (%s)" % state), None
        return dataclasses.replace(verdict, ok=False, code=REVOKED,
                                   reason="authentificateur révoqué pendant l'enregistrement"), None
    return dataclasses.replace(verdict, consumed=True,
                               reason="grant #%d enregistré" % int(grant_id)), int(grant_id)


def _amount(value) -> int:
    if value is None:
        return 0
    if _int(value, "montant") < 0:
        raise ReceiptError("montant négatif")
    return value


def standing_reserve(db: Db, grant_id: int, action_id: str, amount, *, connector: str,
                     operation: str, action_class: str, currency: str | None = None,
                     reserved_by: str | None = None) -> dict | None:
    """Réserve `amount` sur le grant pour la tentative de `action_id`.

    Atomique (fonction `ameesh_standing_reserve`, une transaction) : verrou
    du grant d'abord, puis TOUT est relu et réévalué après le verrou —
    révocation, échéance comparée à l'heure réelle (`clock_timestamp()`, pas
    `now()` figé avant l'attente du verrou), authentificateur, couverture,
    cumul + montant <= max — et le lien est inséré.
    La description de l'action (connecteur, opération, classe, devise) est
    OBLIGATOIRE : la couverture est contrôlée dans la même transaction, une
    réservation sur un grant qui ne couvre pas l'action est impossible.
    Idempotente par (grant, action) : une réservation vivante est rendue
    (`reused`) seulement si la description est IDENTIQUE (montant, devise,
    connecteur, opération, classe) ; toute différence lève ReceiptError — un
    refus explicite, jamais un succès silencieux. None = pas de couverture.
    """
    if not isinstance(action_id, str) or not action_id:
        raise ReceiptError("action_id requis")
    value = _amount(amount)
    try:
        rows = db.query(
            "SELECT * FROM ameesh_standing_reserve(%s::bigint, %s::text, %s::bigint, %s::text, "
            "%s::text, %s::text, %s::text, %s::text)",
            (int(grant_id), action_id, value, reserved_by, connector, operation, action_class,
             currency),
        )
    except DbError as exc:
        if "ameesh_standing_reserve" in str(exc):
            raise ReceiptError(str(exc)) from exc
        raise
    if not rows:
        return None
    row = dict(rows[0])
    row["reused"] = bool(row.get("reused"))
    return row


def standing_release(db: Db, reservation_id: int, *, outcome: str = "failed",
                     reason: str | None = None) -> dict | None:
    """Libère une réservation après un échec CERTAIN de la tentative.

    `outcome` doit valoir `failed` : une issue `unknown` ne libère jamais (le
    paiement a peut-être eu lieu). Une seule libération par réservation ; None
    si elle était déjà libérée ou inconnue.
    """
    if outcome != "failed":
        raise ReceiptError("une réservation n'est libérée que sur un échec certain "
                           "(failed), pas sur %r" % (outcome,))
    rows = db.query("SELECT * FROM ameesh_standing_release(%s::bigint, %s::text)",
                    (int(reservation_id), reason or "failed"))
    return dict(rows[0]) if rows else None


def standing_cover(db: Db, action: dict, *, reserved_by: str | None = None) -> dict | None:
    """Trouve un grant qui couvre l'action et y réserve son montant.

    `action` : action_id, connector, operation, class, amount (facultatif),
    currency (facultatif). Une réservation vivante existante pour l'action est
    réutilisée en priorité — si elle décrit la même action, sinon ReceiptError
    (`standing_reserve`). None = aucune couverture.
    """
    action_id = action["action_id"]
    action_class = action.get("class", action.get("action_class"))
    value = _amount(action.get("amount"))
    currency = action.get("currency")
    live = db.query(
        "SELECT grant_id FROM standing_reservations WHERE action_id = %s "
        "AND released_at IS NULL ORDER BY id", (action_id,))
    candidates = [int(row["grant_id"]) for row in live]
    rows = db.query(
        """
        SELECT id FROM standing_approvals
         WHERE connector = %s AND operations @> jsonb_build_array(%s::text)
           AND action_class = %s AND revoked_at IS NULL AND valid_until > clock_timestamp()
           AND consumed_amount + %s::bigint <= max_amount
           AND (%s::bigint = 0 OR currency = %s)
         ORDER BY valid_until, id
        """,
        (action["connector"], action["operation"], action_class, value, value, currency),
    )
    candidates += [int(row["id"]) for row in rows if int(row["id"]) not in candidates]
    for grant_id in candidates:
        reserved = standing_reserve(
            db, grant_id, action_id, value, connector=action["connector"],
            operation=action["operation"], action_class=action_class, currency=currency,
            reserved_by=reserved_by)
        if reserved:
            return reserved
    return None


def revoke_standing(db: Db, grant_id: int, *, by: str) -> bool:
    """Révoque un grant (aucune réservation nouvelle ensuite).

    L'appelant a vérifié l'autorité de la révocation (spec §8.3 : un reçu, ou
    une entrée du canon) ; révoquer ne fait que retirer de l'autorité.
    """
    rows = db.query(
        "UPDATE standing_approvals SET revoked_at = now(), revoked_by = %s "
        "WHERE id = %s AND revoked_at IS NULL RETURNING id", (by, int(grant_id)))
    return bool(rows)


def get_standing(db: Db, grant_id: int) -> dict | None:
    rows = db.query(
        """
        SELECT id, approver, nonce, authenticator_id, connector, operations, action_class,
               max_amount, currency, consumed_amount, uses,
               extract(epoch from valid_until)::float8 AS until_ts,
               extract(epoch from revoked_at)::float8  AS revoked_ts, revoked_by
          FROM standing_approvals WHERE id = %s
        """, (int(grant_id),))
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# synchronisation du registre depuis le canon (§8.2)
# --------------------------------------------------------------------------

def _normalize_level(value) -> str:
    if value is None:
        return "standard"
    text = str(value).strip().lower()
    aliases = {"standard": "standard", "eleve": "eleve", "élevé": "eleve", "elevé": "eleve",
               "high": "eleve"}
    if text not in aliases:
        raise ReceiptError("niveau inconnu : %r (standard, eleve)" % (value,))
    return aliases[text]


def _normalize_aaguid(value) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower().replace("-", "")
    if not _AAGUID_RE.fullmatch(text):
        raise ReceiptError("aaguid illisible : %r" % (value,))
    return "%s-%s-%s-%s-%s" % (text[:8], text[8:12], text[12:16], text[16:20], text[20:])


def _label(approver: str, facade: str, credential_id: str) -> str:
    return "%s/%s/%s" % (approver, facade, credential_id)


def sync_authenticators(db: Db, members: list) -> dict:
    """Met la table `authenticators` en conformité avec le canon lu par ailleurs.

    `members` : [{"title": "smichea", "canon_ref": "members/smichea.md@<sha>",
    "authenticators": [{facade, credential_id, public_key, aaguid, level,
    canon_ref?}]}]. Le canon est la référence complète : un authentificateur
    actif absent de la liste est révoqué ; un changement de clé révoque
    l'ancienne ligne et en crée une nouvelle.

    Une entrée invalide — en particulier **sans référence de commit**
    (`canon_ref` = chemin@SHA) — est refusée : constat d'erreur, aucune
    écriture pour elle (ni ajout, ni mise à jour, ni révocation de la ligne
    existante de même identité). Les autres entrées sont appliquées.

    Renvoie {"added", "updated", "revoked", "unchanged", "errors"}.
    """
    errors: list[str] = []
    desired: dict[tuple[str, str], dict] = {}
    protected: set[tuple[str, str]] = set()
    claimed: dict[tuple[str, str], int] = {}

    for index, member in enumerate(members or []):
        title = member.get("title") if isinstance(member, dict) else None
        if not isinstance(title, str) or not re.fullmatch(_NAME, title):
            errors.append("membre #%d : titre invalide %r" % (index, title))
            continue
        approver = "human:" + title
        entries = member.get("authenticators") or []
        if not isinstance(entries, list):
            errors.append("%s : authenticators doit être une liste" % approver)
            continue
        for position, entry in enumerate(entries):
            where = "%s, authentificateur #%d" % (approver, position)
            if not isinstance(entry, dict):
                errors.append("%s : objet attendu" % where)
                continue
            facade = entry.get("facade")
            credential_id = entry.get("credential_id")
            if facade not in FACADES or not isinstance(credential_id, str) \
                    or not _CREDENTIAL_RE.fullmatch(credential_id):
                errors.append("%s : façade ou credential_id invalide" % where)
                continue
            identity = (facade, credential_id)
            claimed[identity] = claimed.get(identity, 0) + 1
            try:
                canon_ref = entry.get("canon_ref") or member.get("canon_ref")
                if not isinstance(canon_ref, str) or not _CANON_REF_RE.fullmatch(canon_ref):
                    raise ReceiptError("sans référence de commit du canon (chemin@SHA)")
                raw = _b64_lenient(entry.get("public_key"), "public_key")
                parse_public_key(facade, raw)
                record = {
                    "approver": approver, "facade": facade, "credential_id": credential_id,
                    "public_key": b64u_encode(raw),
                    "key_fingerprint": hashlib.sha256(raw).hexdigest(),
                    "aaguid": _normalize_aaguid(entry.get("aaguid")),
                    "level": _normalize_level(entry.get("level")),
                    "canon_ref": canon_ref,
                }
            except ReceiptError as exc:
                errors.append("%s (%s) : %s" % (where, _label(approver, *identity), exc))
                protected.add(identity)
                continue
            desired[identity] = record

    for identity, count in claimed.items():
        if count > 1:
            errors.append("%s/%s déclaré %d fois dans le canon : refusé"
                          % (identity[0], identity[1], count))
            desired.pop(identity, None)
            protected.add(identity)

    result = {"added": [], "updated": [], "revoked": [], "unchanged": 0, "errors": errors}
    active = list_authenticators(db)
    keep: set[tuple[str, str]] = set()
    for row in active:
        identity = (row["facade"], row["credential_id"])
        label = _label(row["approver"], *identity)
        if identity in protected:
            continue
        wanted = desired.get(identity)
        if wanted and wanted["approver"] == row["approver"] \
                and wanted["public_key"] == row["public_key"]:
            keep.add(identity)
            if (wanted["level"], wanted["aaguid"], wanted["canon_ref"]) != (
                    row["level"], row.get("aaguid"), row.get("canon_ref")):
                db.query(
                    "UPDATE authenticators SET level = %s, aaguid = %s, canon_ref = %s, "
                    "updated_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
                    (wanted["level"], wanted["aaguid"], wanted["canon_ref"], int(row["id"])))
                result["updated"].append(label)
            else:
                result["unchanged"] += 1
            continue
        reason = ("clé changée dans le canon" if wanted and wanted["approver"] == row["approver"]
                  else "absent du canon")
        rows = db.query(
            "UPDATE authenticators SET revoked_at = now(), revoked_reason = %s, "
            "updated_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (reason, int(row["id"])))
        if rows:
            result["revoked"].append(label)

    for identity, record in desired.items():
        if identity in keep:
            continue
        rows = db.query(
            """
            INSERT INTO authenticators
                (approver, facade, credential_id, public_key, key_fingerprint, aaguid,
                 level, canon_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (facade, credential_id) WHERE revoked_at IS NULL DO NOTHING
            RETURNING id
            """,
            (record["approver"], record["facade"], record["credential_id"],
             record["public_key"], record["key_fingerprint"], record["aaguid"],
             record["level"], record["canon_ref"]))
        label = _label(record["approver"], *identity)
        if rows:
            result["added"].append(label)
        else:
            errors.append("%s : une ligne active concurrente existe déjà" % label)
    return result
