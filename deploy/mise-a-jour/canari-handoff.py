#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""H7: qualify installed helpers with disposable, secret-free enrollment.

Run with the candidate environment's Python, from outside its source checkout.
No DB, SSH, systemd, actual account, credential or /etc write is performed.
This proves package admission/refusal and document rollback, not a host move.
"""
import json
from pathlib import Path
import tempfile

from ameesh import handoff_factory


def qualify():
    source = handoff_factory.source_path()
    handoff_factory._trusted(source)
    module = handoff_factory._module(source)
    checks = {"installed_source": source.is_file()}
    with tempfile.TemporaryDirectory(prefix="handoff-canary-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        documents = {
            "enrollment": {"version": 1, "host": "canary"},
            "hosts": {"hosts": {"canary": {"deepseek": {
                "accounts": ["canary"], "tools": [], "secrets": []}}}},
            "repositories": {"repositories": {"ameesh": "https://github.com/manaty/ameesh.git"}},
            "secrets": {"references": {}},
            "transports": {"transports": {}},
        }
        for name, value in documents.items():
            path = root / (name + ".json")
            path.write_text(json.dumps(value), encoding="utf-8")
            path.chmod(0o600)
        factory = module.HandoffFactory(root=root)
        agent = {"name": "handoff-canary", "harness": "deepseek",
                 "session_account": "canary", "project": "ameesh"}
        checks["admitted"] = factory.requirements_for(agent, "canary").account == "canary"
        # Missing execution enrollment must never fabricate a host inventory.
        try:
            factory.adapter.inventory("canary")
        except ValueError:
            checks["missing_execution_refused"] = True
        else:
            checks["missing_execution_refused"] = False
        path = root / "hosts.json"
        previous = path.read_bytes()
        path.chmod(0o666)
        try:
            module.HandoffFactory(root=root)
        except ValueError:
            checks["unsafe_permissions_refused"] = True
        else:
            checks["unsafe_permissions_refused"] = False
        path.chmod(0o600)
        path.write_text('{"hosts":{}}', encoding="utf-8")
        try:
            module.HandoffFactory(root=root)
        except ValueError:
            checks["invalid_candidate_refused"] = True
        else:
            checks["invalid_candidate_refused"] = False
        # Restore the previous private document, then compose again.
        path.write_bytes(previous)
        checks["document_rollback"] = (
            module.HandoffFactory(root=root).requirements_for(agent, "canary").account == "canary")
        # Load the two transitive code paths without any transport/effect.
        factory.provider.bridge._module()
        factory.credential_bridge()
        checks["relative_import_chain"] = True
    return checks


if __name__ == "__main__":
    result = qualify()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if all(result.values()) else 1)
