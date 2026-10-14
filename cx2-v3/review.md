# Transport et composition du paquet 1 v3 — cx2/Codex

Mandat mesh-design #41053, 14/10/2026 22:32:48 CEST. Tête examinée b689cb84db7a5fa2cf39088e21f070d364c3fc39, arbre 3e01f21a35e23ae53ada67574a8fbceda399bb59, base main2458ec053123d3f33644fef1e092fc84cc2773d9. Source v2 qualifiée :8c1f99ff00a5f7fd085fb4bfe726ab4593ed12e9/arbre9a36aa1b, note19056 et rapport1342-v2/review.md.

**GO Codex borné de transport/composition sur v3 : 0 nouveau B/P2.** Cette qualification repose sur les comparaisons v3 réalisées ce tour, pas sur un rattachement automatique de l'ancien GO. Elle conserve la portée des avis par apport ; elle ne constitue ni admission finale de la matrice, ni gel, ni feu CI/fusion, ni revue autonome du code #1339 déjà sur main.

Dix merges indépendamment reproduits par merge-tree : parents, arbres et chemins exacts ;10/10 patch-ids -U0 sources/v2/v3 identiques. Patch-id global ff9070b11774e8800ae29c04a7043ae15317386f constant. Arbre de l'union directe main2458+v2 = arbre v3 exact. Aucune résolution ou adaptation manuelle nouvelle.

44 chemins membres :43 blobs strictement identiques à v2 ; seul docs/EXPLOITATION.md change. Vérification aussi sur les étapes intermédiaires : hors doc, aucun blob membre ne change. Le delta entier v2→v3 a le même patch-id que main0532→main2458 ; il porte seulement docs/EXPLOITATION.md, scripts/test-plan.py et tests/test_l161f_plan.py (#1339). Ces deux derniers blobs sont exactement ceux de main2458. Le delta doc est identique à celui de main, relu : section sélection des blocs sans mesure, importers/certified/uncovered et motifs phase2 ; aucune interaction avec les sections513/706/859/1020. Zéro migration ajoutée par le transport.

Contextes qualifiés constants par identité de fichiers : alerts/ALERT_TYPES513 avec tri total main1060 ; link_message859 avec parent_lot main1314 ; fonctions distinctes cost513/1028 ; runner706/config ; watch1020 ; scripts1145 ; portes/checks/cache1293 ; SIDuuid4 de1196 et consommateur1037. L'ajout1339 concerne le planificateur des tests, pas ces fichiers ; son code n'est ni adapté ni relu au nom de ce mandat. #1294 est nommé couvert par1293@f7b72025, avis cx3:4240, mêmes trois fichiers ; aucun second merge1294.

Matrice par apport conservée à portée (table issue du rapport v2, appliquée au contenu v3 seulement après les contrôles ci-dessus ; les références « ici » de la ligne1196 désignent l'avis statique v2 désormais transporté, pas un constat post-fusion) :

| Lot | Auteur / apport original | Familles et portée à porter au manifeste |
|---|---|---|
| 490 | cx3/Codex original ; impl06/DeepSeek intégration constante | DeepSeek impl06 event7179, lecture 10 fichiers aca83eba ; Claude bureau jalon3584 tête b2f0a1b8 entière ; transport pilote12042 et qualification v1. Dix blobs exacts. Codex n'est pas une autre famille que l'auteur original. |
| 513 | r1 dev3/Claude ; r2/r3 impl02/DeepSeek | Source primaire auteur event18960 du14/10 20:0xZ : initial Claude couvert Codex2476/2483 + DeepSeek2431 ; corrections DeepSeek couvertes Codex2476/2483 + Claude2508/3569 (entier6d5763d5c). NO-GO1954/2376/2392 historiques corrigés, pas comptés comme GO. Constance a3aa8989/6d576 vers b3e910 documentée event18899 et lineage v1 ; composition constante v2. Aucun avis Claude contre initial Claude n'est compté indépendant. |
| 706 | auditeur/Codex (source5514) | DeepSeek impl01 jalon4299 exact4f0bb179 ; pilote17688/17807. Source et composition constantes. |
| 859 | impl01/DeepSeek à l'écriture | Cost turns du13/10 10:24–11:33Z (7 tours deepseek-flash, IDs6045..6166) recoupés avec events6063/6100 : auteur réel, pas registre actuel. Codex auditeur2253 + Claude bureau2320 suraffc65bd, transport pilote17156 versc3be406a ; garde et composition parent_lot constants. |
| 1020 | impl03/DeepSeek à l'écriture | Cost turns du13/10 18:58–19:43Z (7 tours deepseek-flash, IDs7075..7181) recoupés event9036. DeepSeek indépendant impl07 :3224 puis4237 exact2241af9a, repli18855a explicitement accordé pilote9099/9205 (13/10, sensible hors prod). Exception historique documentée, pas autre famille inventée. Avis Codex actuel sur composition constante seulement ; nouvelle relecture globale non revendiquée. |
| 1028 | impl04/DeepSeek (11635) | Codex cx2 :3481 sur22c6 puis4306 transport04e065 ; pilote17823/18304. Composition cost513 constante. |
| 1145 | dev5/DeepSeek (14448) | Codex cx1 :3988 surba95c776, rebase trap/nettoyer constant vérifié lineage v1 et maintien17822 versd118afd. Deux scripts exacts. |
| 1279 | impl04/DeepSeek (15014) | Codex codex2 :4056 sur9e735c59, blob51aa750e exact24b1e4aa, source16958. N/P2 suivi1281 préservé. |
| 1293 | dev5/DeepSeek (16950) | Codex cx3 :4240 exactf7b72025, trois fichiers, couvre1294, pilote17022. YAML/portes constants depuis v1. |
| 1196 | impl06/DeepSeek à l'écriture | Cost turns du14/10 02:29–02:38Z (3 tours deepseek-flash, IDs8377/8382/8411) recoupés event12639. GO statique indépendant cx2/Codex sur sourcec3e3 et composition8c1f ici. Classe légère, post-fusion à constater après geste réel. |


Pour1020, l'exception historique DeepSeek indépendant est sourcée aux décisions pilote9099/9205 du13/10 ; cet avis ne fabrique pas une famille différente ni une nouvelle exception actuelle. Le pilote garde l'admission de cette preuve selon le canon en vigueur. Pour513, les avis indépendants sont comptés PAR APPORT : initialClaude -> Codex+DeepSeek ; correctionsDeepSeek -> Codex+Claude, source primaire auteur18960. Pour490, Codex original n'est pas compté comme relecteur indépendant de lui-même. Les familles réelles des auteurs859/1020/1196 restent appuyées sur les tours historiques recoupés v2, pas sur leur nom ou registre courant.

Résiduels inchangés : diff --check rc2 exactement comme v2, deux N/P3 hérités tool_cgroups.py:56 EOF vide et test_1075_pace_multiplier.py:122 espace terminal ; N/P2 léger1279 suivi1281 préservé. Aucun nouveau test/suite/SQL/CI ni compilation nécessaire sur les blobs constants ; les sondes v1/v2 restent attribuées à leurs arbres d'origine.

#388 reste exclu, sans verdict supplémentaire. #532/#1144 restent hors candidat. #919 n'est pas membre : avis borné QA01#41010 sur son candidat local8d374e99 reçu séparément, pas un avis SQL général, aucune incidence sur cette matrice.

Aucun checkout/fetch/push/CI/fusion/déploiement ou geste sur refs. Candidat local non gelé. Coordination bureau38076/1341, admission finale, gel/tête exacte, CI complète unique et toutes parts attendues, fraîcheur cible, feu et staging restent côté pilote ; aucune SORTIE générale déduite de1339 fusionné et aucune tête future couverte.

Preuves indépendantes :verification.json, documentation-1339.diff, source-v2-review.md, author-family-evidence-v2.json, source-v3-manifest.json, sha256.json. La preuve de recomposition fournie par le pilote est conservée séparément pour comparaison.
