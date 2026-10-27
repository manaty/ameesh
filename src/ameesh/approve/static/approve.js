// SPDX-License-Identifier: AGPL-3.0-only
// ameesh-approve — signature d'une approbation (navigator.credentials.get) et
// enrôlement d'une passkey (navigator.credentials.create).
//
// Aucun script en ligne (CSP script-src 'self') ; les données viennent des
// attributs data-* de la page, rendus et échappés par le service. Le texte
// affiché passe uniquement par textContent (jamais innerHTML).
"use strict";

(function () {
  function fromB64u(text) {
    var b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    b64 += "===".slice((b64.length + 3) % 4);
    var binary = atob(b64);
    var out = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
      out[i] = binary.charCodeAt(i);
    }
    return out;
  }

  function toB64u(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function ids(text) {
    return (text || "").split(",").filter(function (id) {
      return id.length > 0;
    }).map(function (id) {
      return { type: "public-key", id: fromB64u(id) };
    });
  }

  function say(text, kind) {
    var status = document.getElementById("status");
    if (status) {
      status.textContent = text;
      status.className = kind || "";
    }
  }

  function setBusy(busy) {
    var buttons = document.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = busy;
    }
  }

  // -- envoi (canon §16, lot #3889) ----------------------------------------
  // Une panne du service (réseau, 5xx, 429) n'est pas un refus : le MÊME corps
  // signé est renvoyé, avec un délai croissant, jusqu'à une réponse définitive.
  // Le service rend le reçu déjà gardé pour un corps identique : aucun double
  // effet, et l'humain ne re-signe jamais. Un refus voulu (4xx) est expliqué,
  // jamais renvoyé.
  var POST_TIMEOUT_MS = 30000;
  var RETRY_DELAYS_S = [2, 5, 10, 15, 30, 60];

  function post(body) {
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timer = controller ? setTimeout(function () {
      controller.abort();
    }, POST_TIMEOUT_MS) : null;
    return fetch(window.location.pathname, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    }).finally(function () {
      if (timer) {
        clearTimeout(timer);
      }
    });
  }

  function transient(result) {
    return result.status >= 500 || result.status === 429;
  }

  // Rend la réponse définitive (2xx ou refus 4xx) ; `waiting(n)` est appelé
  // avant chaque nouvel essai. Ne rejette jamais.
  function deliver(body, waiting) {
    var attempt = 0;
    return new Promise(function (resolve) {
      function again() {
        var delay = RETRY_DELAYS_S[Math.min(attempt, RETRY_DELAYS_S.length - 1)];
        attempt += 1;
        waiting(attempt);
        setTimeout(once, delay * 1000);
      }
      function once() {
        post(body).then(function (result) {
          if (transient(result)) {
            again();
          } else {
            resolve(result);
          }
        }, again);
      }
      once();
    });
  }

  function pendingText() {
    return "Sending… The service is busy: your signed answer is kept and will be " +
      "sent by itself. You do not need to sign again.";
  }

  function refused(result) {
    return "Refused by the service (" + (result.data.error || result.status) + "): " +
      (result.data.message || "");
  }

  // Le corps signé (approbation, passkey d'un enrôlement), gardé sur
  // l'appareil AVANT l'envoi et jusqu'à la réponse définitive : une page
  // rechargée, rouverte ou en attente (« Just a moment ») le renvoie d'elle-même.
  var KEPT_PREFIX = "ameesh-approve:pending:";
  var KEPT_MAX_AGE_MS = 7 * 86400 * 1000;

  function keep(kind, body) {
    try {
      window.localStorage.setItem(KEPT_PREFIX + window.location.pathname,
        JSON.stringify({ at: Date.now(), kind: kind, body: body }));
    } catch (e) { /* stockage refusé : l'envoi continue en mémoire */ }
  }

  function forget() {
    try {
      window.localStorage.removeItem(KEPT_PREFIX + window.location.pathname);
    } catch (e) { /* rien */ }
  }

  // { kind, body } gardé pour cette page, ou null ; élague les trop anciens
  function kept() {
    try {
      var store = window.localStorage;
      for (var i = store.length - 1; i >= 0; i--) {
        var key = store.key(i);
        if (key && key.indexOf(KEPT_PREFIX) === 0) {
          var entry = JSON.parse(store.getItem(key) || "null");
          if (!entry || !(Date.now() - entry.at < KEPT_MAX_AGE_MS)) {
            store.removeItem(key);
          }
        }
      }
      var mine = JSON.parse(store.getItem(KEPT_PREFIX + window.location.pathname) || "null");
      return mine && mine.body ? { kind: mine.kind || "approve", body: mine.body } : null;
    } catch (e) {
      return null;
    }
  }

  // garde, envoie, renvoie après une panne, oublie à la réponse définitive
  function sendKept(kind, body, doneText) {
    setBusy(true);
    keep(kind, body);
    deliver(body, function () {
      say(pendingText(), "pending");
    }).then(function (result) {
      forget();
      if (result.ok) {
        say(doneText(result), "done");
      } else {
        setBusy(false);
        say(refused(result), "error");
      }
    });
  }

  function enrolled(result) {
    return "Passkey created. Key fingerprint: " +
      String(result.data.key_fingerprint || "").slice(0, 16) +
      ". It will be active after the canon PR is reviewed and merged.";
  }

  // la page d'attente (service momentanément indisponible) : un corps gardé
  // part d'ici ; sans lui, elle se recharge seule
  function waiting(root) {
    var entry = kept();
    if (entry) {
      say("Sending what you signed…");
      sendKept(entry.kind, entry.body, entry.kind === "enroll" ? enrolled
        : entry.kind === "login" ? settledLogin : function () {
          return "Your signed answer is recorded. You can close this page.";
        });
      return;
    }
    var delay = parseInt(root.dataset.retry || "15", 10) || 15;
    setTimeout(function () {
      window.location.reload();
    }, delay * 1000);
  }

  function supported() {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      say("This browser does not support passkeys (WebAuthn).", "error");
      return false;
    }
    return true;
  }

  // -- approbation ---------------------------------------------------------
  function approval(root) {
    var data = root.dataset;

    function signed(decision) {
      return decision === "approve"
        ? (data.kind === "assume-duplicate"
          ? "Decision signed: you assume the risk of a duplicate. You can close this page."
          : "Approval signed and recorded. You can close this page.")
        : "Denial signed and recorded. You can close this page.";
    }

    function send(body) {
      sendKept("approve", body, function () {
        return signed(body.decision);
      });
    }

    function decide(decision) {
      if (!supported()) {
        return;
      }
      setBusy(true);
      say("Signing: follow your device's instructions…");
      var challenge = decision === "approve" ? data.challengeApprove : data.challengeDeny;
      navigator.credentials.get({
        publicKey: {
          challenge: fromB64u(challenge),
          rpId: data.rpId,
          allowCredentials: ids(data.credentials),
          userVerification: "required",
          timeout: 120000,
          hints: ["client-device", "security-key"]
        }
      }).then(function (credential) {
        var response = credential.response;
        say("Sending your signed answer…");
        send({
          decision: decision,
          credential_id: toB64u(credential.rawId),
          authenticatorData: toB64u(response.authenticatorData),
          clientDataJSON: toB64u(response.clientDataJSON),
          signature: toB64u(response.signature)
        });
      }, function () {
        setBusy(false);
        say("Signing cancelled or refused by the device.", "error");
      });
    }

    var entry = kept();
    if (entry && entry.kind === "approve") {
      say("Sending your signed answer…");
      send(entry.body);
    }

    document.getElementById("approve").addEventListener("click", function () {
      decide("approve");
    });
    document.getElementById("deny").addEventListener("click", function () {
      decide("deny");
    });
  }

  // -- défi de connexion (L232d) -------------------------------------------
  // Le lien seul est muet : l'URL et le code ne sont demandés au service
  // qu'après une assertion (`view`) acceptée. Ils ne sont jamais écrits dans
  // le document HTML initial, seulement dans ces deux <code> par textContent.
  function settledLogin(result) {
    return result.data.decision === "cancel"
      ? "Cancellation signed and recorded. You can close this page."
      : "Done signed and recorded. You can close this page.";
  }

  function login(root) {
    var data = root.dataset;
    var privateSection = document.getElementById("login-private");
    var openButton = document.getElementById("login-open");
    var doneButton = document.getElementById("login-done");
    var cancelButton = document.getElementById("login-cancel");
    var doneChallenge = "";

    var entry = kept();
    if (entry && entry.kind === "login") {   // done/cancel déjà signé repart
      say("Sending what you signed…");
      sendKept("login", entry.body, settledLogin);
    }

    function sign(decision, challenge) {
      setBusy(true);
      return new Promise(function (resolve, reject) {
        navigator.credentials.get({
          publicKey: {
            challenge: fromB64u(challenge),
            rpId: data.rpId,
            allowCredentials: ids(data.credentials),
            userVerification: "required",
            timeout: 120000,
            hints: ["client-device", "security-key"]
          }
        }).then(function (credential) {
          var response = credential.response;
          var body = {
            decision: decision,
            credential_id: toB64u(credential.rawId),
            authenticatorData: toB64u(response.authenticatorData),
            clientDataJSON: toB64u(response.clientDataJSON),
            signature: toB64u(response.signature)
          };
          // §16 (#3889) : done/cancel (un reçu) gardés sur l'appareil AVANT
          // l'envoi et repris au rechargement ; jamais `view` (sa réponse
          // porte l'URL et le code privés, et elle ne s'écrit nulle part)
          var final = decision !== "view";
          if (final) {
            keep("login", body);
          }
          deliver(body, function () {
            say(pendingText(), "pending");
          }).then(function (result) {
            if (final) {
              forget();
            }
            if (result.ok) {
              resolve(result.data);
            } else {
              setBusy(false);
              say(refused(result), "error");
              reject(new Error("refused"));
            }
          });
        }, function () {
          setBusy(false);
          say("Signing cancelled or refused by the device.", "error");
          reject(new Error("cancelled"));
        });
      });
    }

    openButton.addEventListener("click", function () {
      if (!supported()) {
        return;
      }
      say("Signing: follow your device's instructions…");
      sign("view", data.challengeView).then(function (payload) {
        document.getElementById("login-url").textContent = payload.url || "";
        document.getElementById("login-code").textContent = payload.code || "";
        available(payload.challenge_done);
        privateSection.hidden = false;
        openButton.hidden = true;
        doneButton.hidden = false;
        setBusy(false);
        say("Private challenge shown. Do not share it.", "done");
      }).catch(function () {});
    });

    function available(challenge) {
      doneChallenge = challenge || "";
    }

    doneButton.addEventListener("click", function () {
      if (!doneChallenge) {
        return;
      }
      say("Signing: follow your device's instructions…");
      sign("done", doneChallenge).then(function () {
        setBusy(true);
        say("Done signed and recorded. You can close this page.", "done");
      }).catch(function () {});
    });

    cancelButton.addEventListener("click", function () {
      if (!supported()) {
        return;
      }
      say("Signing: follow your device's instructions…");
      sign("cancel", data.challengeCancel).then(function () {
        setBusy(true);
        say("Cancellation signed and recorded. You can close this page.", "done");
      }).catch(function () {});
    });
  }

  // -- enrôlement ----------------------------------------------------------
  function enrollment(root) {
    var data = root.dataset;
    var entry = kept();
    if (entry && entry.kind === "enroll") {   // la passkey déjà créée repart
      say("Sending the new passkey…");
      sendKept("enroll", entry.body, enrolled);
    }
    document.getElementById("enroll").addEventListener("click", function () {
      if (!supported()) {
        return;
      }
      setBusy(true);
      say("Creating the passkey: follow your device's instructions…");
      navigator.credentials.create({
        publicKey: {
          rp: { id: data.rpId, name: data.rpName },
          user: { id: fromB64u(data.userId), name: data.userName, displayName: data.userName },
          challenge: fromB64u(data.challenge),
          pubKeyCredParams: [{ type: "public-key", alg: -7 }, { type: "public-key", alg: -8 }],
          authenticatorSelection: { userVerification: "required", residentKey: "preferred" },
          attestation: "none",
          excludeCredentials: ids(data.exclude),
          timeout: 300000
        }
      }).then(function (credential) {
        var response = credential.response;
        var body = {
          credential_id: toB64u(credential.rawId),
          clientDataJSON: toB64u(response.clientDataJSON),
          attestationObject: toB64u(response.attestationObject)
        };
        if (typeof response.getTransports === "function") {
          body.transports = response.getTransports();
        }
        say("Sending the new passkey…");
        sendKept("enroll", body, enrolled);
      }, function () {
        setBusy(false);
        say("Creation cancelled or refused by the device.", "error");
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("ameesh-approval");
    if (root) {
      approval(root);
    }
    root = document.getElementById("ameesh-login");
    if (root) {
      login(root);
    }
    root = document.getElementById("ameesh-enroll");
    if (root) {
      enrollment(root);
    }
    root = document.getElementById("ameesh-wait");
    if (root) {
      waiting(root);
    }
  });
})();
