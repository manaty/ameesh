# SPDX-License-Identifier: AGPL-3.0-only
"""Résumé d'une action et pages HTML d'ameesh-approve.

Le **résumé** est calculé par le service depuis l'action relue (jamais depuis
un texte fourni par un agent) ; c'est un texte stable, une ligne par champ,
dont le SHA-256 devient `summary_digest` dans la demande signée :

    summary_digest = "sha256:" + SHA-256(UTF-8(résumé))

Chaque valeur passe par `visible()` : les caractères de contrôle, de format
(contrôles bidirectionnels U+202E…, espaces de largeur nulle), les séparateurs
de ligne et les espaces exotiques sont écrits `\\u{XXXX}`, et la barre
oblique inverse est doublée. Une valeur ne peut donc ni s'étaler sur plusieurs
lignes (imiter un autre champ), ni se réordonner à l'affichage.

Une décision « assumer le doublon » (duplicate.py) a un résumé et une page
distincts : première ligne `décision : ASSUMER LE RISQUE D'UN DOUBLON…`,
empreinte de l'action ET empreinte de la décision, encadré d'alerte qui
nomme l'action, sa cible et son montant.

Les pages n'ont **aucun script en ligne** ni style en ligne (CSP stricte) :
le JavaScript et la feuille de style sont des fichiers statiques servis par le
service ; les données passent par des attributs `data-*` échappés. Toute
valeur dynamique est échappée par `html.escape`.
"""
from __future__ import annotations

import hashlib
import html
import time
import unicodedata

from .. import jcs
from . import duplicate

SUMMARY_VERSION = "ameesh-summary/1"
#: un résumé plus long n'est plus relisable par un humain sur un téléphone
MAX_SUMMARY = 32 * 1024

#: exposant des unités mineures (ISO 4217) quand il n'est pas 2
_MINOR_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}


def visible(text, *, json_text: bool = False) -> str:
    """Rend chaque caractère invisible ou trompeur explicite (`\\u{XXXX}`).

    `json_text` : le texte est déjà du JSON (JCS), où une barre oblique inverse
    littérale est déjà doublée — on ne la double pas une seconde fois.
    """
    out = []
    for char in str(text):
        if char == "\\" and not json_text:
            out.append("\\\\")
            continue
        category = unicodedata.category(char)
        if (category[0] == "C" or category in ("Zl", "Zp")
                or (category == "Zs" and char != " ")):
            out.append("\\u{%04x}" % ord(char))
        else:
            out.append(char)
    return "".join(out)


def has_non_ascii(text) -> bool:
    return any(ord(char) > 0x7E for char in str(text))


def format_amount(amount, currency) -> str:
    if amount is None:
        return "none"
    if currency is None:
        return "%d (minor units, no currency)" % amount
    exponent = _MINOR_EXPONENT.get(currency, 2)
    if exponent:
        units, minor = divmod(amount, 10 ** exponent)
        major = "%d.%0*d" % (units, exponent, minor)
    else:
        major = "%d" % amount
    return "%s %s (%d in minor units)" % (major, currency, amount)


def short_digest(digest: str) -> str:
    """« sha256:abcd… » → « abcd ef01 2345 6789 » (16 premiers chiffres)."""
    hexa = digest.split(":", 1)[-1][:16]
    return " ".join(hexa[i:i + 4] for i in range(0, len(hexa), 4))


_JSON_LABELS = ("policy version", "arguments")
#: première ligne du résumé d'une décision « assumer le doublon » : elle ne
#: peut pas être confondue avec un résumé d'approbation ordinaire
ASSUME_DUPLICATE_DECISION = ("ASSUME THE RISK OF A DUPLICATE: the outcome of the action is "
                             "unknown (it may already have happened); a new identical "
                             "action replaces it")


def summary_lines(action: dict, approver: str, requested_by: str, *,
                  assume_duplicate: bool = False) -> list[tuple[str, str]]:
    content = [
        ("project", action["project"]),
        ("connector", action["connector"]),
        ("operation", action["operation"]),
        ("target", action["target"]),
        ("class", action["class"]),
        ("amount", format_amount(action.get("amount"), action.get("currency"))),
        ("policy version", jcs.dumps(action.get("policy_version"))),
        ("arguments", jcs.dumps(action["args"])),
    ]
    people = [("approver", approver), ("requested by", requested_by)]
    if assume_duplicate:
        return ([("decision", ASSUME_DUPLICATE_DECISION),
                 ("replaced action", action["action_id"])]
                + content
                + [("action digest", action["computed_digest"]),
                   ("decision digest", duplicate.assume_duplicate_digest(action))]
                + people)
    return ([("action", action["action_id"])] + content
            + [("digest", action["computed_digest"])] + people)


def render_summary(action: dict, approver: str, requested_by: str, *,
                   assume_duplicate: bool = False) -> str:
    """Le résumé signé (par son empreinte) : texte stable, une ligne par champ."""
    lines = [SUMMARY_VERSION]
    lines += ["%s: %s" % (label, visible(value, json_text=label in _JSON_LABELS))
              for label, value in summary_lines(action, approver, requested_by,
                                                assume_duplicate=assume_duplicate)]
    return "\n".join(lines) + "\n"


def summary_digest(summary: str) -> str:
    return "sha256:" + hashlib.sha256(summary.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _moment(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch))


def _page(title: str, body: str, *, base: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        "<!-- SPDX-License-Identifier: AGPL-3.0-only -->\n"
        "<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n"
        "<title>%s</title>\n"
        "<link rel=\"stylesheet\" href=\"%sstatic/approve.css\">\n"
        "<script src=\"%sstatic/approve.js\" defer></script>\n"
        "</head>\n<body>\n%s\n</body>\n</html>\n"
    ) % (_e(title), _e(base), _e(base), body)


def message_page(title: str, message: str, *, base: str = "../") -> str:
    body = "<main class=\"message\">\n<h1>%s</h1>\n<p>%s</p>\n</main>" % (_e(title), _e(message))
    return _page(title, body, base=base)


def _field(label: str, value, *, warn: str = "") -> str:
    extra = "<span class=\"warn\">%s</span>" % _e(warn) if warn else ""
    return "<dt>%s</dt><dd><code>%s</code>%s</dd>" % (_e(label), _e(visible(value)), extra)


#: approbation ordinaire d'une action déjà tentée : la page le dit
_RETRY_NOTICES = {
    "failed": "New attempt of the SAME action (same identifier): the previous attempt "
              "definitely failed. Your approval authorizes ONE new attempt.",
    "unknown": "New attempt of the SAME action (same identifier): the outcome of the "
               "previous attempt is unknown, but the connector guarantees "
               "deduplication.",
}


def _duplicate_warning(action: dict) -> str:
    """Encadré d'une décision « assumer le doublon » : en clair, QUELLE action
    risque d'avoir lieu deux fois (opération, cible, montant)."""
    return (
        "<section class=\"duplicate\" role=\"alert\">\n"
        "<h2>This is NOT an ordinary approval</h2>\n"
        "<p>The action <code>%s</code> (<code>%s / %s</code> on the target <code>%s</code>, "
        "amount <code>%s</code>) was started, but its outcome is "
        "<strong>UNKNOWN</strong>: it may already have happened.</p>\n"
        "<p>By signing, you <strong>assume the risk of a DUPLICATE of this action</strong>: "
        "a new identical action replaces it and may produce the same effect a second "
        "time (it will still require its own approval). Only sign if a second effect "
        "is acceptable, or if you have checked that the first one did not happen.</p>\n"
        "</section>\n"
    ) % (_e(visible(action["action_id"])), _e(visible(action["connector"])),
         _e(visible(action["operation"])), _e(visible(action["target"])),
         _e(format_amount(action.get("amount"), action.get("currency"))))


def approval_page(view: dict) -> str:
    """Page d'approbation. `view` : voir ApproveService.link_view.

    Une décision « assumer le doublon » (`view["assume_duplicate"]`) a sa
    propre page : titre, encadré d'alerte, libellés et bouton distincts — elle
    ne ressemble jamais à une approbation ordinaire.
    """
    action = view["action"]
    request = view["request"]
    assume = view.get("assume_duplicate") is True
    non_ascii = "Warning: contains non-ASCII characters (lookalike risk)"
    fields = [
        _field("Operation", "%s / %s" % (action["connector"], action["operation"]),
               warn=non_ascii if has_non_ascii(action["connector"] + action["operation"])
               else ""),
        _field("Target", action["target"],
               warn=non_ascii if has_non_ascii(action["target"]) else ""),
        _field("Amount", format_amount(action.get("amount"), action.get("currency"))),
        _field("Class", action["class"]),
        _field("Project", action["project"],
               warn=non_ascii if has_non_ascii(action["project"]) else ""),
        _field("Replaced action" if assume else "Action", action["action_id"]),
        _field("Requested by", request["requested_by"]),
        _field("Approver", request["approver"]),
        _field("Link valid until", _moment(view["link_exp"])),
    ]
    if assume:
        fields.insert(0, _field("Decision", "ASSUME THE DUPLICATE (replacement)"))
    data = {
        "kind": "assume-duplicate" if assume else "approve",
        "rp-id": view["rp_id"],
        "challenge-approve": view["challenge_approve"],
        "challenge-deny": view["challenge_deny"],
        "credentials": ",".join(view["credentials"]),
        "level": view["level"],
    }
    attributes = " ".join("data-%s=\"%s\"" % (name, _e(value)) for name, value in data.items())
    if assume:
        heading = "Assume the risk of a DUPLICATE"
        lead = ("Check the replaced action, its target and its amount. Sign "
                "<strong>on your phone</strong> only; never scan an approval QR code "
                "shown on a computer.")
        notice = _duplicate_warning(action)
        digest_title = "Decision digest"
        digest_extra = ("<p class=\"digest\">digest of the replaced action: "
                        "<code>%s</code></p>\n" % _e(action["computed_digest"]))
        approve_label = "Assume the duplicate"
        title = "Duplicate to assume — ameesh"
    else:
        heading = "Approval requested"
        lead = ("Check the operation, the target and the amount. Approve "
                "<strong>on your phone</strong> only; never scan an approval QR code "
                "shown on a computer.")
        retry = _RETRY_NOTICES.get(action.get("state"))
        notice = "<p class=\"notice\">%s</p>\n" % _e(retry) if retry else ""
        digest_title = "Digest"
        digest_extra = ""
        approve_label = "Approve"
        title = "Approval — ameesh"
    body = (
        "<main id=\"ameesh-approval\" %s>\n"
        "<h1>%s</h1>\n"
        "<p class=\"lead\">%s</p>\n"
        "%s"
        "<dl class=\"fields\">\n%s\n</dl>\n"
        "<section>\n<h2>%s</h2>\n<p class=\"digest-short\">%s</p>\n"
        "<p class=\"digest\"><code>%s</code></p>\n%s</section>\n"
        "<section>\n<h2>Arguments</h2>\n<pre class=\"args\">%s</pre>\n</section>\n"
        "<section>\n<h2>Signed summary</h2>\n<pre class=\"summary\">%s</pre>\n"
        "<p class=\"digest\">summary digest: <code>%s</code></p>\n</section>\n"
        "<div class=\"buttons\">\n"
        "<button id=\"approve\" type=\"button\" class=\"approve\">%s</button>\n"
        "<button id=\"deny\" type=\"button\" class=\"deny\">Deny</button>\n"
        "</div>\n"
        "<p id=\"status\" role=\"status\" aria-live=\"polite\"></p>\n"
        "<noscript><p>JavaScript is required to sign with your passkey.</p></noscript>\n"
        "</main>"
    ) % (
        attributes, _e(heading), lead, notice, "\n".join(fields), _e(digest_title),
        _e(short_digest(request["digest"])), _e(request["digest"]), digest_extra,
        _e(visible(jcs.dumps(action["args"]), json_text=True)),
        _e(view["summary"]), _e(request["summary_digest"]), _e(approve_label),
    )
    return _page(title, body, base="../")


def enroll_page(view: dict) -> str:
    data = {
        "rp-id": view["rp_id"],
        "rp-name": view["rp_name"],
        "challenge": view["challenge"],
        "user-id": view["user_id"],
        "user-name": view["approver"],
        "exclude": ",".join(view["exclude"]),
    }
    attributes = " ".join("data-%s=\"%s\"" % (name, _e(value)) for name, value in data.items())
    body = (
        "<main id=\"ameesh-enroll\" %s>\n"
        "<h1>Enroll a passkey</h1>\n"
        "<p class=\"lead\">For <code>%s</code>, on the domain <code>%s</code>. Create the "
        "passkey <strong>on your phone</strong> (or a hardware key).</p>\n"
        "<p>The key will <strong>not be active</strong> right away: the service produces a "
        "canon entry proposal, which must be reviewed and merged through a PR before it "
        "becomes authoritative.</p>\n"
        "<p>Link valid until %s.</p>\n"
        "<div class=\"buttons\">\n"
        "<button id=\"enroll\" type=\"button\" class=\"approve\">Create the passkey</button>\n"
        "</div>\n"
        "<p id=\"status\" role=\"status\" aria-live=\"polite\"></p>\n"
        "<noscript><p>JavaScript is required to create a passkey.</p></noscript>\n"
        "</main>"
    ) % (attributes, _e(view["approver"]), _e(view["rp_id"]), _e(_moment(view["exp"])))
    return _page("Enrollment — ameesh", body, base="../")
