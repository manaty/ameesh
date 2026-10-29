# SPDX-License-Identifier: AGPL-3.0-only
"""#3218 : le reçu d'étapes d'un tour de courrier (`turn.stages`).

Un consommateur qui envoie un message à un agent au repos mesure l'attente
jusqu'au début du tour sans voir où elle passe. L'exécuteur date chaque
jalon du chemin et publie, une fois par tour de courrier et à la fin du flux
du harnais, un événement `turn.stages` (`ameesh events`, `GET /api/v1/events`) :

  mail_ts            le plus ancien message servi est écrit (horloge de la base)
  seen_ts            l'exécuteur le lit pour la première fois (horloge de l'hôte,
                     comme tous les jalons suivants)
  due_ts             le tour est dû (fenêtre de regroupement finie, ou aucune)
  admitted_ts        le tour est admis : compte, fenêtre courte, `begin_turn`
  spawned_ts         le harnais est lancé (ou la consigne acceptée par un résident)
  delivered_ts       les messages sont soldés (absent pour un message humain,
                     soldé en fin de tour, L77)
  first_output_ts    première ligne du harnais (absent pour un flux muet)

et la durée de chaque étape, en secondes :

  busy               l'agent finissait un tour précédent
  held               le tour dû attendait une garde d'admission (budget, fenêtre
                     courte du compte : `held_reasons`)
  wake               du message à sa première lecture, hors des deux précédentes
                     (NOTIFY, sondage)
  batch              fenêtre de regroupement du courrier (`mail_batch`, L125)
  admit              du tour dû au tour admis (choix du compte, rotation de
                     session, admission atomique), hors `held`
  spawn              préparation et lancement du harnais
  harness_start      du lancement à la première ligne du harnais (démarrage du
                     harnais et de sa session chez le fournisseur)

`dominant` nomme l'étape la plus longue. `mail_ts` est lu sur l'horloge de la
base, les autres jalons sur celle de l'hôte : un décalage d'horloge déplace
du temps entre `wake` et l'étape voisine, jamais au-delà ; une étape négative
est ramenée à zéro.

#3513 demande de nommer les étages « reserve → deliver → harness start →
pending → grant ». Amesh en mesure trois et les expose en plus des étapes
séquentielles :

  reserve_s    `due_ts` → `admitted_ts` : attente d'admission (budget, compte,
               fenêtre courte) puis ouverture atomique du tour ; c'est la
               somme de `held` et `admit`, sous un seul nom pour le consommateur ;
  deliver_s    `spawned_ts` → `delivered_ts` : remise des messages après le
               lancement du harnais (absente pour un message humain, soldé en
               fin de tour) ;
  harness start est `spawn` + `harness_start` ; `first_output_ts` est le
               dernier jalon qu'ameesh voit. `pending` et `grant` appartiennent
               au consommateur, après `first_output_ts` : il les ajoute à son
               propre reçu, ameesh ne les invente pas.
"""
from __future__ import annotations

EVENT = "turn.stages"
STAGES = ("busy", "held", "wake", "batch", "admit", "spawn", "harness_start")
JALONS = ("mail_ts", "seen_ts", "due_ts", "admitted_ts", "spawned_ts",
          "delivered_ts", "first_output_ts")
#: au plus autant d'identifiants de messages dans le reçu
MAX_IDS = 20


def _overlap(a: float, b: float, c: float, d: float) -> float:
    return max(0.0, min(b, d) - max(a, c))


def _gap(start, end) -> float | None:
    """Écart positif entre deux jalons, ou None si l'un manque."""
    if start is None or end is None:
        return None
    return max(0.0, float(end) - float(start))


def _merged(intervals) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for start, end in sorted((float(s), float(e)) for s, e in intervals if e > s):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(s, e) for s, e in out]


def receipt(etapes: dict, retenues=()) -> dict:
    """Les données de l'événement `turn.stages` : jalons, durées par étape,
    étape dominante. `retenues` : les gardes d'admission récentes de l'agent,
    `(début, fin, nature)`."""
    ts = {k: etapes.get(k) for k in JALONS}
    chain = (("wake", ts["mail_ts"], ts["seen_ts"]),
             ("batch", ts["seen_ts"], ts["due_ts"]),
             ("admit", ts["due_ts"], ts["admitted_ts"]),
             ("spawn", ts["admitted_ts"], ts["spawned_ts"]),
             ("harness_start", ts["spawned_ts"], ts["first_output_ts"]))
    busy_end = etapes.get("prev_turn_end_ts")
    retenues = [r for r in retenues or () if r[1] > r[0]]
    holds = _merged((r[0], r[1]) for r in retenues)
    stages = dict.fromkeys(STAGES, 0.0)
    for name, start, end in chain:
        if start is None or end is None or end <= start:
            continue
        start, end = float(start), float(end)
        busy = _overlap(start, end, float("-inf"), float(busy_end)) \
            if busy_end is not None else 0.0
        libre = max(start, float(busy_end)) if busy_end is not None else start
        held = sum(_overlap(libre, end, s, e) for s, e in holds) if libre < end else 0.0
        stages["busy"] += busy
        stages["held"] += held
        stages[name] += max(0.0, (end - start) - busy - held)
    first = ts["mail_ts"]
    last = ts["first_output_ts"] or ts["spawned_ts"]
    total = max(0.0, float(last) - float(first)) if first and last else None
    reasons = sorted({r[2] for r in retenues if first is None or r[1] > float(first)})
    longest = max(STAGES, key=lambda k: stages[k])
    reserve = _gap(ts["due_ts"], ts["admitted_ts"])
    deliver = _gap(ts["spawned_ts"], ts["delivered_ts"])
    return {
        "messages": list(etapes.get("ids") or [])[:MAX_IDS],
        "ts": {k: round(float(v), 3) for k, v in ts.items() if v is not None},
        "stages_s": {k: round(v, 3) for k, v in stages.items()},
        "reserve_s": round(reserve, 3) if reserve is not None else None,
        "deliver_s": round(deliver, 3) if deliver is not None else None,
        "total_s": round(total, 3) if total is not None else None,
        "dominant": longest if stages[longest] > 0 else None,
        "held_reasons": reasons if stages["held"] > 0 else [],
    }
