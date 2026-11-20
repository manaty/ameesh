#!/usr/bin/env python3
"""Resolution du conflit contract.json (fusion main ef4461b5 x lot #1359 54efc21d).

Table FERMEE : on applique les insertions du lot (base -> lot) sur la version
de main, sans reordonner les listes de main ; puis on verifie l'union contre
l'interface fusionnee (memes regles que tests/test_l107)."""
import difflib
import json
import subprocess
import sys
import typing

sys.path.insert(0, "src")
from ameesh.storage import interface as SI  # noqa: E402

PATH = "src/ameesh/mediated_executor/contract.json"


def stage(n: int) -> dict:
    out = subprocess.run(["git", "show", f":{n}:{PATH}"], capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)


base, ours, theirs = stage(1), stage(2), stage(3)

hints = typing.get_type_hints(SI.Storage)
domains = {d: cls for d, cls in hints.items()
           if isinstance(cls, type) and issubclass(cls, SI.Domain)}
all_ops = {d: sorted(n for n, v in vars(cls).items()
                     if not n.startswith("_") and getattr(v, "__isabstractmethod__", False))
           for d, cls in domains.items()}

adm_ours = {o["name"] for o in ours["operations"]}
adm_theirs = {o["name"] for o in theirs["operations"]}
assert adm_ours == adm_theirs, adm_ours ^ adm_theirs
admitted = adm_ours


def merge_list(before: list, after: list, target: list) -> list:
    """Applique les insertions before->after sur target (ancre sur le contenu)."""
    out = list(target)
    sm = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag not in ("insert", "replace"):
            continue
        for jj in range(j1, j2):
            item = after[jj]
            if item in out:
                continue
            anchor = next((after[k] for k in range(jj - 1, -1, -1) if after[k] in out), None)
            out.insert(0 if anchor is None else out.index(anchor) + 1, item)
    return out


b_ops = base["refused"]["operations"]
o_ops = ours["refused"]["operations"]
t_ops = theirs["refused"]["operations"]

new_ops: dict[str, list] = {}
for d in list(t_ops) + [d for d in o_ops if d not in t_ops]:
    before = b_ops.get(d, [])
    after = o_ops.get(d, before)
    new_ops[d] = merge_list(before, after, t_ops.get(d, []))

computed = {d: sorted(n for n in names if f"{d}.{n}" not in admitted)
            for d, names in all_ops.items()}
computed = {d: v for d, v in computed.items() if v or d in new_ops}
assert set(new_ops) == set(computed), set(new_ops) ^ set(computed)
for d in computed:
    assert set(new_ops[d]) == set(computed[d]), (d, set(new_ops[d]) ^ set(computed[d]))

total = sum(len(v) for v in all_ops.values())
old_total = theirs["refused"]["interface_total"]
reasons = dict(theirs["refused"]["reasons"])
for d, r in ours["refused"]["reasons"].items():
    reasons.setdefault(d, r)
assert set(reasons) == set(new_ops), set(reasons) ^ set(new_ops)

theirs["refused"]["operations"] = new_ops
theirs["refused"]["reasons"] = {d: reasons[d] for d in new_ops}
theirs["refused"]["interface_total"] = total

with open(PATH, "w", encoding="utf-8") as fh:
    json.dump(theirs, fh, indent=1, ensure_ascii=False)
    fh.write("\n")

print(f"interface_total {old_total} -> {total} (+{total - old_total})")
for d in computed:
    added = [n for n in new_ops[d] if n not in t_ops.get(d, [])]
    if added:
        print("  +", d, added)
