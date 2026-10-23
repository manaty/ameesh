# Paquet integration/paquet-h-20261021-0800 — composition (§12)

Base : `main` **`a76bbf676f56`** (fusion du paquet h-0600, PR manaty/ameesh#2269,
21/10 06:15 UTC). Branche `integration/paquet-h-20261021-0800`, cible `main`,
lot porteur #2967. Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, pour le pilote mesh-design (courriers #122488 du
21/10 06:08 et 06:49, après fusion de h-0600) ; aucun autre paquet composé en
parallèle.

## Admissions (3 unités, 3 lots portés)

| lot | source | classe | P | avis | auteurs | objet |
|---|---|---|---|---|---|---|
| #2961 | `0bc1b4c715ba` (gel 15255), remise `3946a8dfea5a` | sensitive | 1 | ameesh-cx2 (codex, #15260) + auditeur (claude, #15264) ; **delta** sur `3946a8df` : auditeur OK (claude, #15300), ameesh-cx2 attendu ; le delta propre du paquet (4 fichiers de la résolution) est désigné dev2 + cx2 au gel #15312 | mty-amh-impl-05 (deepseek) | porte du composeur : famille de la référence lue au gel |
| #2906 | `1ba6c1d2205b` (gel 15173) | light | 2 | ameesh-dev2 (claude, #15221) | ameesh-cx3 (codex) | panier « #382 » N1 : sockets device vivants, arrêts workstation |
| #2944 | `4a476751d3f7` (gel 15140) | light | 2 | ameesh-cx3 (codex, #15250) + mty-amh-impl-01 (deepseek, #15142) | auditeur (claude) | EXPLOITATION.md : rotation Codex lue au journal de session |

Avis et classes lus par `scripts/paquet-candidats.py` sur la tête composée avec
#2961 (`a76bbf67` + #2961), porte §2 : `ok` sur le gel exact, aucun avis d'un
auteur, familles distinctes.

**#2892, reporté (décision pilote 06:49).** Sur `main` seule, le composeur
refuse #2892 (`verdicts-missing`) à cause du défaut que corrige #2961 : la
famille de l'auteur de référence est lue au registre courant (mesh-design y est
lu claude), l'avis dev1 (claude, #15222) est donc exclu comme même famille et
il ne reste que qa-01 (deepseek, #14832). Après composition de #2961, la liste
recalculée depuis l'arbre du paquet sort #2892 **prêt** (author_family=codex,
avis qa-01 deepseek + dev1 claude, merge-tree propre) — mais la porte qui admet
un paquet est celle de `main`, appliquée par `packet_automerge` (canon §7 : le
mécanique est du code), pas celle de l'arbre du paquet. Retiré du véhicule sur
décision du pilote ; il entre dans le premier paquet composé après #2961 sur
`main` (h-0900 si h-0800 fusionne d'ici là), avant la coupe de 11:00.

## Composition (3 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2961 | `0bc1b4c715ba` | `d7c2360ae` | fusion de la tête gelée (provenance connue), conflits résolus à l'arbre exact de la remise `3946a8df` (identique à `232770e2`, vérifié par l'auditeur) |
| 2 | #2906 | `1ba6c1d2205b` | `50e7c0ec6` | fusion de la tête gelée, conflit `tests/test_l93_device_state.py` résolu à l'arbre de la résolution h-0600 `18b1cd25` |
| 3 | #2944 | `4a476751d3f7` | `9f08c1669` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

| unité | arbre de résolution | provenance | conflits d'origine |
|---|---|---|---|
| #2961 | `3946a8dfea5a` (remise sur main, `refs/ameesh/resolutions/2961/main-a76bbf67`) | fusion de la tête gelée `0bc1b4c7` (provenance connue) ; le commit de composition porte la résolution et le trailer | `src/ameesh/merge_queue.py`, `src/ameesh/review_authors.py`, `src/ameesh/review_designation_activation.py`, `src/ameesh/train_history.py` (unions de paramètres ; arbre identique à `232770e2`) |
| #2906 | `18b1cd25025b` (résolution h-0600) | fusion de la tête gelée `1ba6c1d2` ; conflit du fichier de test résolu à l'arbre de la résolution | `tests/test_l93_device_state.py` |

Les commits de résolution `3946a8df` et `18b1cd25` ont été créés par
`work resolve --done` sans reçu de provenance exact (#2942) : les fusionner
comme source rendrait la plage gelée illisible (auteur inconnu) et la porte du
véhicule refuserait. Les têtes gelées sont donc fusionnées directement — leur
provenance est prouvée — et la résolution est portée par le commit de
composition (trailer `ameesh-work`). L'arbre final est identique, fichier pour
fichier, à celui des deux résolutions (vérifié par `git diff`). Aucune
adaptation de code produit.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2892 | `81e203002aad` | prêt au calcul sur l'arbre composé avec #2961 ; refusé sur `main` par le défaut de registre courant que corrige #2961 — décision pilote 06:49 : entre dans le premier paquet composé après #2961 sur `main`, avant la coupe de 11:00 |
| #2942 | `0fdbb3b84166` | prêt au calcul, hors de l'ordre de composition du pilote (#122488) : laissé au prochain paquet ; pilote informé |
| #2903 | `75fb3826a775` | prêt après la rédaction du manifeste (gel 06:38, P3 « santé courrier ») ; hors de l'ordre du pilote, laissé au prochain paquet |
| #2963 | `b36fd4da44c0` | prêt, hors de l'ordre de ce véhicule ; décision pilote 06:49 : va au prochain paquet |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` (un avis deepseek, deux familles requises) ; conflit sur `src/ameesh/exploitation.py` |
| #2614 | `blocked-verdict` |
| #2771 | `verdicts-missing` (un avis deepseek, deux familles requises) |
| #2895 | `blocked-verdict` ; conflit sur `docs/design/etudes/index.md` |
| #2916 | `conflict` sur `src/ameesh/exploitation.py`, `src/ameesh/slo.py` |
| #2925 | porté par h-0600 (fusionné), clôture en cours |
| #2932 | porté par h-0600 (fusionné), clôture en cours |
| #2935 | porté par h-0600 (fusionné), clôture en cours |
| #2950 | porté par h-0600 (fusionné), clôture en cours |
| #2951 | porté par h-0600 (fusionné), clôture en cours |
| #2955 | `blocked-verdict` |
| #2961 | admis par sa tête fusionnée, résolution `3946a8df` portée par le commit de composition |
| #2962 | `verdicts-missing` |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py --repo . --target main` sur la
  tête composée avec #2961 : prêts **[2892, 2903, 2942, 2944, 2963]**, en
  attente 14, erreurs 1 (#2914 : gel antérieur `66a06cf09d5c` absent du dépôt,
  lot hors véhicule). Aucun lot prêt omis : #2944 est composé, #2892, #2903,
  #2942 et #2963 sont écartés avec motif ci-dessus.
- Contenu : sans #2892 ; à recalculer au commit du manifeste.
- Tests ciblés sur l'arbre composé (local, sans CI) : `test_2961_reference_family_freeze`
  **10 OK** (187 s) sur base de test privée ; `test_2808_author_at_freeze`,
  `test_2447_review_designation_activation`, `test_2931_packet_target`,
  `test_l160e_merge_queue`, `test_l93_device_state` en cours (banc partagé
  bloqué, base privée utilisée).
- Avis delta #2961 : auditeur (claude) OK #15300 sur `3946a8df` ; le delta
  propre du paquet est désigné dev2 (claude) + cx2 (codex) au gel #15312. La
  fusion attend ces avis et la CI verte ; si l'un manque au vert, #2961 seul
  est retiré (consigne pilote 06:49).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` : à compléter.
- CI complète : à demander sur la tête finale (une seule suite).
- Prévalidation indisponible (carte 86 h) : écart noté, non bloquant.
