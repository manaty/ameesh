# #1075 / PR #827 — R1 finale, auditeur Codex

13/10/2026. Harnais réellement exécuté : Codex.

**GO contenu final, 0 B / 0 N nouveau**, sur
`2fb4913a7bf04470168553e5a380225b6a90f84c`.
Lecture des quatre fichiers du diff depuis main `b85777c1` et du chemin
réel de choix/garde du compte. Cet avis ne reprend pas une prélecture comme
verdict : le gel a été relu ici.

`Profile.pace_multiplier` vaut 1.0 par défaut. Le parseur n'invente aucune
valeur selon le harnais, le nom du compte ou son dossier ; le réglage est
propre à chaque profil. La validation refuse booléens, valeurs illisibles,
non finies, nulles ou négatives. Un réglage absent reste nominal.

`Gauge.pace_cap(now, m)` calcule `min(0.90, elapsed(now) * m + 0.10)`.
La valeur publiée, la part écoulée et le reset ne sont pas modifiés.
`accounts.limits` applique le multiplicateur de ce profil au plafond de
rythme, puis retire la réserve et conserve les comparaisons avec les plafonds
fixes et d'overage. Le plafond court explicite garde son comportement existant.
Le garde zéro et les contrôles d'overage ne changent pas.

Pour une fenêtre écoulée à 10 %, sans plafond fixe plus contraignant :

| Profil | Critique | Non critique, réserve de 10 points |
| --- | --- | --- |
| m=1 | 20 % | 10 % |
| m=2 | 30 % | 20 % |

Le chemin `account_turn.choose` → `accounts.choose` → `evaluate` utilise
ces limites. La garde du runner évite ensuite une deuxième vérification
nominale (`book.over(..., pace=False)`) quand des profils sont déclarés :
le multiplicateur ne se perd pas avant le départ du tour. Les comptes sans
profil déclaré gardent leur comportement nominal. Les explications et les
caps du rapport accounts lisent également le multiplicateur ; les ratios du
choix de compte utilisent les limites calculées.

Les dix témoins couvrent défaut, profils distincts, invalides, formule,
marge/plafond, réserve des non-critiques, plafonds fixes, retrait à 1 et
évaluation de comptes aux mêmes jauges. Horloge et jauges sont fournies ; le
profil secondaire utilise un dossier temporaire créé par le test et ne
dépend pas d'identifiants réels. Les SHA256 du gel correspondent au paquet :
accounts `d5fd91bc…`, cost `329fce92…`, test `055b61e2…`.

Validation du relecteur : lecture, recherche des appels et `git diff --check`
propre. Aucune suite rejouée (canon §3). Les dix tests psql/psycopg et les
259 tests ciblés annoncés sont les preuves de l'auteur. La CI exacte
`38473067335` est en file à la consultation ; aucun vert n'est attesté ici.

Le couplage déjà signalé avec #777 / PR #612 reste à intégrer : held_cap et
free_at devront recevoir le même `profile.pace_multiplier` lors de leur
intégration, comme convenu avec l'auteur et le pilote. Aucun verdict sur
ce code non fusionné n'est donné par cet avis.

Fusion : tête inchangée, toutes les parts CI success sans annulation et feu
conditionnel du pilote (mesh-design, 23:51). L'application effective du rythme
doublé nécessite le déploiement du code et le réglage explicite **2 sur Codex
primaire seulement** par le bureau ; Codex secondaire et Claude restent à 1.
Cette revue n'applique ni configuration, ni voucher, ni déploiement.
