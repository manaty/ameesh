# #1112 — composition finale du workflow et provenance #940

Relecture auditeur du 14/10/2026, harnais réel **Codex**. Demande bureau de 00:53. Tête exacte `afa32932e72c459bf1d23314912647b23b306b73`, base du paquet `b85777c1e3e757b9ba0caa8cef49b0db1558b251`. Périmètre : composition `.github/workflows/tests.yml` (#880 + #940 + #947) et qualification du R2 #940 ; aucun avis nouveau sur le delta budgets ou sur les autres lots du manifeste.

**GO de composition du workflow — 0 B / 0 P2 / 0 N nouveau.** Le N documentaire P3 de l'avis précédent reste suivi dans #824. Cet avis ne vaut pas feu de fusion ni validation du paquet complet.

## Workflow exact

- Blob final : `fbf1e8d0cd7dec6d945c19e23a92b35c88d3dd35`, identique à l'empreinte du manifeste `reviews/2026-10-14-1112-integration-manifest.md`.
- Par rapport au candidat `e368725c` déjà relu (rapport `4f1e77873408b1aafbe0dd22b9256e35dae472ed`), le workflow ajoute exactement les deux gardes de #940 : sortie explicite `|| exit 1` après le repli du contrôle de version et après celui de psql, avec leurs commentaires. Aucun changement de `needs`, de condition `if`, de commande de suite ou de publication du flux dans cette composition.
- Comparaison directe : le corps entier de l'étape finale « bilan des parts » est **identique** à celui de #940 `913eab23ee7744f30b3a7b8441cb359f165fd7cc` (blob workflow d'origine `fc461d87a…`). La composition conserve donc les deux gardes déjà relues sans réécriture logique. Les changements #880/#947 restent le plafond psql de 45 minutes et les commentaires des trois parts de PR ; le plan conserve quatre parts psycopg et six parts psql hors PR.
- Huit témoins isolés sur le **script final exact**, expressions GitHub substituées, exécuté sous `bash -e` : dispatch complet vert → 0 ; psql failure/cancelled/skipped avec parts non vides → 1 ; psycopg failure → 1 ; macOS cancelled → 1 ; version skipped sur PR → 1 ; cas légitime psql skipped avec matrice vide → 0. Aucun accès base, aucune suite rejouée, aucun workflow déclenché par le relecteur.
- `git diff --check b85777c1e afa32932e` vert.

## R2 #940 : famille Claude vérifiée directement

Journal réel `/home/smichea/.claude/projects/-home-smichea-Work/11783f10-5492-408f-988b-8c1035bec79b.jsonl`, ligne **2874**, `2026-10-13T16:43:43.205Z`, `message.model = claude-opus-5-5`, UUID `31e8899d-12eb-43dd-bdf7-df80600b37f5`. L'appel `Bash` pose `ameesh work milestone 940 verdict ok --sha 913eab23e --actor bureau` avec la note R2 et envoie le verdict au pilote. La ligne 2885, à 16:43:57Z, confirme le GO #940 sous le même modèle.

La qualification est donc **R1 cx2 Codex + ancien R2 bureau Claude**, hors auteur qa-01 DeepSeek. La session bureau actuelle sous Codex ne remplace pas cet ancien avis et ne l'invalide pas. Le transport de ces avis aux gardes finales repose sur la comparaison du corps ci-dessus ; aucune nouvelle famille ne découle du nom historique d'un agent.

## Suite du paquet

Le manifeste et le pilote prévoient une seule QA **complète** par `workflow_dispatch` sur l'arbre final : psycopg quatre parts, psql six parts, documentation/contrôles et plateforme. Le plan PR de trois parts psql pilotes ne suffit pas pour #1112 (§12 du canon). Toutes les parts attendues doivent être réellement exécutées et vertes ; une annulation, un saut indu ou un agrégat vert seul ne valide pas le paquet.

Le pilote a corrigé à 00:55 le classement de #878 : test seul de concurrence, hors SQL/sécurité/prod, avis cx2 Codex suffisant, blob `4dd28da5` conservé. Ce reclassement est un fait annoncé et noté par mesh-design ; il ne constitue pas une relecture #878 par cet avis. Restent les autres qualifications du manifeste, l'avis indépendant sur les blobs budgets publiés, le feu pilote, le contrôle de la tête cible et les preuves distinctes de déploiement. Aucun élargissement du gel ni geste de production par l'auditeur.
