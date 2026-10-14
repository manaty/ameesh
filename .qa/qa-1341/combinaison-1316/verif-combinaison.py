#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#1341 x #1316 — vérificateur de la combinaison sur main 2458ec05.

Lecture seule : aucun push, aucun run, aucune modification de branche.
Recalcule et affirme, à partir des seules réfs d'atelier publiées :

  1. merge-tree(union1341, union1316) rc=0 -> un arbre unique ;
  2. delta combinaison vs main = exactement les 6 fichiers attendus, 0 migration ;
  3. delta combinaison vs union1316 == delta #1341 (patch-ids standard et -U0
     constants : 3c87ed0c… / 1dba6c87…) ;
  4. delta combinaison vs union1341 == delta #1316 (patch-ids 54168a46… /
     307d2749…) ;
  5. rejeu séquentiel des deux patchs sur main, dans un index temporaire,
     reproduit exactement l'arbre de la combinaison.

Usage : python3 verif-combinaison.py [-v]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]

MAIN = "2458ec053123d3f33644fef1e092fc84cc2773d9"          # main 2458ec05
U1 = "7ca354a521ab6088e8aee5ddc552512d93ec61e6"            # #1341 union v2
U2 = "0bcd2ae76c91b6b38d7c28b285d804ddf41e4491"            # #1316 union
COMBO_REF = "refs/atelier/combinaison-1341-1316/union-main-2458ec05"

FILES_1341 = {
    ".github/workflows/carte.yml",
    "scripts/map.py",
    "scripts/test-parts.py",
    "tests/test_carte_parts_completes.py",
}
FILES_1316 = {
    ".github/workflows/carte.yml",
    "tests/test_1314_carte_concurrency.py",
    "tests/test_1316_carte_dispatch.py",
}
PATCH_ID_1341 = ("1dba6c8775af29e2e6998b995ec7100cba9734bf",
                 "3c87ed0c77c29926bdbc336f55932f4785eaa363")
PATCH_ID_1316 = ("307d2749b090b427a114893261d6cccc7e2f32f7",
                 "54168a4695b6f2e71e41e380cd0bfeb35d20c06f")

FAILS: list[str] = []


def git(*args: str, index: str | None = None, check: bool = True) -> str:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    p = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                       text=True, env=env)
    if check and p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} -> rc={p.returncode}\n{p.stderr}")
    return p.stdout


def git_stdin(args: list[str], data: str, index: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", "-C", str(REPO), *args], input=data,
                          capture_output=True, text=True, env=env)


def check(ok: bool, label: str, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def patch_ids(rev_a: str, rev_b: str) -> dict[str, str]:
    out = {}
    for flag, key in (("", "standard"), ("-U0", "u0")):
        args = ["diff"] + ([flag] if flag else []) + [rev_a, rev_b]
        left = subprocess.run(["git", "-C", str(REPO), *args],
                              capture_output=True, text=True, check=True)
        right = subprocess.run(["git", "patch-id", "--stable"], input=left.stdout,
                               capture_output=True, text=True, check=True)
        out[key] = right.stdout.split()[0] if right.stdout.split() else ""
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    combo = git("rev-parse", COMBO_REF).strip()
    check(bool(combo), "réf de combinaison publiée", f"{COMBO_REF} = {combo}")
    combo_tree = git("rev-parse", f"{combo}^{{tree}}").strip()

    # 1. merge-tree rc=0 et arbre unique.
    p = subprocess.run(["git", "-C", str(REPO), "merge-tree", "--write-tree", U1, U2],
                       capture_output=True, text=True)
    check(p.returncode == 0, "merge-tree(union1341, union1316) rc=0",
          f"rc={p.returncode}")
    tree = p.stdout.splitlines()[0].strip() if p.stdout.strip() else ""
    check(tree == combo_tree, "arbre recalculé == arbre de la réf de combinaison",
          f"{tree} == {combo_tree}")

    # 2. delta vs main.
    names = set(git("diff", "--name-only", MAIN, combo).split())
    expected = FILES_1341 | FILES_1316
    check(names == expected, "delta combinaison vs main = 6 fichiers attendus",
          f"{len(names)} fichiers")
    if args.verbose:
        for n in sorted(names):
            print(f"        {n}")
    migr = [n for n in names if "migration" in n or n.endswith(".sql")]
    check(not migr, "aucune migration dans la combinaison", str(migr))

    # 3./4. constance des deux deltas.
    d1 = patch_ids(U2, combo)
    d1_ref = patch_ids(MAIN, U1)
    check(d1 == d1_ref and d1["standard"] == PATCH_ID_1341[1]
          and d1["u0"] == PATCH_ID_1341[0],
          "delta combinaison vs union1316 == delta #1341 (patch-ids constants)",
          f"std={d1['standard'][:12]} u0={d1['u0'][:12]}")
    d2 = patch_ids(U1, combo)
    d2_ref = patch_ids(MAIN, U2)
    check(d2 == d2_ref and d2["standard"] == PATCH_ID_1316[1]
          and d2["u0"] == PATCH_ID_1316[0],
          "delta combinaison vs union1341 == delta #1316 (patch-ids constants)",
          f"std={d2['standard'][:12]} u0={d2['u0'][:12]}")

    # Constance blob à blob des fichiers propres à chaque lot.
    def blob(rev: str, path: str) -> str:
        return git("rev-parse", f"{rev}:{path}").strip()

    blobs_ok = True
    for path in FILES_1341 - {".github/workflows/carte.yml"}:
        blobs_ok &= blob(U1, path) == blob(combo, path)
    for path in FILES_1316 - {".github/workflows/carte.yml"}:
        blobs_ok &= blob(U2, path) == blob(combo, path)
    check(blobs_ok, "blobs propres aux deux lots constants (hors carte.yml fusionné)")

    # 5. rejeu séquentiel dans un index temporaire.
    with tempfile.TemporaryDirectory(prefix="1341x1316-") as td:
        idx = str(pathlib.Path(td) / "index")
        git("read-tree", MAIN, index=idx)
        p1 = subprocess.run(["git", "-C", str(REPO), "diff", MAIN, U1],
                            capture_output=True, text=True, check=True)
        a1 = git_stdin(["apply", "--cached", "-"], p1.stdout, idx)
        check(a1.returncode == 0, "patch #1341 appliqué sur main (index temporaire)",
              a1.stderr.strip()[:80])
        p2 = subprocess.run(["git", "-C", str(REPO), "diff", U1, combo],
                            capture_output=True, text=True, check=True)
        a2 = git_stdin(["apply", "--cached", "-"], p2.stdout, idx)
        check(a2.returncode == 0, "patch #1316 appliqué ensuite (index temporaire)",
              a2.stderr.strip()[:80])
        replay = git("write-tree", index=idx).strip()
        check(replay == combo_tree, "rejeu séquentiel == arbre de la combinaison",
              f"{replay}")

    result = {
        "main": MAIN,
        "union_1341": U1,
        "union_1316": U2,
        "combinaison": combo,
        "arbre": combo_tree,
        "fichiers": sorted(names),
        "patch_ids": {"1341_dans_combinaison": d1, "1316_dans_combinaison": d2},
        "migrations": migr,
        "verdict": "FAIL" if FAILS else "OK",
        "fails": FAILS,
    }
    (HERE / "verif-combinaison.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: result[k] for k in
                      ("combinaison", "arbre", "verdict", "fails")},
                     ensure_ascii=False))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
