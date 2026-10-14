#!/bin/sh
# #976 — vérification lecture seule de la constance du delta du lot après réintégration de main.
# Usage : sh verifier.sh   (les objets doivent être présents ; les réfs d'atelier suffisent)
# Sortie : une ligne PASS/FAIL par contrôle, code de sortie 1 si un contrôle échoue.
set -u
A=04a6281d909d2fb56c742067b32cd1cadd529bb7   # tête gelée du lot = PR #777 = origin/lot/976-...
B=fcff03db4e77aeee6ccaa3bb09debde3e5ad089c   # main d'avant #1003 (base d'union)
M=dc40ccbb023e9f517687a81cf5854ed26e713ac6   # main courante (#1003 fusionné)
C=57fa1824a378423d6fcf92d7ced90231d5ab203f   # candidat préparé
# NB : l'arbre d'auto-fusion n'est pas figé (cf. section 4) ; seuls les blobs de base le sont.
fail=0
ok()   { printf 'PASS  %s\n' "$1"; }
ko()   { printf 'FAIL  %s\n' "$1"; fail=1; }
chk()  { if [ "$2" = "$3" ]; then ok "$1 : $2"; else ko "$1 : attendu $2, lu $3"; fi; }
tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT

echo "== 0. identités =="
chk "commit candidat"        "$C" "$(git rev-parse "$C")"
chk "arbre candidat"         "df596d3bd277d778b46d149436569e0dfe1cf61d" "$(git rev-parse "$C^{tree}")"
chk "parents candidat"       "$A $M" "$(git log -1 --format=%P "$C")"
chk "merge-base(A,M)"        "$B" "$(git merge-base "$A" "$M")"

echo "== 1. même ensemble de fichiers =="
git diff --name-only "$B" "$A" | sort > "$tmp/l1"
git diff --name-only "$M" "$C" | sort > "$tmp/l2"
if cmp -s "$tmp/l1" "$tmp/l2"; then ok "14/14 fichiers identiques ($(wc -l < "$tmp/l1") fichiers)"; else ko "ensembles de fichiers différents"; diff "$tmp/l1" "$tmp/l2"; fi

echo "== 2. diff du lot, avant vs après (fichier par fichier) =="
while IFS= read -r f; do
  key=$(printf '%s' "$f" | tr '/' '_')
  git diff "$B" "$A" -- "$f" > "$tmp/avant"
  git diff "$M" "$C" -- "$f" > "$tmp/apres"
  if cmp -s "$tmp/avant" "$tmp/apres"; then
    ok "diff textuellement identique : $f"
  else
    grep -E '^[+-]' "$tmp/avant" | grep -vE '^(\+\+\+|---)' | sort > "$tmp/la"
    grep -E '^[+-]' "$tmp/apres" | grep -vE '^(\+\+\+|---)' | sort > "$tmp/lb"
    if cmp -s "$tmp/la" "$tmp/lb"; then
      ok "ensemble de lignes +/- identique : $f ($(wc -l < "$tmp/la") lignes)"
    else
      ko "diff et lignes différents : $f"
    fi
  fi
done < "$tmp/l1"

echo "== 3. blobs =="
n=0; bad=0
while IFS= read -r f; do
  [ "$f" = "docs/EXPLOITATION.md" ] && continue
  n=$((n+1))
  [ "$(git rev-parse "$A:$f")" = "$(git rev-parse "$C:$f")" ] || bad=$((bad+1))
done < "$tmp/l1"
chk "blobs du lot non touchés par main, identiques à la tête gelée" "$n" "$((n-bad))"
for f in src/ameesh/merge_pr.py src/ameesh/mesh_cli.py tests/test_1003_merge_pr.py; do
  chk "blob main conservé : $f" "$(git rev-parse "$M:$f")" "$(git rev-parse "$C:$f")"
done

echo "== 4. auto-fusion trois points =="
# NB : l'OID de l'arbre d'auto-fusion dépend de l'orthographe des arguments dans les étiquettes
# de conflit (« 04a6281d9 » court vs SHA complet) ; on ne fige donc que les blobs de base et le
# delta d'un seul fichier, stables.
git merge-tree --write-tree 04a6281d9 dc40ccbb > "$tmp/mt" 2> "$tmp/mt.err"; rc=$?
chk "merge-tree rc" "1" "$rc"
chk "fichiers en conflit" "docs/EXPLOITATION.md" "$(grep -E '^[0-7]{6} ' "$tmp/mt" | awk '{print $4}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
chk "message de conflit" "CONFLICT (content): Merge conflict in docs/EXPLOITATION.md" "$(grep -F 'CONFLICT (content): Merge conflict in docs/EXPLOITATION.md' "$tmp/mt")"
chk "blob base (stage 1)" "97b6d1c39e6ce755326003fb862c73c861ba27e1" "$(sed -n '2p' "$tmp/mt" | awk '{print $2}')"
chk "blob lot (stage 2)"  "4d7cd67d926db61acaa01b7198c6881ca9e53f94" "$(sed -n '3p' "$tmp/mt" | awk '{print $2}')"
chk "blob main (stage 3)" "ee349bdcca935cbd7df0e8bdda7afa50b54ab423" "$(sed -n '4p' "$tmp/mt" | awk '{print $2}')"
MT=$(head -1 "$tmp/mt")
git diff --name-only "$MT" "$C" > "$tmp/dmt"
chk "arbre candidat vs auto-fusion : un seul fichier" "docs/EXPLOITATION.md" "$(cat "$tmp/dmt")"

echo "== 5. union du document =="
git diff "$B" "$A" -- docs/EXPLOITATION.md | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' | sort -u > "$tmp/la"
git diff "$B" "$M" -- docs/EXPLOITATION.md | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' | sort -u > "$tmp/lm"
git show "$C:docs/EXPLOITATION.md" > "$tmp/doc"
chk "lignes ajoutées par le lot absentes du candidat" "0" "$(grep -FxvF -f "$tmp/doc" "$tmp/la" | wc -l)"
chk "lignes ajoutées par main absentes du candidat"   "0" "$(grep -FxvF -f "$tmp/doc" "$tmp/lm" | wc -l)"

echo "== 6. propreté =="
if git diff --check "$M" "$C" && git diff --check "$A" "$C"; then ok "diff --check propre (vs main et vs tête gelée)"; else ko "diff --check"; fi

echo
if [ "$fail" -eq 0 ]; then echo "RESULTAT : tous les contrôles PASS"; else echo "RESULTAT : au moins un contrôle FAIL"; fi
exit "$fail"
