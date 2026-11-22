---
type: Exploration
status: exploration
title: "claude-reauth: re-login a remote Claude Code profile without manual steps"
---

# claude-reauth: re-login a remote Claude Code profile without manual steps

Status: **exploration** (lot #8038). The tool lives in `deploy/poste/claude-reauth`; nothing in the mesh calls it.

## Problem

Claude Code profiles on mesh hosts (`CLAUDE_CONFIG_DIR=…/.claude*`) use claude.ai OAuth. When a login expires, `credential_expiring` fires and a session running with `--remote-control` reports every failed turn to claude.ai, which notifies the owner's browser. Today the fix is manual: run `claude auth login` in a tmux on the host, open the URL in a browser signed in to the right account, click *Authorize*, copy the code back.

## What the tool does

`claude-reauth EMAIL --ssh "ssh -J root@HUB root@HOST" --user ameesh --config-dir /home/ameesh/.claude`

1. starts `claude auth login` in a tmux session on the host and reads the authorization URL;
2. opens it in a Playwright-driven Chrome with a **dedicated profile per account** (`~/.local/share/claude-reauth/profiles/EMAIL`);
3. if claude.ai asks to log in: e-mail login by default (the user pastes the link or the verification code from the e-mail in the terminal), or `--google` (Google password asked in the terminal, never stored or logged; 2FA done in the window);
4. checks that the authorize page shows EMAIL, clicks *Authorize*, reads the code;
5. pastes the code into the host's tmux and verifies `claude auth status` (logged in as EMAIL).

`claude-reauth EMAIL --url URL` only prints the code.

## First real run (22/11/2026)

- The claude.ai login page runs an invisible hCaptcha; a challenge, when shown, is solved by the human in the window. The tool never tries to solve or bypass it.
- *Continue with Google* stays busy forever in the driven browser (likely FedCM); e-mail login is therefore the default path.
- Playwright's default `--no-sandbox` flag is removed (`chromium_sandbox=True`).
- Two runs cannot share a profile: Chrome refuses a profile already open.

## Open questions

- Once the dedicated profile holds a claude.ai session, does a re-login need no human step at all (`--headless`)? How long does that session last?
- Could hosts without remote control use `claude setup-token` (long-lived token) instead, and keep browser login only for `--remote-control` sessions?
- Where should it run: only on the owner's workstation (it needs the owner's claude.ai sessions), triggered by `credential_expiring`?
