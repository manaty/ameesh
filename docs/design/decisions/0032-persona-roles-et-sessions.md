---
type: Decision
title: "Persona, rôles et sessions : qui agit, à quel titre, dans quelle exécution"
description: "Persona = qui (personnalité propre, liée à un seul canon : mémoire, rôles, droits, outils et skills, sessions sauvegardées hors de l'appareil, responsable humain avec suppléants et supérieurs) ; rôle = fonction dans un périmètre ; session = exécution. Pas d'ouvrier générique ; mémoire par persona prioritaire en v2 ; les agents v1 vivent jusqu'à la fin de leur lot ; un mesh par organisation."
status: stable
tags: [persona, role, session, memoire, v2]
decided_by: human:smichea
decision_date: 2026-10-09
attestation: "réponses du propriétaire dans sa conversation avec mesh-design (2026-10-09) ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T05:30:00+02:00" }
sources:
  - { resource: "0029-persona-et-session.md", title: "Décision 0029 (persona et session)" }
  - { resource: "0030-pas-de-travail-sans-reveil-possible.md", title: "Décision 0030 (réveil, délégation)" }
  - { resource: "../etudes/persona-et-harnais.md", title: "Étude persona et harnais" }
---

# Contexte

La décision [0029](0029-persona-et-session.md) sépare la persona de ses
sessions, mais la v1 ne l'applique pas : une ligne du registre, l'« agent »,
porte à la fois l'identité (responsable, capacités) et l'exécution (harnais,
session, bail). Il n'y a donc qu'une session par agent. Les noms hérités de la v0
(`deepseekN`) mêlent le harnais et un numéro, et la mémoire n'est que le
transcript de la session. La notion de **rôle** manque aussi : un agent
permanent qui tient une fonction stable pour une équipe, par exemple le
vérificateur des migrations de base de données, n'a aujourd'hui qu'un nom.

# Décision

1. **Trois notions distinctes.**
   - **Persona** : *qui* agit.
   - **Rôle** : une *fonction dans un périmètre* (équipe, projet, service),
     tenue par une persona ou par un humain.
   - **Session** : une *exécution* de la persona (harnais, modèle, compte, hôte,
     bail), éphémère, conformément à [0029](0029-persona-et-session.md).

   Dans le langage courant, « agent » désigne une persona qui n'est pas le
   double d'un humain.
2. **Une persona a une personnalité propre, liée à un seul canon.** À un
   instant donné, elle est définie par :
   - sa **mémoire** ;
   - ses **rôles** ;
   - ses **droits** ;
   - ses **outils et skills** ;
   - ses **sessions** (ses exécutions, en cours et passées) ;
   - son **humain responsable**, avec ses **suppléants** et ses **supérieurs**.

   Les sessions font partie de la persona : elles sont **sauvegardées hors de
   l'appareil qui les exécute**, sur un serveur du mesh ou, entre appareils,
   via Nexlink. La perte d'un appareil ne fait pas perdre une persona.
3. **Création de personas.** On pourra en créer de nouvelles **par clonage**
   ou **par assemblage**. Les modalités sont hors du périmètre de la v2.
4. **Pas d'ouvrier générique.** Un ouvrier est spécialisé dans le domaine où
   il travaille. Sa création par clonage ou par apprentissage relève de la v3.
5. **La mémoire par persona est prioritaire en v2.** C'est le dépôt de mémoire
   de [0029](0029-persona-et-session.md) (lots P1 et P4 de l'étude). C'est
   aussi la condition pour qu'une persona change d'hôte, par exemple vers un
   serveur, sans perdre ce qu'elle sait.
6. **Les agents v1 vivent jusqu'à la fin de leur lot.** Aucun n'est renommé.
   Les nouveaux sont créés comme personas, avec un nom de rôle.
7. **Un mesh par organisation.** Une organisation a son mesh : sa base, ses
   exécuteurs, ses personas. Un appareil peut participer aux meshes de
   plusieurs organisations, avec un exécuteur par mesh. Plusieurs canons pour
   une même organisation ne sont pas un besoin de la v2.

# À préciser

- **Le modèle des rôles** :
  - l'adressage par rôle ;
  - le titulaire et les suppléants ;
  - les absences ;
  - un rôle tenu par un humain ou par un agent ;
  - le vocabulaire commun et propre à chaque canon.

  Il sera instruit dans l'étude v2.
- **Les supérieurs du responsable** : la chaîne d'escalade, et ce qu'un
  supérieur peut faire à la place d'un responsable absent.

# Conséquences

- La fiche du canon `Agent` devient `Persona` (lot P1, déjà prévu par 0029),
  avec les rôles, les outils et skills, et le responsable avec ses suppléants
  et supérieurs. La fiche `Agent` reste lisible pendant la transition.
- Le registre sépare les personas de leurs sessions : plusieurs sessions par
  persona, et un bail par session.
- L'étude [ameesh v2](../etudes/ameesh-v2.md) prend ces trois notions pour
  base.
