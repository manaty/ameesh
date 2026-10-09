# SPDX-License-Identifier: AGPL-3.0-only
"""Proxy MCP (étude v2 E2) : le point d'application entre un harnais et un
serveur MCP d'outils.

Le harnais parle au proxy comme à un serveur MCP (stdio, JSON-RPC par ligne) ;
le proxy lance le vrai serveur et relaie. Il **contrôle chaque appel d'outil**
(`tools/call`) contre la fiche de la persona au canon (`tools:`) et en garde
une trace, sans les arguments (ils peuvent contenir des données ou des
secrets) :

    tools: [mcp:transport]               # tous les outils du serveur « transport »
    tools: [mcp:transport/lire_colis]    # seulement cet outil

Un appel refusé n'atteint jamais le serveur : le proxy répond lui-même une
erreur JSON-RPC. La liste des outils (`tools/list`) est filtrée de la même
façon, pour que le modèle ne voie que ce qu'il peut appeler.

    ameesh mcp-proxy --persona verif-a --server transport -- <commande du serveur…>

Dans la configuration MCP du harnais, la commande du serveur est remplacée par
celle du proxy. L'identité « pour le compte de » (D3) et les règles par
relation (D2) s'ajouteront à ce même point.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

REFUS = -32001


def allowed_tools(tools: list[str] | None, server: str) -> tuple[bool, set[str]]:
    """(tous les outils permis ?, outils permis un par un) pour `server`."""
    tous, un_par_un = False, set()
    for t in tools or []:
        if t == "mcp:%s" % server or t == "mcp:%s/*" % server:
            tous = True
        elif t.startswith("mcp:%s/" % server):
            un_par_un.add(t[len("mcp:%s/" % server):])
    return tous, un_par_un


class Policy:
    def __init__(self, persona: str, server: str, tools: list[str] | None):
        self.persona, self.server = persona, server
        self.tous, self.permis = allowed_tools(tools, server)

    def permet(self, tool: str) -> bool:
        return self.tous or tool in self.permis


class Journal:
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()

    def note(self, **entry) -> None:
        if not self.path:
            return
        entry["at"] = round(time.time(), 3)
        with self.lock:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _erreur(ident, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": REFUS, "message": message}}


def filtre_entrant(msg: dict, policy: Policy, journal: Journal) -> dict | None:
    """Message du harnais vers le serveur. Rend la réponse de refus à renvoyer
    au harnais, ou None pour relayer."""
    if msg.get("method") != "tools/call":
        return None
    tool = str((msg.get("params") or {}).get("name") or "")
    ok = policy.permet(tool)
    journal.note(persona=policy.persona, server=policy.server, tool=tool,
                 decision="permis" if ok else "refusé")
    if ok:
        return None
    return _erreur(msg.get("id"), "outil %s/%s non permis à la persona %s (fiche du canon, "
                                  "`tools:`)" % (policy.server, tool, policy.persona))


def filtre_sortant(msg: dict, policy: Policy, en_attente: dict) -> dict:
    """Message du serveur vers le harnais : la réponse à `tools/list` est filtrée."""
    if "id" in msg and en_attente.pop(msg.get("id"), None) == "tools/list":
        result = msg.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            result = dict(result)
            result["tools"] = [t for t in result["tools"]
                               if isinstance(t, dict) and policy.permet(str(t.get("name")))]
            msg = dict(msg, result=result)
    return msg


def relayer(cmd: list[str], policy: Policy, journal: Journal, entree=None, sortie=None) -> int:
    entree = entree or sys.stdin
    sortie = sortie or sys.stdout
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, bufsize=1)
    en_attente: dict = {}
    verrou = threading.Lock()

    def ecrire(obj) -> None:
        with verrou:
            sortie.write(json.dumps(obj, ensure_ascii=False) + "\n")
            sortie.flush()

    def du_serveur() -> None:
        for ligne in proc.stdout:
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                msg = json.loads(ligne)
            except json.JSONDecodeError:
                continue          # le protocole n'a que du JSON sur stdout
            ecrire(filtre_sortant(msg, policy, en_attente))

    lecteur = threading.Thread(target=du_serveur, daemon=True)
    lecteur.start()
    try:
        for ligne in entree:
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                msg = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            refus = filtre_entrant(msg, policy, journal) if isinstance(msg, dict) else None
            if refus is not None:
                ecrire(refus)
                continue
            if isinstance(msg, dict) and "id" in msg and "method" in msg:
                en_attente[msg["id"]] = msg["method"]
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        lecteur.join(timeout=5)
    return proc.returncode or 0


def main(argv) -> int:
    import argparse
    if "--" not in argv:
        print("usage: ameesh mcp-proxy --persona P --server S [--tools t1,t2] -- <commande>",
              file=sys.stderr)
        return 2
    i = argv.index("--")
    p = argparse.ArgumentParser(prog="ameesh mcp-proxy")
    p.add_argument("--persona", required=True)
    p.add_argument("--server", required=True)
    p.add_argument("--tools", help="outils permis (sinon : fiche de la persona au canon)")
    p.add_argument("--journal", help="fichier de trace (défaut : <état>/mcp/<persona>.jsonl)")
    args = p.parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd:
        print("ameesh mcp-proxy : commande du serveur manquante après --", file=sys.stderr)
        return 2
    from .config import load as load_config
    cfg = load_config()
    if args.tools is not None:
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    else:
        from . import canon as canon_mod
        tools = None
        for c in canon_mod.load_configured(cfg):
            agent = c.agent(args.persona)
            if agent is not None:
                tools = agent.tools or []
                break
        if tools is None:
            print("ameesh mcp-proxy : persona %s absente du canon : tout est refusé"
                  % args.persona, file=sys.stderr)
            tools = []
    journal = Journal(args.journal or os.path.join(cfg.state_dir, "mcp", "%s.jsonl" % args.persona))
    return relayer(cmd, Policy(args.persona, args.server, tools), journal)


# --------------------------------------------------------------------------
# configuration MCP d'une persona : chaque serveur permis passe par le proxy
# --------------------------------------------------------------------------

def persona_config(persona: str, tools: list[str] | None, servers: dict,
                   ameesh_bin: str = "ameesh") -> dict:
    """La configuration `mcpServers` d'une persona (format Claude Code, repris par
    les autres harnais) : seuls les serveurs dont elle a au moins un outil
    permis, chacun lancé à travers `ameesh mcp-proxy`.

    `servers` vient de l'hôte (`mcp_servers` : nom → {command, args, env}),
    jamais du canon : la fiche dit ce qui est PERMIS, l'hôte dit COMMENT lancer.
    """
    out = {}
    for name, spec in sorted((servers or {}).items()):
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        tous, un_par_un = allowed_tools(tools, name)
        if not (tous or un_par_un):
            continue
        entry = {"command": ameesh_bin,
                 "args": ["mcp-proxy", "--persona", persona, "--server", name, "--",
                          str(spec["command"]), *[str(a) for a in spec.get("args") or []]]}
        if isinstance(spec.get("env"), dict):
            entry["env"] = {str(k): str(v) for k, v in spec["env"].items()}
        out[name] = entry
    return {"mcpServers": out}


def config_main(argv) -> int:
    """`ameesh mcp-config <persona>` : configuration MCP de la persona, sur stdout."""
    import argparse
    from . import canon as canon_mod
    from .config import load as load_config
    p = argparse.ArgumentParser(prog="ameesh mcp-config")
    p.add_argument("persona")
    args = p.parse_args(argv)
    cfg = load_config()
    tools = None
    for c in canon_mod.load_configured(cfg):
        agent = c.agent(args.persona)
        if agent is not None:
            tools = agent.tools or []
            break
    if tools is None:
        print("ameesh mcp-config : persona %s absente du canon" % args.persona, file=sys.stderr)
        return 2
    print(json.dumps(persona_config(args.persona, tools, getattr(cfg, "mcp_servers", {}) or {}),
                     ensure_ascii=False, indent=2))
    return 0
