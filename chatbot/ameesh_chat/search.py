# SPDX-License-Identifier: AGPL-3.0-only
"""Recherche lexicale multilingue « au mieux » dans l'index de passages.

Pas de base vectorielle, pas de service externe : BM25 sur des jetons
normalisés (minuscules, accents retirés), une racine grossière (troncature à
six caractères après retrait d'un pluriel), des bigrammes pour les écritures
sans espaces (chinois, japonais, coréen), un petit glossaire qui rapproche les
mots courants de quelques langues des termes anglais de la documentation, et un
bonus pour les mots du titre.

Le budget de contexte est borné : on ne garde que les meilleurs passages tant
que l'estimation de jetons tient dans le budget.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

#: mots vides (en, fr, es, de, it, pt) : courts, sans poids pour la recherche
STOPWORDS = frozenset("""
a an and are as at be by can do does for from has have how i if in into is it its me my
not of on or so than that the their them then there these this to was what when where
which who why will with you your about should would could
au aux avec ce ces cet cette comme comment dans de des du elle en est et etre il ils je
la le les leur mais me mon ne nous on ou par pas pour qu que qui quoi sa se son sont sur
ta te ton tu un une vos votre vous y faire fait peut puis quel quelle quels quelles
al con como cual cuando el es esta este los las lo mas para pero por que se sin su sus una uno
das dem den der des die ein eine einen ist mit nicht oder und von wie was wer zu im auf
che chi cosa di il gli nel per piu sono come
como uma um os as no na em por para que qual
""".split())

#: glossaire minimal : mot normalisé d'une autre langue → termes anglais de la doc
GLOSSARY = {
    # français
    "recu": "receipt", "recus": "receipt", "jeton": "token", "jetons": "token",
    "cout": "cost", "couts": "cost", "porte": "gate", "fil": "thread", "fils": "thread",
    "hote": "host", "hotes": "host", "humain": "human", "humains": "human",
    "approbation": "approval", "approuver": "approve", "cle": "key", "cles": "key",
    "equipe": "team", "equipes": "team", "securite": "security", "installer": "install",
    "installation": "install", "demarrer": "start", "configuration": "configuration",
    "bail": "lease", "baux": "lease", "executeur": "runner", "irreversible": "irreversible",
    "depot": "repository", "fusion": "merge", "fusionner": "merge", "plafond": "cap",
    "feuille": "roadmap", "route": "roadmap", "licence": "license", "jauge": "gauge",
    "forfait": "plan", "abonnement": "plan", "modele": "model", "modeles": "model",
    "harnais": "harness", "lot": "lot", "revue": "review", "demo": "demo",
    "decision": "decision", "exigence": "requirement", "exigences": "requirement",
    # espagnol / portugais / italien
    "recibo": "receipt", "costo": "cost", "coste": "cost", "custo": "cost",
    "puerta": "gate", "hilo": "thread", "anfitrion": "host", "humano": "human",
    "aprobacion": "approval", "aprovacao": "approval", "approvazione": "approval",
    "clave": "key", "chave": "key", "chiave": "key", "equipo": "team", "equipe": "team",
    "seguridad": "security", "seguranca": "security", "sicurezza": "security",
    "instalar": "install", "installare": "install", "agente": "agent", "agentes": "agent",
    "presupuesto": "budget", "orcamento": "budget", "licencia": "license",
    # allemand
    "quittung": "receipt", "kosten": "cost", "tor": "gate", "faden": "thread",
    "mensch": "human", "menschen": "human", "genehmigung": "approval",
    "schlussel": "key", "sicherheit": "security", "installieren": "install",
    "agenten": "agent", "rechner": "host", "lizenz": "license",
}

_WORD = re.compile(r"\w+", re.UNICODE)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF or 0xF900 <= code <= 0xFAFF)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def stem(word: str) -> str:
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word[:6]


def tokens(text: str, *, expand: bool = False) -> list[str]:
    """Jetons de recherche ; `expand` ajoute les équivalents du glossaire (requête)."""
    out: list[str] = []
    for raw in _WORD.findall(normalize(text)):
        if any(_is_cjk(ch) for ch in raw):
            chars = [ch for ch in raw if _is_cjk(ch)]
            out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
            if len(chars) == 1:
                out.append(chars[0])
            rest = "".join(ch for ch in raw if not _is_cjk(ch))
            if len(rest) > 1:
                out.append(stem(rest))
            continue
        if raw in STOPWORDS or (len(raw) < 2 and not raw.isdigit()):
            continue
        out.append(stem(raw))
        if expand and raw in GLOSSARY:
            out.append(stem(GLOSSARY[raw]))
    return out


def estimate_tokens(text: str) -> int:
    """Estimation prudente (≈ 3,5 caractères par jeton, arrondi au-dessus)."""
    return int(math.ceil(len(text) / 3.5))


@dataclass
class Passage:
    id: str
    title: str
    section: str
    url: str
    text: str
    source: str = "site"            #: "site" (doc publique) ou "design" (docs/design, en français)
    _tf: dict = field(default_factory=dict, repr=False)
    _title_terms: frozenset = field(default=frozenset(), repr=False)
    _length: int = 0


@dataclass
class Hit:
    passage: Passage
    score: float


class Index:
    """Index BM25 en mémoire, construit au chargement de la fonction."""

    K1 = 1.4
    B = 0.75
    TITLE_BOOST = 1.6
    SITE_BOOST = 1.15

    def __init__(self, passages: list[dict]):
        self.passages: list[Passage] = []
        df: dict[str, int] = {}
        for raw in passages:
            p = Passage(id=str(raw["id"]), title=str(raw.get("title", "")),
                        section=str(raw.get("section", "")), url=str(raw["url"]),
                        text=str(raw["text"]), source=str(raw.get("source", "site")))
            terms = tokens(p.text)
            title_terms = tokens(p.title + " " + p.section)
            tf: dict[str, int] = {}
            for t in terms + title_terms:
                tf[t] = tf.get(t, 0) + 1
            p._tf, p._title_terms, p._length = tf, frozenset(title_terms), len(terms) + len(title_terms)
            for t in tf:
                df[t] = df.get(t, 0) + 1
            self.passages.append(p)
        n = max(len(self.passages), 1)
        self.avg_len = sum(p._length for p in self.passages) / n if self.passages else 1.0
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def from_json(cls, data: dict) -> "Index":
        return cls(list(data.get("passages", [])))

    def search(self, query: str, *, context: str = "", limit: int = 20) -> list[Hit]:
        """`context` : la question précédente, à poids réduit (suite de conversation)."""
        weights: dict[str, float] = {}
        for t in tokens(query, expand=True):
            weights[t] = max(weights.get(t, 0.0), 1.0)
        for t in tokens(context, expand=True):
            weights[t] = max(weights.get(t, 0.0), 0.4)
        hits: list[Hit] = []
        for p in self.passages:
            score = 0.0
            norm = self.K1 * (1 - self.B + self.B * p._length / (self.avg_len or 1.0))
            for t, w in weights.items():
                f = p._tf.get(t)
                if not f:
                    continue
                s = self.idf.get(t, 0.0) * f * (self.K1 + 1) / (f + norm)
                if t in p._title_terms:
                    s *= self.TITLE_BOOST
                score += w * s
            if score > 0:
                if p.source == "site":
                    score *= self.SITE_BOOST
                hits.append(Hit(p, score))
        hits.sort(key=lambda h: (-h.score, h.passage.id))
        return hits[:limit]

    def fallback(self) -> list[Passage]:
        """Passages d'ouverture (vue d'ensemble) quand rien ne correspond."""
        return [p for p in self.passages if p.source == "site"][:2]


def select(hits: list[Hit], *, budget_tokens: int, max_passages: int = 8,
           per_page: int = 3) -> list[Passage]:
    """Les meilleurs passages tant que le budget de jetons tient."""
    chosen: list[Passage] = []
    used = 0
    pages: dict[str, int] = {}
    for hit in hits:
        p = hit.passage
        page = p.url.split("#", 1)[0]
        if pages.get(page, 0) >= per_page:
            continue
        cost = estimate_tokens(p.text) + estimate_tokens(p.title + p.section + p.url) + 12
        if used + cost > budget_tokens:
            continue
        chosen.append(p)
        used += cost
        pages[page] = pages.get(page, 0) + 1
        if len(chosen) >= max_passages:
            break
    return chosen
