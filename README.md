# #1114 — dossier de delta final (composition #917 + #1114) — 15/10/2026

Mandat : pilote mesh-design, 15/10/2026 00:03Z (mail #44155). Classe
**sensible/prod** confirmée dans la portée annoncée (appel `exploitation.py`
modifié). Deux avis indépendants **Codex + Claude** sur la tête finale de la
composition 917+1114, **sans CI** : R1 = `auditeur` (Codex), R2 = `ameesh-dev2`
(Claude) après ses lots #1104/#414. Les preuves auteur 117+117 ne sont pas à
rejouer. Aucun GO de fusion ne se déduit de ce dossier ; l'admission au
prochain paquet se fait après les deux avis. Aucun élargissement de #1342
(paquet 1, PR #1050).

## Tête gelée et bases

| Objet | Référence | Arbre |
|---|---|---|
| Tête gelée de la composition | `d5276bafc219ed8e9e2d46248c3472a23afd271e` (parents `8c713b3c` + `3e047c8d`) | `793700a9ad7b837afc4505d1c7efb0c63914d50c` |
| Base de delta (paquet 1 / futur main) | `8c713b3c129b84517e521ac8643997fb81fae907` | `6a9ece943ab1048048c669c7d78484951045b2fd` |
| #917 (PR #708, `lot/917-stale-lot-equipe`) | `182492c6398776b6ee8976d2d2f3830230a19fa8` | `13d32b2e2da3e3c146b05fd6b1bdd5377d67effe` |
| #1114 (contient #917) | `3e047c8d00c41590c2ddba37765d830e63860623` | `ab0611aea87962ad8adff67683edc4adb2319cf2` |

Union de contrôle de la préparation auteur : `eae2173e` — même arbre
`793700a9`, message `sim:` ; la tête gelée `d5276baf` est le même contenu avec
un message de gel. Aucune reprise des preuves.

Récupération : `git fetch origin '+refs/atelier/1114/*:refs/atelier/1114/*'`

## Delta à lire

| Fichier | Contenu | Taille |
|---|---|---|
| `patch-composition-vs-base.patch` | composition vs base paquet 1 (les deux lots) | 10 fichiers, +141/-23 |
| `patch-1114-lot.patch` | delta propre de #1114 (`3e047c8d^..3e047c8d`) | 6 fichiers, +105/-16 |
| `patch-917-lot.patch` | delta de #917 vs main `c4fa14a2` | 7 fichiers, +36/-7 |

#1114 : `alert_orchestrators` (déclarés + rôle canon, `history=False`) remplace
`orchestrators` sur les chemins d'alerte (`exploitation.py` ×5,
`sous_utilisation.alerts`, `undeliverable.known_orchestrators`,
`alert_briefs._of_teams`) ; l'historique d'attribution 30 j ne confère plus le
rôle de destinataire. Témoins neufs : `test_stale_lot_sans_role_par_historique`
(l26) et `test_destinataires_d_alerte_sans_role_par_historique` (l94 : assigneur
récent `roles=[implementation]` exclu, hub sans équipe admis par déclaration
seulement, rôle canon admis).

#917 (7 fichiers, +36/-7) a été relu sur `dd5d6a67886eb3aa5ad2573a7c9d14cb207003b0` :
R1 `mty-amh-impl-06` GO, avis Claude `bureau` GO 0 B/0 N, avis Codex `ameesh-cx2`
GO 0 B/0 P2 (pilote 13/10 15:31). Depuis, la production est constante ; seule
évolution : `tests/test_l132_alert_briefs.py` +3/-2 (témoin aligné sur L713,
commit `182492c6`).

## Résolution de l'union (seul conflit)

`src/ameesh/sous_utilisation.py` : le paquet 1 apporte #1028
`multipliers=pace_multipliers(cfg)` à l'appel `plan_underused` ; #1114 change
`orchestrators` en `alert_orchestrators`. **Les deux sont conservés.**

Lignes de la tête gelée :

    out = plan_underused(db, listing, now, ..., multipliers=pace_multipliers(cfg))
    orchestras = alert_orchestrators(cfg, db, listing, declared=orchestrators_declared,
                                     canons=canons)

Contrôles : `git merge-tree 8c713b3c 3e047c8d` rc=1, conflit unique
`src/ameesh/sous_utilisation.py` ; l'arbre résolu ne diffère de l'arbre de
conflit (`a2fb60da`) que par ce fichier, sans marqueur ; `resolution-vs-paquet.diff`
est exactement le delta #1114 du fichier, `resolution-vs-1114.diff` exactement le
delta #1028 ; aucun marqueur dans l'arbre entier.

## Preuves auteur (pas de rejeu)

`preuves/banque-117-psql-et-psycopg.log` : 117 OK psql (435 s) + 117 OK psycopg
(78 s) sur l'arbre exact `793700a9` — modules `test_l26_exploitation`,
`test_l94_underuse`, `test_undeliverable`, `test_l132_alert_briefs`.

## Portée

Publication atelier seule : aucune CI, aucun push de branche de lot, aucune PR,
aucune fusion. Le rebasage réel, la CI exacte (paquet) et le feu restent à faire
à l'admission au prochain paquet, après les deux avis. Vérification après
déploiement prévue par `mty-nxl-impl-01` et `deepseek1` (signaleurs).
