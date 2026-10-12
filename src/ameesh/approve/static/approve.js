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

  function post(body) {
    return fetch(window.location.pathname, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      referrerPolicy: "no-referrer"
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
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
        return post({
          decision: decision,
          credential_id: toB64u(credential.rawId),
          authenticatorData: toB64u(response.authenticatorData),
          clientDataJSON: toB64u(response.clientDataJSON),
          signature: toB64u(response.signature)
        });
      }, function () {
        throw new Error("cancelled");
      }).then(function (result) {
        if (result.ok) {
          var approved = data.kind === "assume-duplicate"
            ? "Decision signed: you assume the risk of a duplicate. You can close this page."
            : "Approval signed and recorded. You can close this page.";
          say(decision === "approve"
            ? approved
            : "Denial signed and recorded. You can close this page.", "done");
        } else {
          setBusy(false);
          say("Refused by the service (" + (result.data.error || result.status) + "): " +
            (result.data.message || ""), "error");
        }
      }).catch(function (error) {
        setBusy(false);
        say(error && error.message === "cancelled"
          ? "Signing cancelled or refused by the device."
          : "Network error: try again.", "error");
      });
    }

    document.getElementById("approve").addEventListener("click", function () {
      decide("approve");
    });
    document.getElementById("deny").addEventListener("click", function () {
      decide("deny");
    });
  }

  // -- enrôlement ----------------------------------------------------------
  function enrollment(root) {
    var data = root.dataset;
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
        return post(body);
      }, function () {
        throw new Error("cancelled");
      }).then(function (result) {
        if (result.ok) {
          say("Passkey created. Key fingerprint: " + String(result.data.key_fingerprint || "").slice(0, 16) +
            ". It will be active after the canon PR is reviewed and merged.", "done");
        } else {
          setBusy(false);
          say("Refused by the service (" + (result.data.error || result.status) + "): " +
            (result.data.message || ""), "error");
        }
      }).catch(function (error) {
        setBusy(false);
        say(error && error.message === "cancelled"
          ? "Creation cancelled or refused by the device."
          : "Network error: try again.", "error");
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("ameesh-approval");
    if (root) {
      approval(root);
    }
    root = document.getElementById("ameesh-enroll");
    if (root) {
      enrollment(root);
    }
  });
})();
