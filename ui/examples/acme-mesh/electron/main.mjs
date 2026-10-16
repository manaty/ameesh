// SPDX-License-Identifier: AGPL-3.0-only
// Reference main process of an Electron host (L92, study §2.3): one window
// per mesh, tokens sealed by safeStorage, approvals in the system browser,
// notifications from the main process. It is a reference, not a runnable
// application: Electron is not a dependency of ameesh and `npm test` only
// proves this file parses. See GUIDE-ELECTRON.md.
import { app, BrowserWindow, Notification, ipcMain, safeStorage, shell } from "electron";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** The meshes this application knows (study §3): entered by the human, or
 *  supplied by the organisation. One application, one window per mesh. */
const MESHES = [
  { id: "acme", name: "Acme", apiBase: "https://mesh.acme.example" },
];
/** Approvals open on the mesh's own origin (ameesh-approve); nowhere else. */
const APPROVAL_ORIGINS = new Set(MESHES.map((mesh) => new URL(mesh.apiBase).origin));

let win = null;

const tokenPath = (mesh) => path.join(app.getPath("userData"), `token-${mesh.id}.bin`);

/** The token is sealed at rest and never written by the renderer. */
async function readToken(mesh) {
  try {
    return safeStorage.decryptString(await readFile(tokenPath(mesh)));
  } catch {
    return null; // no session yet: the human signs in (OIDC + PKCE, RFC 8252)
  }
}

async function writeToken(mesh, token) {
  await mkdir(path.dirname(tokenPath(mesh)), { recursive: true });
  await writeFile(tokenPath(mesh), safeStorage.encryptString(token), { mode: 0o600 });
}

function createWindow(mesh) {
  win = new BrowserWindow({
    width: 1180, height: 820,
    webPreferences: {
      // self-contained CommonJS: a sandboxed preload cannot use ESM
      preload: path.join(HERE, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      // one partition per mesh: cookies and storage never cross (study §3)
      partition: `persist:ameesh-${mesh.id}`,
    },
  });
  void win.loadFile(path.join(HERE, "renderer.html"));
}

/** Alerts with the window closed: the main process holds the stream. */
async function followAlerts(mesh) {
  const token = await readToken(mesh);
  if (!token) return;
  const response = await fetch(new URL("/api/v1/events", mesh.apiBase), {
    headers: { Accept: "text/event-stream", Authorization: `Bearer ${token}` },
  });
  if (!response.ok || !response.body) return;
  let buffer = "";
  for await (const chunk of response.body) {
    buffer += Buffer.from(chunk).toString("utf8");
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = frame.split("\n").filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim()).join("\n");
      if (!data) continue; // a heartbeat comment is not an event
      try {
        const alert = JSON.parse(data);
        if (alert?.text) new Notification({ title: alert.title ?? "ameesh", body: alert.text }).show();
      } catch { /* not an alert frame */ }
    }
  }
}

void app.whenReady().then(() => {
  ipcMain.handle("ameesh:meshes", () =>
    MESHES.map(({ id, name, apiBase }) => ({ id, name, apiBase })));
  ipcMain.handle("ameesh:get-token", (_event, id) => {
    const mesh = MESHES.find((item) => item.id === (id ?? MESHES[0].id));
    return mesh ? readToken(mesh) : null;
  });
  // Filled by the sign-in flow (authorization code + PKCE in the system
  // browser); the renderer never sees the sealing, only the token.
  ipcMain.handle("ameesh:set-token", (_event, id, token) => {
    const mesh = MESHES.find((item) => item.id === id);
    return mesh ? writeToken(mesh, token) : undefined;
  });
  ipcMain.handle("ameesh:open-approval", (_event, url) => {
    const target = new URL(url);
    if (!APPROVAL_ORIGINS.has(target.origin)) {
      throw new Error("approval origin refused: " + target.origin);
    }
    return shell.openExternal(target.href); // the human signs, in the foreground
  });
  ipcMain.handle("ameesh:notify", (_event, alert) => {
    new Notification({ title: alert.title ?? "ameesh", body: alert.text }).show();
  });
  // Navigation stays the application's: the renderer's router listens.
  ipcMain.handle("ameesh:open", (_event, target) => {
    win?.webContents.send("ameesh:navigate", target);
  });
  createWindow(MESHES[0]);
  for (const mesh of MESHES) void followAlerts(mesh);
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(MESHES[0]); });
});

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
