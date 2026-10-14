# #1244 — avis DeepSeek indépendant du fragment Codex (union #1004 `6be4710d`), 14 octobre 2026

Avis demandé par mesh-design (mails 36948/14:56, mandat orchestrateur 36972/14:53) : lecture
bornée du fragment Codex `329c55f32` **et de ses interactions dans l'union**, par un harnais
DeepSeek d'un autre chantier (Nexlink) et d'une session distincte de l'intégrateur dev5 et de
l'auteur auditeur. Ne porte pas d'avis global sur l'union : la R1 Codex du pilote couvre le
fragment DeepSeek #1314, la R2 Claude native (#1098) reste due.

## Références gelées, vérifiées à la source (2026-10-14 ~15:00Z)

| Objet | Valeur |
|---|---|
| PR manaty/ameesh#1004 | OPEN, MERGEABLE, non fusionnée (`mergedAt` nul), tête `6be4710d6f573f5ef0e7def057042a3394a9fe80` |
| `refs/heads/lot/1314-carte-1211`, `refs/atelier/1314/tete-6be4710d6` | `6be4710d6f573f5ef0e7def057042a3394a9fe80` |
| base | `046a106a5c8d2a81a5bf717401f8a68e8dd2d4ac` (main) |
| fragment Codex | `329c55f32d178e4411430fad2c0c2a02e22096ec`, arbre `63b1d8ea38f31129af4e78945144ab3f0dd4a554`, `refs/atelier/1244/docs-range-329c55f32` |
| cherry-pick | `fb64c47384b2871ed6c5e07bbe32deb05261c118`, arbre identique `63b1d8ea` (`git diff 329c55f32 fb64c4738` vide), auteur `smichea` conservé, ligne « cherry picked from commit 329c55f32 » présente |
| ranges de lecture | Codex seul `046a106a..fb64c473` (2 fichiers, +120/−9) ; DeepSeek seul `fb64c473..6be4710d` (4 fichiers, +227/−8) |

## Méthode

- lecture à la source du diff Codex seul, puis de `scripts/test-plan.py` et du harnais dans l'union ;
- **6 témoins ciblés** ajoutés par le fragment rejoués localement (dépôt jetable du harnais,
  aucun fichier de l'arbre modifié) : `tests.test_l161f_plan.DocsLaneTest.{test_docs_pr_ignores_ci_changes_already_in_its_base,
  test_docs_pr_with_old_base_map_keeps_database_readers,
  test_docs_pr_keeps_a_database_reader_changed_on_its_base,
  test_docs_pr_keeps_unmeasured_base_code_in_the_full_suite,
  test_mixed_pr_with_old_base_map_does_not_take_docs_lane,
  test_missing_uncertainty_diagnostic_keeps_the_full_suite}` → **6 OK, 1,25 s**. Aucune suite
  complète, aucun dispatch CI, aucune fusion.
- **deux sondes adverses indépendantes** (mêmes fixtures `_Repo`, sous `$TMPDIR`) :
  - retrait de `deploy/install.sh` (non-documentation) sur la base depuis la carte →
    `sure=False`, `docs_only=False`, doute `file removed: deploy/install.sh` conservé ;
  - `docs/conf.py` ajouté sur la base → doute `docs/conf.py: Python outside src/ameesh/ is not
    measured` conservé, voie doc refusée.

## Constats

1. **Fail-closed structurel.** `docs_only` ne peut être mis à vrai que par `nature()`, sur le
   range `base→tête` ; le fragment ne touche ni `nature()`, ni `outputs()`, ni les conditions
   `if:` des jobs. `classify`/`make_plan` ne peuvent que le remettre à faux. Le fragment ne peut
   donc pas ouvrir la voie documentation à un range qui n'est pas déjà documentation seule.
2. **Un seul écart assumé, borné.** Dans la voie doc, seul est écarté un doute dont le libellé
   exact est `test infrastructure changed: <chemin>` pour un `<chemin>` de `baseline.foundation` :
   socle déjà changé par la cible depuis la carte, et non changé par la branche. Tout autre doute
   hérité est recopié (`impact.doubt`) ; `untested`/`unknown_blocks` hérités donnent le doute
   générique « documentation base contains unmeasured code » ; une suppression donne son doute ;
   une baseline illisible lève → `map not usable` ; un lecteur de base P0, ou un module des
   tables de la carte, alimente `doc_readers`/`db_readers` → suite complète (le job « lecteurs de
   la doc » n'a pas de Postgres). Aucun chemin trouvé qui élargisse le périmètre sauté.
3. **Lecteurs hérités réellement fusionnés.** `impact.p0.update(baseline.p0)` +
   `impact.p1.setdefault(...)` font entrer les modules impactés par le range carte→base dans
   `classes`/`doc_readers` **avant** la décision de voie ; le doute de lecteur DB s'applique donc
   aussi aux lecteurs hérités (témoin 3).
4. **Interaction DeepSeek neutre pour le fragment.** Le `judge` de la baseline est appelé sans
   `own`/`own_base` : les défauts `None` rendent la sémantique pré-fragment (exemption
   `pyproject.toml` jugée carte→base). L'appel principal de la voie doc passe
   `own = chemins du range propre` ; un range documentation ne contient aucun chemin de socle,
   la branche `is_foundation` n'y est pas atteinte. `own_changes`/`is_merge_commit` ne
   s'appliquent qu'au chemin non-doc. Le delta DeepSeek du fichier de tests n'ajoute que
   `InheritedFoundationTest` après la ligne 688 : les témoins Codex sont intacts.
5. **Manques de témoins, non bloquants** : `baseline.untested` n'a pas de témoin propre (même
   branche que `unknown_blocks`) ; la propagation `baseline.p0` (module de test changé sur la
   base) et le doute de suppression hérité ne sont pas témoignés — la sonde A couvre la
   suppression, le repli reste conservateur dans les deux cas.

## Observations non bloquantes

- **O1 — prémisse du socle hérité.** « La cible a validé son changement par une suite complète »
  est exact pour `main`, `release/**`, `integration/**` (`lot_selection` → suite complète hors
  PR). Elle est plus faible si la base d'une PR est une branche de lot (PR empilée), dont la CI
  peut n'avoir lancé que les impactés. Le delta propre de la PR doc reste documentation seule ;
  à rouvrir si des PR de documentation ciblent des branches de lot.
- **O2 — pas de garde `is_merge_commit` dans la voie doc.** `own_changes` refuse un `HEAD^1` qui
  n'est pas un commit de fusion, mais la voie doc utilise directement `base→tête`. `nature()`
  juge le même range : la cohérence interne du plan est tenue, mais sur un checkout non-fusion
  (hors `refs/pull/N/merge`) la portée serait le dernier commit, pas la PR. Comportement
  pré-fragment, bord non atteint par le workflow actuel.
- **O3 — libellé de repli.** Un repli de voie doc pour lecteurs DB affiche « full suite » alors
  que #1211 peut retomber sur impactés+contrat (`phase2_safe` vrai) ; les lecteurs DB P0/P1 sont
  alors bien sélectionnés — pas de trou de test constaté, message seulement imprécis.
  Pré-existant (#1211, base), hors fragment.
- **O4 — filtre par libellé exact.** Un renommage futur du doute `test infrastructure changed:`
  ne l'écarterait plus : le doute reste, donc repli sûr (fail-closed).
- **O5 — objets non fusionnés.** `baseline.textual` et `impact.removed` ne sont pas recopiés
  comme listes, mais tous leurs doutes le sont : sans effet sur la sûreté.

## Verdict

**GO** sur le fragment Codex `329c55f32` (arbre `63b1d8ea`) tel qu'intégré dans l'union
`6be4710d` (arbre `a978be48`), pour la part « avis DeepSeek indépendant » de la classe sécurité.
Aucun défaut bloquant relevé ; aucune réserve ne bloque la fusion. Portée strictement limitée à
ce fragment et à ses interactions : ne vaut pas avis global sur l'union, la R1 Codex du pilote
reste due sur le fragment DeepSeek #1314, la R2 Claude native (#1098) reste due. O1 et O3 sont à
suivre.

## Limites

Vérification sur les arbres gelés ci-dessus ; pas de rejeu de suite complète, pas de dispatch CI,
pas d'exécution du workflow réel, pas de revue du fragment DeepSeek #1314 au-delà de ses
interactions avec le fragment Codex. Code du fragment non modifié ; la sonde vit sous `$TMPDIR`.
