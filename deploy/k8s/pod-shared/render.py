#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7695 (P2 de #7633) : rend le chart d'un hôte pod partagé à N tenants.

Le fichier `host.exemple.yaml` (ou celui passé en `--config`) est la source :
il nomme l'hôte logique, l'image, le canon et la liste des tenants. Le rendu
produit les objets Kubernetes d'un pod partagé :

* UN superviseur (uid 10001, sans volume de tenant) ;
* N conteneurs de tenant, chacun sous l'UID de `tenancy.pod_plan()` et
  montant UNIQUEMENT son PVC ;
* N PVC `ReadWriteOnce`, un `fsGroup` neutre unique pour le pod (jamais un
  `fsGroup` par tenant, N de #7659) ;
* la ConfigMap `passwd`/`group` dérivée des mêmes plans, une ConfigMap de
  configuration par tenant (noms de comptes seulement) et celle du
  superviseur, la ConfigMap du canon ;
* les init/sidecar du canon, les `securityContext` du design §6.

Le rendu est un document d'opérateur : `render.py --check` compare les
fichiers de `rendered/` au rendu courant, `--write` les écrit, `--stdout`
les imprime. Aucun objet n'est appliqué par ce script et il ne lit aucun
secret. Le plan d'un tenant (`ameesh host tenant add … --pod`) reste la
source de l'UID ; le chart recopie ses valeurs et le battement les vérifie.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - l'opérateur installe PyYAML
    sys.exit("render.py: PyYAML est requis (pip install pyyaml)")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ameesh import tenancy  # noqa: E402  (le chemin du dépôt d'abord)

#: Fichiers du rendu, dans l'ordre de kustomization.yaml.
RENDERED = ("configmaps.yaml", "pvcs.yaml", "statefulset.yaml",
            "networkpolicy.yaml", "resourcequota.yaml", "pdb.yaml")
HEADER = (
    "# SPDX-License-Identifier: AGPL-3.0-only\n"
    "# GÉNÉRÉ par deploy/k8s/pod-shared/%s — ne pas editer a la main.\n"
    "# Source : %s ; %s\n"
)
SA_NAME = "ameesh-pod"
CANON_PATH = tenancy.POD_CANON
CANON_REPO = tenancy.POD_CANON_REPO
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
#: Adresse de métadonnées d'instance : jamais accessible depuis un tenant,
#: même quand la sortie est ouverte aux fournisseurs de modèles (design §6.4).
METADATA_CIDR = "169.254.0.0/16"


class RenderError(ValueError):
    """Configuration ou plan incohérent ; le rendu est refusé."""


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value).lower()).strip("-")
    return slug or "tenant"


def _memory_bytes(value) -> int:
    """« 2Gi », « 512Mi », « 1000M », un entier : des octets."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTPE]i?|)", text, re.IGNORECASE)
    if not match:
        raise RenderError("mémoire illisible : %r" % (value,))
    number = float(match.group(1))
    suffix = match.group(2)
    factors = {
        "": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3,
        "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
        "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3,
        "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    }
    return int(number * factors[suffix])


def _cpu_cores(value) -> float:
    """« 2 », « 250m », « 1.5 » : des cœurs (au moins 0.001)."""
    if isinstance(value, (int, float)):
        return max(0.001, float(value))
    text = str(value or "").strip()
    try:
        if text.endswith("m"):
            return max(0.001, float(text[:-1]) / 1000.0)
        return max(0.001, float(text))
    except ValueError:
        raise RenderError("CPU illisible : %r" % (value,))


def _quantity_bytes(value: int) -> str:
    """Une quantité Kubernetes exacte, lisible : « 40Gi », « 512Mi », octets."""
    value = int(value)
    for factor, unit in ((1024 ** 3, "Gi"), (1024 ** 2, "Mi"), (1024, "Ki")):
        if value and value % factor == 0:
            return "%d%s" % (value // factor, unit)
    return str(value)


def _quantity_cpu(cores: float) -> str:
    """Une quantité Kubernetes en millicores, jamais nulle."""
    return "%dm" % max(1, int(round(float(cores) * 1000.0)))


def _object_name(base: str, suffix: str) -> str:
    """Nom d'objet déterministe, borné à 63 caractères (DNS-1123)."""
    room = 63 - len(suffix) - 1
    prefix = str(base)[:max(1, room)].rstrip("-")
    return "%s-%s" % (prefix, suffix)


def _image_reference(config) -> str:
    """L'image épinglée du chart : `dépôt@sha256:<64 hex>` par défaut.

    Un tag seul est refusé quand `require_digest` est vrai (défaut du chart
    de référence) : un `latest` mutable rendrait le retour arrière et la
    qualification non reproductibles. `require_digest: false` n'est admis que
    pour un banc local, et le runbook de qualification l'interdit.
    """
    raw = str(config.get("image") or "").strip()
    if not raw:
        raise RenderError("`image` est requis")
    digest = str(config.get("image_digest") or "").strip().lower()
    if "@" in raw:
        name, _, embedded = raw.partition("@")
        embedded = embedded.strip().lower()
        if not DIGEST_RE.match(embedded):
            raise RenderError("digest d'image illisible dans `image` : %r"
                              % (embedded,))
        if digest and digest != embedded:
            raise RenderError("`image` et `image_digest` portent des digests "
                              "différents")
        digest = embedded
        raw = name
    if digest:
        if not DIGEST_RE.match(digest):
            raise RenderError("`image_digest` doit être sha256:<64 hex> : %r"
                              % (digest,))
    require = bool(config.get("require_digest", True))
    if require and not digest:
        raise RenderError("`image_digest` est requis : le chart de référence "
                          "épingle l'image par digest (require_digest: false "
                          "seulement sur un banc local)")
    # Un tag dans `image` est refusé avec un digest : le digest fait foi et
    # deux références dans le même champ brouillent le retour arrière.
    last = raw.rsplit("/", 1)[-1]
    if ":" in last:
        if digest:
            raise RenderError("`image` doit nommer un dépôt sans tag quand "
                              "`image_digest` est posé : %r" % (raw,))
        raw_name = raw.rsplit(":", 1)[0]
        if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", raw_name):
            raise RenderError("dépôt d'image illisible : %r" % (raw_name,))
        return raw
    if not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9][a-z0-9._-]*)*",
            raw):
        raise RenderError("dépôt d'image illisible : %r" % (raw,))
    return raw + ("@" + digest if digest else "")


def _version_label(config) -> str:
    """Valeur du label `app.kubernetes.io/version` (jamais le tag seul)."""
    raw = str(config.get("version") or "").strip()
    if raw:
        if not LABEL_RE.match(raw):
            raise RenderError("`version` n'est pas une valeur de label : %r"
                              % (raw,))
        return raw
    image = _image_reference(config)
    if "@sha256:" in image:
        return image.rsplit("@sha256:", 1)[1][:12]
    return _slug(image.rsplit("/", 1)[-1])


def _grace(config) -> tuple[int, int]:
    """(terminationGracePeriodSeconds, attente preStop), cohérents.

    La grâce couvre le `preStop` **et** le drainage du runner : l'attente
    preStop garde une marge de 10 s avant la fin de la grâce, et le runner
    reçoit `AMEESH_DRAIN_SECONDS` plus court que cette attente.
    """
    try:
        grace = int(config.get("grace_period", 120))
    except (TypeError, ValueError):
        raise RenderError("`grace_period` doit être un entier de secondes")
    if grace < 120:
        raise RenderError("`grace_period` doit être ≥ 120 s (design §6.1)")
    default_wait = min(90, grace - 10)
    try:
        wait = int(config.get("prestop_wait", default_wait))
    except (TypeError, ValueError):
        raise RenderError("`prestop_wait` doit être un entier de secondes")
    if wait != 0 and not (30 <= wait <= grace - 10):
        raise RenderError("`prestop_wait` doit valoir 0 ou être entre 30 et "
                          "%d s (grâce - 10)" % (grace - 10))
    return grace, wait


def _check_labels(value, field: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise RenderError("`%s` doit être un mapping non vide" % field)
    out = {}
    for key, raw in value.items():
        key = str(key)
        if not re.fullmatch(
                r"([a-z0-9]([a-z0-9.-]*[a-z0-9])?/)?"
                r"[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?", key):
            raise RenderError("clé de label illisible dans `%s` : %r"
                              % (field, key))
        text = str(raw)
        if not LABEL_RE.match(text):
            raise RenderError("valeur de label illisible dans `%s` : %r"
                              % (field, text))
        out[key] = text
    return out


def _dns_peer(config) -> dict | None:
    """Le pair DNS autorisé en sortie, ou None si `network.dns` est faux."""
    network = config.get("network") or {}
    if not isinstance(network, dict):
        raise RenderError("`network` doit être un mapping")
    dns = network.get("dns", {"namespace": "kube-system",
                              "pod_labels": {"k8s-app": "kube-dns"}})
    if dns is False:
        return None
    if not isinstance(dns, dict):
        raise RenderError("`network.dns` doit être un mapping ou false")
    namespace = str(dns.get("namespace") or "kube-system")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise RenderError("namespace DNS illisible : %r" % (namespace,))
    return {
        "namespace": namespace,
        "pod_labels": _check_labels(dns.get("pod_labels")
                                    or {"k8s-app": "kube-dns"},
                                    "network.dns.pod_labels"),
    }


def _egress_rules(config) -> list[dict]:
    """Les règles de sortie déclarées, validées ; la sortie est refusée sinon.

    Une NetworkPolicy standard ne filtre pas par nom de domaine : les
    fournisseurs de modèles se déclarent par `ipBlock` (ou par le pair que le
    CNI du cluster expose). `0.0.0.0/0` reçoit automatiquement l'exception des
    métadonnées d'instance (`169.254.0.0/16`) ; une règle plus étroite n'est
    pas élargie.
    """
    network = config.get("network") or {}
    if not isinstance(network, dict):
        raise RenderError("`network` doit être un mapping")
    entries = network.get("egress") or []
    if not isinstance(entries, list):
        raise RenderError("`network.egress` doit être une liste")
    rules = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RenderError("`network.egress[%d]` doit être un mapping"
                              % index)
        name = _slug(entry.get("name") or "regle-%d" % (index + 1))
        if name in seen:
            raise RenderError("deux règles de sortie partagent le nom %r"
                              % name)
        seen.add(name)
        peers = entry.get("to")
        if not isinstance(peers, list) or not peers:
            raise RenderError("`network.egress[%d].to` doit être une liste "
                              "non vide" % index)
        out_peers = []
        for peer in peers:
            if not isinstance(peer, dict):
                raise RenderError("pair de sortie illisible (règle %r)" % name)
            if "ipBlock" in peer:
                if len(peer) != 1:
                    raise RenderError("un `ipBlock` ne se combine pas avec un "
                                      "sélecteur (règle %r)" % name)
                block = peer.get("ipBlock")
                if not isinstance(block, dict) or not block.get("cidr"):
                    raise RenderError("`ipBlock.cidr` requis (règle %r)" % name)
                cidr = str(block["cidr"])
                try:
                    net = ipaddress.ip_network(cidr, strict=False)
                except ValueError:
                    raise RenderError("CIDR illisible (règle %r) : %r"
                                      % (name, cidr))
                if net.version != 4:
                    raise RenderError("le chart de référence n'admet que les "
                                      "CIDR IPv4 (règle %r) : %r" % (name, cidr))
                excepts = [str(item) for item in (block.get("except") or [])]
                for item in excepts:
                    try:
                        ipaddress.ip_network(item, strict=False)
                    except ValueError:
                        raise RenderError("CIDR `except` illisible (règle "
                                          "%r) : %r" % (name, item))
                if net.prefixlen == 0 and METADATA_CIDR not in excepts:
                    # Refuser explicitement les métadonnées d'instance, même
                    # quand la sortie est ouverte aux fournisseurs.
                    excepts.append(METADATA_CIDR)
                rendered = {"cidr": str(net)}
                if excepts:
                    rendered["except"] = excepts
                out_peers.append({"ipBlock": rendered})
                continue
            selector = {}
            if "namespaceSelector" in peer:
                selector["namespaceSelector"] = {
                    "matchLabels": _check_labels(
                        peer["namespaceSelector"].get("matchLabels")
                        if isinstance(peer["namespaceSelector"], dict) else None,
                        "network.egress.namespaceSelector")}
            if "podSelector" in peer:
                selector["podSelector"] = {
                    "matchLabels": _check_labels(
                        peer["podSelector"].get("matchLabels")
                        if isinstance(peer["podSelector"], dict) else None,
                        "network.egress.podSelector")}
            if not selector:
                raise RenderError("un pair de sortie n'a ni `ipBlock`, ni "
                                  "`namespaceSelector`, ni `podSelector` "
                                  "(règle %r)" % name)
            if set(peer) - {"namespaceSelector", "podSelector"}:
                raise RenderError("clé inconnue dans un pair de sortie "
                                  "(règle %r)" % name)
            out_peers.append(selector)
        ports = entry.get("ports") or []
        if not isinstance(ports, list):
            raise RenderError("`network.egress[%d].ports` doit être une liste"
                              % index)
        out_ports = []
        for port in ports:
            if not isinstance(port, dict):
                raise RenderError("port de sortie illisible (règle %r)" % name)
            protocol = str(port.get("protocol") or "TCP").upper()
            if protocol not in ("TCP", "UDP", "SCTP"):
                raise RenderError("protocole de sortie inconnu (règle %r) : %r"
                                  % (name, protocol))
            raw_port = port.get("port")
            if isinstance(raw_port, int):
                if not 1 <= raw_port <= 65535:
                    raise RenderError("port hors 1..65535 (règle %r) : %r"
                                      % (name, raw_port))
                value = raw_port
            elif isinstance(raw_port, str) and re.fullmatch(
                    r"[a-z0-9]([a-z0-9-]{0,13}[a-z0-9])?", raw_port):
                value = raw_port
            else:
                raise RenderError("port de sortie illisible (règle %r) : %r"
                                  % (name, raw_port))
            out_ports.append({"protocol": protocol, "port": value})
        rules.append({"name": name, "to": out_peers, "ports": out_ports})
    return rules


def _quota_settings(config) -> dict:
    quota = config.get("quota") or {}
    if not isinstance(quota, dict):
        raise RenderError("`quota` doit être un mapping")
    hard = quota.get("hard") or {}
    if not isinstance(hard, dict):
        raise RenderError("`quota.hard` doit être un mapping")
    extra = {}
    for key, value in hard.items():
        key = str(key)
        if not re.fullmatch(r"[a-zA-Z0-9./-]+", key):
            raise RenderError("clé de quota illisible : %r" % (key,))
        text = str(value)
        if not text:
            raise RenderError("valeur de quota vide : %r" % (key,))
        extra[key] = text
    try:
        pods = int(quota.get("pods", 1))
    except (TypeError, ValueError):
        raise RenderError("`quota.pods` doit être un entier")
    if pods < 1:
        raise RenderError("`quota.pods` doit être ≥ 1")
    return {"pods": pods, "extra": extra}


def _pdb_settings(config) -> dict:
    pdb = config.get("pdb") or {}
    if not isinstance(pdb, dict):
        raise RenderError("`pdb` doit être un mapping")
    if "min_available" in pdb and "max_unavailable" in pdb:
        raise RenderError("`pdb` : `min_available` et `max_unavailable` sont "
                          "exclusifs")
    if "max_unavailable" in pdb:
        key, api_key = "max_unavailable", "maxUnavailable"
    else:
        key, api_key = "min_available", "minAvailable"
    try:
        value = int(pdb.get(key, 1))
    except (TypeError, ValueError):
        raise RenderError("`pdb.%s` doit être un entier" % key)
    if value < 0 or (key == "min_available" and value < 1):
        raise RenderError("`pdb.%s` hors bornes : %r" % (key, value))
    return {api_key: value}


def load_config(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except OSError as exc:
        raise RenderError("configuration illisible : %s" % exc) from exc
    if not isinstance(config, dict):
        raise RenderError("la configuration doit être un mapping YAML")
    for key in ("host", "tenants"):
        if not config.get(key):
            raise RenderError("`%s` est requis" % key)
    # Valider tôt l'image, l'étiquette, la grâce et les objets de référence :
    # une erreur de chart ne doit pas attendre le rendu pour se voir.
    _image_reference(config)
    _version_label(config)
    _grace(config)
    _dns_peer(config)
    _egress_rules(config)
    _quota_settings(config)
    _pdb_settings(config)
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?",
                        str(config["host"])):
        raise RenderError("`host` n'est pas un nom d'hôte pod lisible")
    tenants = config["tenants"]
    if not isinstance(tenants, list) or not tenants:
        raise RenderError("`tenants` doit être une liste non vide")
    seen_slugs, seen_uids = set(), set()
    normalised = []
    for item in tenants:
        entry = {"tenant": item} if isinstance(item, str) else dict(item or {})
        tenant = tenancy.check_tenant(str(entry.get("tenant") or ""))
        plan = tenancy.pod_plan(
            str(config["host"]), tenant,
            uid=entry.get("uid"),
            image=_image_reference(config),
            storage_size=str((entry.get("storage_size")
                              or (config.get("tenant_defaults") or {}).get("storage_size")
                              or "20Gi")))
        slug = _slug(tenant.split(":", 1)[1])
        if slug in seen_slugs:
            raise RenderError("deux tenants partagent le nom court %r" % slug)
        if plan["uid"] in seen_uids:
            raise RenderError("deux tenants partagent l'UID %d (donner un --uid "
                              "explicite à l'un d'eux)" % plan["uid"])
        seen_slugs.add(slug)
        seen_uids.add(plan["uid"])
        entry["plan"] = plan
        normalised.append(entry)
    config = dict(config)
    config["tenants"] = normalised
    if not config.get("statefulset"):
        config["statefulset"] = "ameesh-pod-" + _slug(config["host"])
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?",
                        str(config["statefulset"])):
        raise RenderError("`statefulset` n'est pas un nom DNS-1123 lisible")
    return config


def _pod_plan_env(env) -> list[dict]:
    """Environnement d'un conteneur de tenant : celui du plan, plus le mode pod.

    Le plan porte déjà HOME/USER/LOGNAME/TMPDIR/AMEESH_* et la paire
    `safe.directory` bornée au dépôt du canon ; on n'ajoute aucun secret.
    `AMEESH_POD=1` rend le mode pod explicite au runner (les profils hors du
    home sont alors refusés à la publication, design §4.2)."""
    out = copy.deepcopy(env)
    if not any(item.get("name") == "AMEESH_POD" for item in out):
        out.append({"name": "AMEESH_POD", "value": "1"})
    return out


def _tenant_config(entry, config) -> dict:
    tenant = entry["plan"]["tenant"]
    ident = tenant.split(":", 1)[1]
    defaults = dict(config.get("tenant_defaults") or {})
    accounts = entry.get("accounts", defaults.get("accounts", {})) or {}
    canon = dict(config.get("canon") or {})
    return {
        "dsn": str(config.get("dsn") or ""),
        "schema": str(config.get("schema") or "public"),
        "project": str(config.get("project") or ""),
        "host": entry["plan"]["host"],
        "tenant": tenant,
        "runner_id": entry["plan"]["runner_id"],
        "humans": [ident],
        "pod": True,
        "canon": CANON_REPO,
        "canon_ref": "origin/%s" % (canon.get("branch") or "main"),
        "canon_sync_interval": int(defaults.get("canon_sync_interval", 0) or 0),
        "lease_ttl": int(defaults.get("lease_ttl", 120) or 120),
        "poll": int(defaults.get("poll", 5) or 5),
        "accounts": copy.deepcopy(accounts),
    }


def _supervisor_config(config) -> dict:
    supervisor = dict(config.get("supervisor") or {})
    canon = dict(config.get("canon") or {})
    hosts = [entry["plan"]["tenant"].split(":", 1)[1] for entry in config["tenants"]]
    return {
        "dsn": str(config.get("dsn") or ""),
        "schema": str(config.get("schema") or "public"),
        "project": str(config.get("project") or ""),
        "host": str(config["host"]),
        "humans": hosts,
        "pod": True,
        "canon": CANON_REPO,
        "canon_ref": "origin/%s" % (canon.get("branch") or "main"),
        "canon_sync_interval": int(supervisor.get("canon_sync_interval", 300) or 300),
        "lease_ttl": int(supervisor.get("lease_ttl", 120) or 120),
        "poll": int(supervisor.get("poll", 5) or 5),
    }


def _security_context(uid: int) -> dict:
    return {
        "runAsUser": uid,
        "runAsGroup": tenancy.POD_FS_GROUP,
        "runAsNonRoot": True,
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _args(poll: int) -> list[str]:
    return ["sh", "-ec", "umask 077\nexec ameesh run --poll %d" % int(poll)]


def _readiness() -> dict:
    return {
        "exec": {"command": ["ameesh", "doctor", "--probe"]},
        "initialDelaySeconds": 5,
        "periodSeconds": 30,
        "timeoutSeconds": 10,
        "failureThreshold": 3,
    }


def _selector_labels(config) -> dict:
    """Les labels IMMUABLES du sélecteur d'un StatefulSet.

    `app.kubernetes.io/version` ne doit jamais entrer dans le sélecteur : un
    changement de digest (mise à jour) modifierait un champ immuable et
    bloquerait le rollout. Les labels de métadonnées, eux, changent avec la
    version.
    """
    return {
        "app.kubernetes.io/name": "ameesh",
        "app.kubernetes.io/instance": _slug(str(config["statefulset"])),
    }


def _labels(config, component="pod-host") -> dict:
    labels = dict(_selector_labels(config))
    labels.update({
        "app.kubernetes.io/component": component,
        "app.kubernetes.io/part-of": "ameesh",
        "app.kubernetes.io/managed-by": "kustomize",
        "app.kubernetes.io/version": _version_label(config),
        "ameesh.dev/host": _slug(str(config["host"])),
    })
    return labels


def _prestop(config) -> dict | None:
    """Le `preStop` de drain : demander le drainage et l'attendre, borné.

    Le signal part vers PID 1 (`tini`, qui le relaie au runner) : le runner
    draine ses tours et rend ses baux, puis sort. Le kubelet n'envoie son
    propre SIGTERM qu'après le `preStop` ; si le runner est déjà sorti, il
    n'atteint plus personne (un second signal serait un arrêt immédiat). Si
    l'attente expire, le SIGTERM du kubelet borne la fin et le SIGKILL de la
    grâce reste le dernier recours. `AMEESH_DRAIN_SECONDS` est plus court que
    cette attente.
    """
    _, wait = _grace(config)
    if wait == 0:
        return None
    return {"exec": {"command": ["sh", "-ec", (
        "kill -TERM 1 2>/dev/null || true\n"
        "i=0\n"
        "while kill -0 1 2>/dev/null && [ \"$i\" -lt %d ]; do\n"
        "  i=$((i+1)); sleep 1\n"
        "done\n") % wait]}}


def _drain_seconds(config) -> int:
    _, wait = _grace(config)
    return 60 if wait == 0 else max(30, wait - 10)


def _canon_init(config) -> dict:
    canon = dict(config.get("canon") or {})
    image = _image_reference(config)
    return {
        "name": "canon",
        "image": image,
        "imagePullPolicy": str(config.get("image_pull_policy") or "IfNotPresent"),
        "args": ["sh", "-ec", (
            "install -m 600 /etc/ameesh/canon-key/ssh-privatekey /tmp/canon-key\n"
            "export GIT_SSH_COMMAND=\"ssh -i /tmp/canon-key -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ameesh/canon-key/known_hosts\"\n"
            "repo=/var/lib/ameesh/canon/repo\n"
            "if [ ! -d \"$repo/.git\" ]; then\n"
            "  git clone --quiet --branch \"$CANON_BRANCH\" -- \"$CANON_URL\" \"$repo\"\n"
            "fi\n"
            "ameesh canon sync --fetch || echo \"canon sync en échec : réclamation "
            "fermée jusqu'au prochain sync\" >&2\n"
            "rm -f /tmp/canon-key\n")],
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "AMEESH_HOST", "value": str(config["host"])},
            {"name": "AMEESH_CONFIG", "value": "/etc/ameesh/supervisor/config.json"},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                "name": "ameesh-db", "key": "password"}}},
            {"name": "CANON_URL", "valueFrom": {"configMapKeyRef": {
                "name": "ameesh-pod-canon", "key": "url"}}},
            {"name": "CANON_BRANCH", "valueFrom": {"configMapKeyRef": {
                "name": "ameesh-pod-canon", "key": "branch"}}},
        ],
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                      "limits": {"cpu": "250m", "memory": "256Mi"}},
        "securityContext": {
            "runAsUser": tenancy.POD_SUPERVISOR_UID,
            "runAsGroup": tenancy.POD_FS_GROUP,
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "privileged": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": "supervisor-config", "mountPath": "/etc/ameesh/supervisor",
             "readOnly": True},
            {"name": "canon", "mountPath": CANON_PATH},
            {"name": "canon-key", "mountPath": "/etc/ameesh/canon-key",
             "readOnly": True},
            {"name": "canon-tmp", "mountPath": "/tmp"},
        ],
    }


def _canon_fetch(config) -> dict:
    image = _image_reference(config)
    return {
        "name": "canon-fetch",
        "image": image,
        "imagePullPolicy": str(config.get("image_pull_policy") or "IfNotPresent"),
        "args": ["sh", "-ec", (
            "install -m 600 /etc/ameesh/canon-key/ssh-privatekey /tmp/canon-key\n"
            "export GIT_SSH_COMMAND=\"ssh -i /tmp/canon-key -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/ameesh/canon-key/known_hosts\"\n"
            "while :; do\n"
            "  git -C /var/lib/ameesh/canon/repo fetch --quiet --no-tags origin \\\n"
            "    || echo \"git fetch du canon en échec : dernière révision connue "
            "conservée\" >&2\n"
            "  sleep \"$CANON_FETCH_INTERVAL\" & wait $!\n"
            "done\n")],
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "CANON_FETCH_INTERVAL", "valueFrom": {"configMapKeyRef": {
                "name": "ameesh-pod-canon", "key": "fetch-interval"}}},
        ],
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                      "limits": {"cpu": "100m", "memory": "128Mi"}},
        "securityContext": _security_context(tenancy.POD_SUPERVISOR_UID),
        "volumeMounts": [
            {"name": "canon", "mountPath": CANON_PATH},
            {"name": "canon-key", "mountPath": "/etc/ameesh/canon-key",
             "readOnly": True},
            {"name": "canon-tmp", "mountPath": "/tmp"},
        ],
    }


def _supervisor(config) -> dict:
    supervisor = dict(config.get("supervisor") or {})
    poll = int(supervisor.get("poll", 5) or 5)
    limits = [(entry["plan"]["container"]["resources"]["limits"])
              for entry in config["tenants"]]
    memory = sum(_memory_bytes(limit.get("memory")) for limit in limits)
    cpu = sum(_cpu_cores(limit.get("cpu")) for limit in limits)
    prestop = _prestop(config)
    supervisor = {
        "name": "supervisor",
        "image": _image_reference(config),
        "imagePullPolicy": str(config.get("image_pull_policy") or "IfNotPresent"),
        "args": _args(poll),
        "workingDir": "/home/ameesh",
        "env": [
            {"name": "HOME", "value": "/home/ameesh"},
            {"name": "AMEESH_HOST", "value": str(config["host"])},
            {"name": "AMEESH_CONFIG", "value": "/etc/ameesh/supervisor/config.json"},
            {"name": "AMEESH_DRAIN_SECONDS", "value": str(_drain_seconds(config))},
            # La capacité DÉCLARÉE du pod, pas les mesures du nœud : load1
            # n'est jamais mesuré (design §3.2). Elle suit la somme des limites
            # des tenants, mise à jour avec le chart.
            {"name": "AMEESH_POD", "value": "1"},
            {"name": "AMEESH_POD_MEMORY_BYTES", "value": str(memory)},
            {"name": "AMEESH_POD_CPU_CORES", "value": ("%g" % cpu)},
            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                "name": "ameesh-db", "key": "password"}}},
        ],
        "readinessProbe": _readiness(),
        "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                      "limits": {"cpu": "500m", "memory": "512Mi"}},
        "securityContext": _security_context(tenancy.POD_SUPERVISOR_UID),
        "volumeMounts": [
            {"name": "supervisor-home", "mountPath": "/home/ameesh"},
            {"name": "supervisor-config", "mountPath": "/etc/ameesh/supervisor",
             "readOnly": True},
            {"name": "canon", "mountPath": CANON_PATH, "readOnly": True},
            {"name": "supervisor-tmp", "mountPath": "/tmp"},
        ],
    }
    if prestop:
        supervisor["lifecycle"] = {"preStop": prestop}
    return supervisor


def _tenant_container(entry, config) -> dict:
    plan = entry["plan"]
    defaults = dict(config.get("tenant_defaults") or {})
    poll = int(defaults.get("poll", 5) or 5)
    slug = _slug(plan["tenant"].split(":", 1)[1])
    container = copy.deepcopy(plan["container"])
    container["args"] = _args(poll)
    container["workingDir"] = plan["home"]
    container["imagePullPolicy"] = str(
        config.get("image_pull_policy") or "IfNotPresent")
    container["env"] = _pod_plan_env(plan["container"]["env"])
    # Le DSN du ConfigMap ne porte aucun mot de passe (design §6.3) : sans
    # cette variable, le runner du tenant ne peut pas ouvrir la base (pas de
    # battement, donc pas de réclamation). Même secret que le superviseur ;
    # `envFrom` reste réservé aux clés d'API du tenant.
    container["env"].append({"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
        "name": "ameesh-db", "key": "password"}}})
    container["env"].append({"name": "AMEESH_DRAIN_SECONDS",
                             "value": str(_drain_seconds(config))})
    container["readinessProbe"] = _readiness()
    container["envFrom"] = [{"secretRef": {
        "name": str(entry.get("secret") or "ameesh-harness-" + slug),
        "optional": True}}]
    prestop = _prestop(config)
    if prestop:
        container["lifecycle"] = {"preStop": prestop}
    mounts = []
    for mount in container["volumeMounts"]:
        mount = dict(mount)
        if mount["name"] == "tenant-tmp":
            mount["name"] = "tenant-tmp-" + slug
        elif mount["name"] == "tenant-config":
            mount["name"] = "tenant-config-" + slug
        mounts.append(mount)
    container["volumeMounts"] = mounts
    return container


def _statefulset(config) -> dict:
    plans = [entry["plan"] for entry in config["tenants"]]
    volumes = [
        {"name": "supervisor-home", "emptyDir": {"sizeLimit": "1Gi"}},
    ]
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = _slug(plan["tenant"].split(":", 1)[1])
        volumes.append({"name": "home-" + slug, "persistentVolumeClaim": {
            "claimName": "pvc-" + slug}})
    volumes += [
        {"name": "canon", "emptyDir": {}},
        {"name": "canon-key", "secret": {
            "secretName": "ameesh-canon-deploy-key", "defaultMode": 0o440}},
        {"name": "canon-tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}},
        {"name": "supervisor-tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        {"name": "supervisor-config", "configMap": {"name": "ameesh-pod-supervisor"}},
        {"name": "passwd", "configMap": {
            "name": "ameesh-pod-passwd",
            "items": [{"key": "passwd", "path": "passwd"}]}},
        {"name": "group", "configMap": {
            "name": "ameesh-pod-passwd",
            "items": [{"key": "group", "path": "group"}]}},
    ]
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = _slug(plan["tenant"].split(":", 1)[1])
        volumes.append({"name": "tenant-config-" + slug, "configMap": {
            "name": plan["config_maps"]["tenant"]["name"],
            "items": [{"key": "config.json", "path": "config.json"}]}})
        volumes.append({"name": "tenant-tmp-" + slug, "emptyDir": {
            "sizeLimit": plan["volumes"]["tmp"]["emptyDir"]["sizeLimit"]}})

    containers = [_supervisor(config)]
    containers += [_tenant_container(entry, config) for entry in config["tenants"]]
    containers.append(_canon_fetch(config))

    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": str(config["statefulset"]),
            "labels": _labels(config),
        },
        "spec": {
            # Un seul superviseur : deux répliques seraient deux écrivains.
            # Le bail SQL (0599) est l'arbitre, ceci la politesse du chart.
            "replicas": 1,
            "serviceName": "ameesh-pod",
            "podManagementPolicy": "OrderedReady",
            # Le retour arrière repose sur l'historique des révisions du
            # contrôleur : `kubectl rollout undo statefulset/<nom>`.
            "revisionHistoryLimit": 5,
            "minReadySeconds": 10,
            "updateStrategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"partition": 0},
            },
            # Le sélecteur ne porte QUE les labels immuables : un changement
            # de version (digest) ne doit pas toucher un champ immuable.
            "selector": {"matchLabels": _selector_labels(config)},
            "template": {
                "metadata": {"labels": _labels(config)},
                "spec": {
                    "serviceAccountName": SA_NAME,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "hostNetwork": False,
                    "hostPID": False,
                    "hostIPC": False,
                    "shareProcessNamespace": False,
                    "terminationGracePeriodSeconds": _grace(config)[0],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": tenancy.POD_SUPERVISOR_UID,
                        "runAsGroup": tenancy.POD_FS_GROUP,
                        "fsGroup": tenancy.POD_FS_GROUP,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": [_canon_init(config)],
                    "containers": containers,
                    "volumes": volumes,
                },
            },
        },
    }


def _configmaps(config) -> list[dict]:
    plans = [entry["plan"] for entry in config["tenants"]]
    # Le fichier passwd est la réunion des plans : root + superviseur, une
    # ligne par tenant (ses UID), nobody. Le group est commun (GID neutre).
    root = plans[0]["passwd"].splitlines()[0]
    supervisor = plans[0]["passwd"].splitlines()[1]
    nobody = plans[0]["passwd"].splitlines()[3]
    tenant_lines = [plan["passwd"].splitlines()[2] for plan in plans]
    passwd = "\n".join([root, supervisor, *tenant_lines, nobody]) + "\n"
    group = plans[0]["group"].rstrip("\n") + "\n"
    out = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ameesh-pod-passwd", "labels": _labels(config)},
            "data": {"passwd": passwd, "group": group},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ameesh-pod-supervisor", "labels": _labels(config)},
            "data": {"config.json": json.dumps(_supervisor_config(config), indent=2,
                                               ensure_ascii=False) + "\n"},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ameesh-pod-canon", "labels": _labels(config)},
            "data": {
                "url": str((config.get("canon") or {}).get("url") or ""),
                "branch": str((config.get("canon") or {}).get("branch") or "main"),
                "fetch-interval": str(int((config.get("canon") or {}).get(
                    "fetch_interval", 300) or 300)),
            },
        },
    ]
    for entry in config["tenants"]:
        plan = entry["plan"]
        out.append({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": plan["config_maps"]["tenant"]["name"],
                "labels": _labels(config),
            },
            "data": {"config.json": json.dumps(_tenant_config(entry, config),
                                               indent=2, ensure_ascii=False) + "\n"},
        })
    return out


def _pvcs(config) -> list[dict]:
    out = []
    for entry in config["tenants"]:
        plan = entry["plan"]
        slug = _slug(plan["tenant"].split(":", 1)[1])
        out.append({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": "pvc-" + slug,
                "labels": dict(_labels(config),
                               **{"ameesh.kubernetes.io/tenant": slug}),
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {
                    "storage": plan["volumes"]["home"]["size"]}},
            },
        })
    return out


def _network_policies(config) -> list[dict]:
    """Entrée refusée, sortie refusée par défaut puis autorisée (design §6.4).

    Une NetworkPolicy standard ne filtre pas l'intra-pod (les conteneurs
    partagent le namespace réseau) : c'est la limite assumée du design §5.1,
    pas une garde. Les règles de sortie viennent du `host.exemple.yaml` ; une
    règle dont le CIDR couvre tout Internet exclut les métadonnées d'instance.
    """
    base = str(config["statefulset"])
    selector = _selector_labels(config)
    labels = _labels(config)
    policies = [{
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": _object_name(base, "default-deny"),
            "labels": dict(labels, **{"ameesh.dev/policy": "default-deny"}),
        },
        "spec": {
            "podSelector": {"matchLabels": selector},
            # Aucune règle : tout est refusé dans les deux sens. Les règles
            # autorisées sont des politiques additives, jamais un « deny ».
            "policyTypes": ["Ingress", "Egress"],
        },
    }]
    dns = _dns_peer(config)
    if dns:
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": _object_name(base, "egress-dns"),
                "labels": dict(labels, **{"ameesh.dev/policy": "egress-dns"}),
            },
            "spec": {
                "podSelector": {"matchLabels": selector},
                "policyTypes": ["Egress"],
                "egress": [{
                    "to": [{
                        "namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": dns["namespace"]}},
                        "podSelector": {"matchLabels": dns["pod_labels"]},
                    }],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                }],
            },
        })
    for rule in _egress_rules(config):
        egress = {"to": rule["to"]}
        if rule["ports"]:
            egress["ports"] = rule["ports"]
        policies.append({
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": _object_name(base, "egress-" + rule["name"]),
                "labels": dict(labels, **{"ameesh.dev/policy":
                                          "egress-" + rule["name"]}),
            },
            "spec": {
                "podSelector": {"matchLabels": selector},
                "policyTypes": ["Egress"],
                "egress": [egress],
            },
        })
    return policies


def _resourcequota(config) -> dict:
    """La borne du namespace : un pod, N PVC, la somme des ressources du chart.

    Le chart suppose **un namespace par pod partagé** (les ConfigMap
    `ameesh-pod-*` y ont des noms fixes). La quota rend la borne visible au
    cluster et refuse un pod de débogage oublié, un PVC non déclaré ou un
    second StatefulSet.
    """
    quota = _quota_settings(config)
    plans = [entry["plan"] for entry in config["tenants"]]
    req_cpu = 0.100 + 0.050 + 0.010
    req_mem = (_memory_bytes("256Mi") + _memory_bytes("64Mi")
               + _memory_bytes("32Mi"))
    lim_cpu = 0.500 + 0.250 + 0.100
    lim_mem = (_memory_bytes("512Mi") + _memory_bytes("256Mi")
               + _memory_bytes("128Mi"))
    storage = 0
    for plan in plans:
        resources = plan["container"]["resources"]
        req_cpu += _cpu_cores(resources["requests"]["cpu"])
        req_mem += _memory_bytes(resources["requests"]["memory"])
        lim_cpu += _cpu_cores(resources["limits"]["cpu"])
        lim_mem += _memory_bytes(resources["limits"]["memory"])
        storage += _memory_bytes(plan["volumes"]["home"]["size"])
    hard = {
        "pods": str(quota["pods"]),
        "persistentvolumeclaims": str(len(plans)),
        "requests.storage": _quantity_bytes(storage),
        "requests.cpu": _quantity_cpu(req_cpu),
        "requests.memory": _quantity_bytes(req_mem),
        "limits.cpu": _quantity_cpu(lim_cpu),
        "limits.memory": _quantity_bytes(lim_mem),
        # Le pod n'expose aucun Service et aucun Ingress (design §6.4) : la
        # quota le rend impossible, pas seulement déconseillé.
        "services": "0",
        # Une ressource hors du groupe core se compte avec le préfixe
        # `count/` ; sans lui, l'API refuse le nom de clé de quota.
        "count/ingresses.networking.k8s.io": "0",
        "count/statefulsets.apps": "1",
    }
    hard.update(quota["extra"])
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {
            "name": _object_name(str(config["statefulset"]), "quota"),
            "labels": _labels(config),
        },
        "spec": {"hard": hard},
    }


def _pdb(config) -> dict:
    """Un pod, une interruption volontaire à la fois (zéro en pratique).

    `minAvailable: 1` sur une seule réplique protège d'une éviction
    accidentelle : un drain de nœud attend une décision explicite de
    l'opérateur (drainer les agents, puis supprimer le pod). Sans PDB, un
    drain viderait le pod sous les tours en cours.
    """
    settings = _pdb_settings(config)
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {
            "name": _object_name(str(config["statefulset"]), "pdb"),
            "labels": _labels(config),
        },
        "spec": dict({"selector": {"matchLabels": _selector_labels(config)}},
                     **settings),
    }


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _represent_str)


def _text(documents, source: str, tool: str = "render.py") -> str:
    body = yaml.dump_all(documents, Dumper=_Dumper, sort_keys=False,
                         default_flow_style=False, allow_unicode=True,
                         width=1000)
    note = ("`render.py --check` verifie la fraicheur." if tool == "render.py"
            else "outil d'opérateur : rien n'est appliqué ni versionné.")
    return HEADER % (tool, source, note) + body


def render(config: dict, source: str = "host.exemple.yaml") -> dict[str, str]:
    return {
        "configmaps.yaml": _text(_configmaps(config), source),
        "pvcs.yaml": _text(_pvcs(config), source),
        "statefulset.yaml": _text([_statefulset(config)], source),
        "networkpolicy.yaml": _text(_network_policies(config), source),
        "resourcequota.yaml": _text([_resourcequota(config)], source),
        "pdb.yaml": _text([_pdb(config)], source),
    }


def render_all(config: dict) -> list[dict]:
    """Le rendu en objets, pour les tests et les outils (kubectl apply -f -)."""
    return (_configmaps(config) + _pvcs(config) + [_statefulset(config)]
            + _network_policies(config)
            + [_resourcequota(config), _pdb(config)])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="render.py",
        description="Rend le chart d'un hôte pod partagé à N tenants (#7695).")
    parser.add_argument("--config", default=str(HERE / "host.exemple.yaml"))
    parser.add_argument("--out", default=str(HERE / "rendered"))
    parser.add_argument("--write", action="store_true",
                        help="écrit les fichiers rendus")
    parser.add_argument("--check", action="store_true",
                        help="compare les fichiers rendus au rendu courant")
    parser.add_argument("--stdout", action="store_true",
                        help="imprime le rendu (sans écrire)")
    parser.add_argument("--kind", action="append", default=[],
                        metavar="KIND",
                        help="avec --stdout : ne rend que ces objets "
                             "(répétable, ex. NetworkPolicy)")
    args = parser.parse_args(argv)
    if args.kind and not args.stdout:
        parser.error("--kind exige --stdout")
    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
        source = os.path.relpath(config_path, ROOT)
        rendered = render(config, source)
    except (RenderError, tenancy.TenancyError) as exc:
        print("rendu refusé : %s" % exc, file=sys.stderr)
        return 2
    if args.stdout:
        if args.kind:
            wanted = {str(kind).lower() for kind in args.kind}
            documents = [document for document in render_all(config)
                         if str(document.get("kind", "")).lower() in wanted]
            if not documents:
                print("aucun objet pour --kind %s" % ", ".join(args.kind),
                      file=sys.stderr)
                return 2
            sys.stdout.write(_text(documents, source))
            return 0
        for index, name in enumerate(RENDERED):
            if index:
                # Entre deux fichiers, le séparateur de documents manquait :
                # `--stdout | kubectl apply -f -` ne voyait qu'une partie des
                # objets. Un seul flux YAML multi-documents reste applicable.
                sys.stdout.write("---\n")
            sys.stdout.write(rendered[name])
        return 0
    out = Path(args.out).resolve()
    if args.check or not args.write:
        ecarts = 0
        for name in RENDERED:
            path = out / name
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current == rendered[name]:
                continue
            ecarts += 1
            print("écart : %s" % path, file=sys.stderr)
            for line in list(difflib.unified_diff(
                    current.splitlines(), rendered[name].splitlines(),
                    fromfile=str(path), tofile="rendu courant", lineterm=""))[:40]:
                print(line, file=sys.stderr)
        if ecarts:
            print("rendu périmé : lancer `render.py --write` et relire le diff",
                  file=sys.stderr)
            return 1
        print("rendu à jour : %d fichier(s)" % len(RENDERED))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    for name in RENDERED:
        (out / name).write_text(rendered[name], encoding="utf-8")
        print("écrit : %s" % (out / name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
