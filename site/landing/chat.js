/* SPDX-License-Identifier: AGPL-3.0-only */
/* "Ask about ameesh": a small chat bubble for the public site (L33).
   No library, no CDN, no storage: the short conversation history lives in this
   page only and is sent with each question. The endpoint comes from
   chat-config.js (window.AMEESH_CHAT_ENDPOINT); empty = the bubble is hidden.
   Answers are rendered as text; only links to the ameesh site or repository
   become clickable. */
(function () {
  "use strict";
  var endpoint = (window.AMEESH_CHAT_ENDPOINT || "").trim();
  if (!endpoint || !/^https:\/\//.test(endpoint) || document.getElementById("amc-root")) return;

  var DOCS = "https://ameesh.org/docs/";
  var MAX_CHARS = 500;
  var MAX_HISTORY = 6;
  var TIMEOUT_MS = 35000;
  var SAFE_LINK = /^https:\/\/(ameesh\.manaty\.net\/|github\.com\/manaty\/ameesh(\/|$))/;

  var TEXT = {
    en: {
      open: "Ask about ameesh", close: "Close", title: "Ask about ameesh",
      placeholder: "Ask a question about ameesh…", send: "Send", label: "Your question",
      notice: "Questions are sent to a third-party model provider (DeepSeek) to produce the answer; ameesh keeps nothing.",
      hello: "Hi! Ask me anything about ameesh — concepts, setup, security. I answer from the documentation.",
      thinking: "Looking in the documentation…", sources: "Sources",
      error: "The assistant is not available right now. Please see the documentation:",
      limited: "Too many questions for now — please wait a moment, or see the documentation:",
      tooLong: "Your question is too long (500 characters max).", docs: "Documentation"
    },
    fr: {
      open: "Questions sur ameesh", close: "Fermer", title: "Questions sur ameesh",
      placeholder: "Posez une question sur ameesh…", send: "Envoyer", label: "Votre question",
      notice: "Les questions sont envoyées à un fournisseur de modèle tiers (DeepSeek) pour produire la réponse ; ameesh ne conserve rien.",
      hello: "Bonjour ! Posez une question sur ameesh — concepts, installation, sécurité. Je réponds à partir de la documentation.",
      thinking: "Recherche dans la documentation…", sources: "Sources",
      error: "L'assistant n'est pas disponible pour le moment. Voir la documentation :",
      limited: "Trop de questions pour l'instant — patientez un peu, ou voyez la documentation :",
      tooLong: "Question trop longue (500 caractères au plus).", docs: "Documentation"
    },
    es: {
      open: "Preguntas sobre ameesh", close: "Cerrar", title: "Preguntas sobre ameesh",
      placeholder: "Haz una pregunta sobre ameesh…", send: "Enviar", label: "Tu pregunta",
      notice: "Las preguntas se envían a un proveedor de modelos externo (DeepSeek) para generar la respuesta; ameesh no conserva nada.",
      hello: "¡Hola! Pregunta lo que quieras sobre ameesh. Respondo a partir de la documentación.",
      thinking: "Buscando en la documentación…", sources: "Fuentes",
      error: "El asistente no está disponible ahora. Consulta la documentación:",
      limited: "Demasiadas preguntas por ahora; espera un momento o consulta la documentación:",
      tooLong: "La pregunta es demasiado larga (500 caracteres como máximo).", docs: "Documentación"
    },
    de: {
      open: "Fragen zu ameesh", close: "Schließen", title: "Fragen zu ameesh",
      placeholder: "Stellen Sie eine Frage zu ameesh…", send: "Senden", label: "Ihre Frage",
      notice: "Fragen werden an einen externen Modellanbieter (DeepSeek) gesendet, um die Antwort zu erzeugen; ameesh speichert nichts.",
      hello: "Hallo! Fragen Sie alles zu ameesh. Ich antworte anhand der Dokumentation.",
      thinking: "Suche in der Dokumentation…", sources: "Quellen",
      error: "Der Assistent ist gerade nicht verfügbar. Siehe die Dokumentation:",
      limited: "Zu viele Fragen im Moment – bitte kurz warten oder die Dokumentation lesen:",
      tooLong: "Die Frage ist zu lang (höchstens 500 Zeichen).", docs: "Dokumentation"
    }
  };
  var lang = ((navigator.language || "en").slice(0, 2) || "en").toLowerCase();
  var t = TEXT[lang] || TEXT.en;

  var history = [];
  var busy = false;

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
    if (text) node.textContent = text;
    return node;
  }

  /* text → DOM: paragraphs, list lines, `code`, and safe links only */
  var INLINE = /\[([^\]\n]{1,200})\]\((https:\/\/[^\s)]+)\)|(https:\/\/[^\s<>()\]]+)|`([^`\n]{1,200})`/g;
  function inline(parent, text) {
    var last = 0, m;
    INLINE.lastIndex = 0;
    while ((m = INLINE.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[4] !== undefined) {
        parent.appendChild(el("code", null, m[4]));
      } else {
        var url = (m[2] || m[3]).replace(/[.,;:!?]+$/, "");
        var label = m[1] || url;
        if (SAFE_LINK.test(url)) {
          parent.appendChild(el("a", { href: url, target: "_blank", rel: "noopener" }, label));
        } else {
          parent.appendChild(document.createTextNode(label));
        }
        if (!m[2] && url.length < m[3].length) parent.appendChild(document.createTextNode(m[3].slice(url.length)));
      }
      last = INLINE.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  function render(container, text) {
    var blocks = String(text).replace(/\*\*/g, "").split(/\n\s*\n/);
    blocks.forEach(function (block) {
      var lines = block.split("\n");
      var isList = lines.every(function (l) { return /^\s*([-*•]|\d+[.)])\s+/.test(l) || !l.trim(); });
      if (isList) {
        var ul = el("ul");
        lines.forEach(function (l) {
          if (!l.trim()) return;
          var li = el("li");
          inline(li, l.replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
          ul.appendChild(li);
        });
        container.appendChild(ul);
      } else {
        var p = el("p");
        lines.forEach(function (l, i) {
          if (i) p.appendChild(el("br"));
          inline(p, l.replace(/^#{1,6}\s+/, ""));
        });
        container.appendChild(p);
      }
    });
  }

  /* --- structure ------------------------------------------------------- */
  var root = el("div", { id: "amc-root", class: "amc" });
  var bubble = el("button", { type: "button", class: "amc-bubble", "aria-expanded": "false",
    "aria-controls": "amc-panel" });
  bubble.appendChild(el("span", { class: "amc-bubble-icon", "aria-hidden": "true" }, "?"));
  bubble.appendChild(el("span", { class: "amc-bubble-label" }, t.open));

  var panel = el("div", { id: "amc-panel", class: "amc-panel", role: "dialog",
    "aria-modal": "false", "aria-labelledby": "amc-title", hidden: "" });
  var head = el("div", { class: "amc-head" });
  head.appendChild(el("h2", { id: "amc-title", class: "amc-title" }, t.title));
  var closeBtn = el("button", { type: "button", class: "amc-close", "aria-label": t.close }, "×");
  head.appendChild(closeBtn);
  var notice = el("p", { class: "amc-notice", id: "amc-notice" }, t.notice);
  var log = el("div", { class: "amc-log", role: "log", "aria-live": "polite", "aria-relevant": "additions",
    tabindex: "0", "aria-label": t.title });
  var form = el("form", { class: "amc-form" });
  var label = el("label", { for: "amc-input", class: "amc-sr" }, t.label);
  var input = el("textarea", { id: "amc-input", class: "amc-input", rows: "2", maxlength: String(MAX_CHARS),
    placeholder: t.placeholder, "aria-describedby": "amc-notice", autocomplete: "off" });
  var send = el("button", { type: "submit", class: "amc-send" }, t.send);
  form.appendChild(label);
  form.appendChild(input);
  form.appendChild(send);
  panel.appendChild(head);
  panel.appendChild(notice);
  panel.appendChild(log);
  panel.appendChild(form);
  root.appendChild(panel);
  root.appendChild(bubble);

  function addMessage(role, text, sources) {
    var item = el("div", { class: "amc-msg amc-" + role });
    if (role === "bot") render(item, text); else item.appendChild(el("p", null, text));
    if (sources && sources.length && !/\bhttps:\/\//.test(text)) {
      var s = el("p", { class: "amc-sources" }, t.sources + ": ");
      sources.forEach(function (src, i) {
        if (!SAFE_LINK.test(src.url)) return;
        if (i) s.appendChild(document.createTextNode(" · "));
        s.appendChild(el("a", { href: src.url, target: "_blank", rel: "noopener" }, src.title || src.url));
      });
      item.appendChild(s);
    }
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
    return item;
  }
  function addFailure(message) {
    var item = el("div", { class: "amc-msg amc-bot amc-error" });
    var p = el("p", null, message + " ");
    p.appendChild(el("a", { href: DOCS, target: "_blank", rel: "noopener" }, t.docs));
    item.appendChild(p);
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
  }

  function open() {
    panel.hidden = false;
    bubble.setAttribute("aria-expanded", "true");
    root.classList.add("amc-open");
    if (!log.childNodes.length) addMessage("bot", t.hello);
    input.focus();
  }
  function close() {
    panel.hidden = true;
    bubble.setAttribute("aria-expanded", "false");
    root.classList.remove("amc-open");
    bubble.focus();
  }
  bubble.addEventListener("click", function () { if (panel.hidden) open(); else close(); });
  closeBtn.addEventListener("click", close);
  panel.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.preventDefault(); close(); } });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : submit(e); }
  });
  form.addEventListener("submit", submit);

  function submit(e) {
    if (e) e.preventDefault();
    var question = input.value.trim();
    if (!question || busy) return;
    if (question.length > MAX_CHARS) { addFailure(t.tooLong); return; }
    busy = true;
    send.disabled = true;
    input.value = "";
    addMessage("user", question);
    var waiting = el("div", { class: "amc-msg amc-bot amc-wait" }, t.thinking);
    log.appendChild(waiting);
    log.scrollTop = log.scrollHeight;
    log.setAttribute("aria-busy", "true");

    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, TIMEOUT_MS);
    fetch(endpoint, {
      method: "POST", mode: "cors", credentials: "omit", redirect: "error", cache: "no-store",
      referrerPolicy: "no-referrer",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question, history: history.slice(-MAX_HISTORY) }),
      signal: ctrl ? ctrl.signal : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) { return { status: r.status, body: body }; });
    }).then(function (res) {
      waiting.remove();
      if (res.status === 200 && res.body && typeof res.body.answer === "string") {
        addMessage("bot", res.body.answer, res.body.sources || []);
        history.push({ role: "user", content: question }, { role: "assistant", content: res.body.answer.slice(0, 1200) });
        history = history.slice(-MAX_HISTORY);
      } else if (res.status === 429) {
        addFailure(t.limited);
      } else if (res.status === 400 && res.body && res.body.error === "too_long") {
        addFailure(t.tooLong);
      } else {
        addFailure(t.error);
      }
    }).catch(function () {
      waiting.remove();
      addFailure(t.error);
    }).then(function () {
      clearTimeout(timer);
      busy = false;
      send.disabled = false;
      log.removeAttribute("aria-busy");
      input.focus();
    });
  }

  function mount() { document.body.appendChild(root); }
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
})();
