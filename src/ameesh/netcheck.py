# SPDX-License-Identifier: AGPL-3.0-only
"""Un réseau de conteneurs qui chevauche une route de l'hôte (lot #3103).

Incident #3092 (23/10/2026) : les pools d'adresses par défaut de Docker
épuisés (172.17–172.31), un nouveau réseau `bridge` a reçu un /20 de
192.168.0.0/16 ; sur le Wi-Fi d'un hôtel dans la même plage, le pont a
capté le trafic du réseau local et de la passerelle : pc-smichea hors
Internet 2 h 24.

`ameesh doctor` le signale, avant même de joindre la base (une base distante
est injoignable quand le réseau local est capté), et le ménage de chaque hôte
le relève (`docker_network` de sa mesure) pour l'alerte d'hôte critique
`host_network_overlap` :

  - un réseau `bridge` dont un sous-réseau chevauche une route de l'hôte
    portée par une autre interface, ou contient la passerelle par défaut ;
  - un réseau `bridge` dans 192.168.0.0/16, la plage des réseaux locaux
    domestiques et d'hôtel : le prochain Wi-Fi peut tomber dessus.

Lecture seule (`ip -j route`, `docker network inspect`) : aucun geste root.
Le correctif d'hôte (`default-address-pools` de /etc/docker/daemon.json,
réseau recréé) reste un geste du bureau. Le verdict du médecin ne change pas ;
seul un chevauchement avéré lève l'alerte, la plage exposée reste au médecin.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import time

from . import containers, platform

PREFIX = "network    : "
INDENT = " " * len(PREFIX)
#: plage des réseaux locaux domestiques et d'hôtel, où Docker puise après
#: 172.17–172.31 (#3092)
LAN_RANGE = ipaddress.ip_network("192.168.0.0/16")
#: les pilotes dont le sous-réseau chevauche le réseau local par conception
#: ne sont pas lus : seuls les ponts posent une route sur l'hôte
BRIDGE_DRIVERS = ("bridge",)
#: un relevé du ménage plus vieux ne lève plus l'alerte (trois passages)
MAX_AGE_S = 1800
FIX = ("fix (host, root — bureau): set `default-address-pools` outside the LAN "
       "ranges in /etc/docker/daemon.json, restart Docker, recreate the network")


def _run(run, argv, timeout):
    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def host_routes(run=subprocess.run, timeout: float = 5.0) -> list[dict] | None:
    """Routes IPv4 et IPv6 de la table principale (`ip -j route`) ; None si
    `ip` manque ou échoue."""
    binary = shutil.which("ip")
    if not binary:
        return None
    out = []
    for family in ("-4", "-6"):
        text = _run(run, [binary, "-j", family, "route", "show", "table", "main"], timeout)
        if text is None:
            if family == "-6":
                continue  # IPv6 coupé sur l'hôte : les routes IPv4 suffisent
            return None
        try:
            rows = json.loads(text or "[]")
        except ValueError:
            return None
        out += [row for row in rows if isinstance(row, dict)]
    return out


def _subnets(document: dict) -> list[str]:
    ipam = document.get("IPAM") or {}
    found = [item.get("Subnet") for item in (ipam.get("Config") or [])
             if isinstance(item, dict)]
    # podman : `subnets: [{subnet, gateway}]`
    found += [item.get("subnet") for item in (document.get("subnets") or [])
              if isinstance(item, dict)]
    return [value for value in found if isinstance(value, str) and value]


def parse_networks(text: str) -> list[dict]:
    """`docker|podman network inspect` (tableau JSON ou une ligne JSON par
    réseau) → `{name, bridge, subnets}` des seuls réseaux `bridge`."""
    text = (text or "").strip()
    try:
        documents = json.loads(text) if text.startswith("[") else [
            json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        return []
    out = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        driver = document.get("Driver") or document.get("driver") or ""
        if driver not in BRIDGE_DRIVERS:
            continue
        ident = document.get("Id") or document.get("id") or ""
        options = document.get("Options") or {}
        bridge = (options.get("com.docker.network.bridge.name")
                  or document.get("network_interface")
                  or ("br-%s" % ident[:12] if ident else ""))
        out.append({"name": document.get("Name") or document.get("name") or ident[:12],
                    "bridge": bridge, "subnets": _subnets(document)})
    return out


def container_networks(cfg, run=subprocess.run, runtime=None) -> tuple[str, list[dict]] | None:
    """Le moteur de conteneurs et ses réseaux `bridge` ; None sans moteur,
    `(moteur, None)` si la lecture échoue. `runtime` : le moteur déjà résolu
    par le ménage (`containers.Runtime`), son `run` et son délai."""
    if runtime is not None:
        binary, run = runtime.binary, runtime.run
        timeout = getattr(runtime, "timeout", None)
    else:
        binary = containers.runtime_binary(getattr(cfg, "container_runtime", "") or "")
        timeout = getattr(cfg, "container_timeout", None)
    if not binary:
        return None
    timeout = min(10.0, max(0.5, float(timeout or 5.0)))
    engine = os.path.basename(binary)
    ids = _run(run, [binary, "network", "ls", "-q"], timeout)
    if ids is None:
        return engine, None
    ids = ids.split()
    if not ids:
        return engine, []
    text = _run(run, [binary, "network", "inspect", *ids], timeout)
    return engine, (None if text is None else parse_networks(text))


def _network(value):
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def conflicts(routes: list[dict], networks: list[dict]) -> list[dict]:
    """Chaque sous-réseau de pont qui chevauche une route portée par une
    autre interface que les ponts, ou contient la passerelle par défaut."""
    bridges = {network["bridge"] for network in networks if network.get("bridge")}
    bridges.add("docker0")
    out = []
    for network in networks:
        for subnet in filter(None, map(_network, network["subnets"])):
            for route in routes:
                dev = route.get("dev") or ""
                if not dev or dev in bridges or dev.startswith("veth"):
                    continue
                dst = route.get("dst") or ""
                if dst == "default":
                    gateway = route.get("gateway") or ""
                    try:
                        hit = ipaddress.ip_address(gateway) in subnet
                    except (ValueError, TypeError):
                        hit = False
                    if hit:
                        out.append({"network": network, "subnet": subnet,
                                    "route": "default via %s" % gateway, "dev": dev})
                    continue
                target = _network(dst)
                if (target is not None and target.version == subnet.version
                        and target.overlaps(subnet)):
                    out.append({"network": network, "subnet": subnet,
                                "route": str(target), "dev": dev})
    return out


def exposed(networks: list[dict]) -> list[tuple[dict, object]]:
    """Les sous-réseaux de pont dans la plage des réseaux locaux."""
    return [(network, subnet) for network in networks
            for subnet in filter(None, map(_network, network["subnets"]))
            if subnet.version == 4 and subnet.subnet_of(LAN_RANGE)]


def collect(cfg, run=subprocess.run, *, now: float | None = None,
            runtime=None) -> dict | None:
    """Le relevé sérialisable : `{sampled_ts, engine, networks, conflicts,
    exposed, error}` ; None hors Linux ou sans moteur de conteneurs. Une
    lecture impossible est une erreur nommée, jamais « aucun chevauchement »."""
    if not platform.is_linux():
        return None  # Docker Desktop : les ponts vivent dans une VM
    found = container_networks(cfg, run=run, runtime=runtime)
    if found is None:
        return None
    engine, networks = found
    out = {"sampled_ts": time.time() if now is None else float(now), "engine": engine,
           "networks": None, "conflicts": [], "exposed": [], "error": None}
    if networks is None:
        out["error"] = "%s networks not read" % engine
        return out
    out["networks"] = len(networks)
    routes = host_routes(run=run)
    if routes is None:
        out["error"] = "host routes not read (`ip -j route`)"
        return out
    out["conflicts"] = [{"network": item["network"]["name"],
                         "bridge": item["network"]["bridge"] or None,
                         "subnet": str(item["subnet"]), "route": item["route"],
                         "dev": item["dev"]} for item in conflicts(routes, networks)]
    out["exposed"] = [{"network": network["name"], "subnet": str(subnet)}
                      for network, subnet in exposed(networks)]
    return out


def describe(item: dict, engine: str) -> str:
    return ("%s network %s (%s, %s) overlaps host route %s dev %s" % (
        engine, item["network"], item["subnet"], item.get("bridge") or "?",
        item["route"], item["dev"]))


def findings(data: dict, host: str, now: float) -> list[dict]:
    """L'alerte d'hôte `host_network_overlap` (critique) tirée du dernier
    relevé du ménage, sans relancer Docker ; un relevé périmé ne dit plus
    rien (le ménage de l'hôte s'est tu : `host_unsampled` le dit)."""
    since = data.get("sampled_ts")
    if not isinstance(since, (int, float)) or now - since > MAX_AGE_S:
        return []
    found = [item for item in data.get("conflicts") or () if isinstance(item, dict)]
    if not found:
        return []
    engine = data.get("engine") or "docker"
    return [{"type": "host_network_overlap", "agent": None, "since": None,
             "value": len(found), "threshold": 1, "host": host, "critical": True,
             "urgent": True, "sampled_ts": since,
             "networks": sorted({item["network"] for item in found}),
             "subnets": sorted({item["subnet"] for item in found}),
             "message": "host %s: %s — the bridge takes that traffic, the host may lose "
                        "its LAN, DNS and Internet (#3092); %s" % (
                            host, "; ".join(describe(item, engine) for item in found), FIX)}]


def doctor_lines(cfg, run=subprocess.run) -> list[str]:
    data = collect(cfg, run=run)
    if data is None:
        return []
    engine = data["engine"]
    if data["error"]:
        return [PREFIX + "%s: overlap not checked" % data["error"]]
    lines = [PREFIX + "WARNING — %s: that traffic goes to the bridge, the host may lose "
             "its LAN and Internet (#3092)" % describe(item, engine)
             for item in data["conflicts"]]
    if data["exposed"]:
        lines.append(PREFIX + "warning — %s bridge network(s) in %s, the range of home "
                     "and hotel LANs: %s" % (engine, LAN_RANGE, ", ".join(
                         "%s (%s)" % (item["network"], item["subnet"])
                         for item in data["exposed"])))
    if lines:
        lines.append(INDENT + FIX)
        return lines
    return [PREFIX + "%d %s bridge network(s), none overlaps a host route"
            % (data["networks"], engine)]
