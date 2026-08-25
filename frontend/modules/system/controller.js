import { systemApi } from "../api/system.js";
import { byId, escapeHtml } from "../core/dom.js";

export function createSystemController({ isAppMode }) {
  const $ = byId;

async function checkSetup() {
  const banner = $("setup-banner");
  if (!banner) return;
  let checks;
  try {
    checks = (await systemApi.doctor()).checks || {};
  } catch (_) {
    return;
  }
  const sf = checks.stockfish || { ok: true };
  const arch = sf.arch || { suboptimal: false };

  if (!sf.ok) {
    showSetupBanner(
      banner,
      true,
      `<b>Stockfish engine not found.</b> ${escapeHtml(sf.hint || "Install Stockfish to analyze games.")}`,
      null
    );
  } else if (arch.suboptimal && localStorage.getItem("hideArchBanner") !== "1") {
    showArchFixBanner(banner);
  } else {
    banner.hidden = true;
  }
}

// Apple Silicon running the Intel Stockfish under Rosetta 2 (works, but slower for a search-heavy
// engine — the symptom of the old first-run install bug). Offer a one-click swap to the native
// arm64 build: POST /api/fix-stockfish-arch downloads it, pins it, and restarts the engine. Amber,
// dismissible (remembered so we don't nag).
function showArchFixBanner(banner) {
  banner.classList.remove("err");
  const dismiss = () => {
    banner.hidden = true;
    localStorage.setItem("hideArchBanner", "1");
  };
  banner.innerHTML =
    '<span class="sb-msg"><b>Stockfish is the Intel build running under Rosetta&nbsp;2.</b> ' +
    "It works, but the native Apple&nbsp;Silicon (arm64) engine is noticeably faster. " +
    '<button class="sb-fix" type="button">Download arm64 build</button></span>' +
    '<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>';
  banner.querySelector(".sb-x").addEventListener("click", dismiss);
  const fix = banner.querySelector(".sb-fix");
  fix.addEventListener("click", async () => {
    fix.disabled = true;
    fix.textContent = "Downloading…";
    let res;
    try {
      res = await systemApi.fixStockfishArch();
    } catch (_) {
      res = null;
    }
    if (res && res.ok) {
      banner.classList.remove("err");
      banner.innerHTML =
        '<span class="sb-msg"><b>Now using the native arm64 Stockfish.</b> Analyses will run faster.</span>' +
        '<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>';
      banner.querySelector(".sb-x").addEventListener("click", () => (banner.hidden = true));
    } else {
      fix.disabled = false;
      fix.textContent = "Retry";
      const msg = banner.querySelector(".sb-msg");
      if (msg && !msg.querySelector(".sb-err")) {
        const err = document.createElement("span");
        err.className = "sb-err";
        err.textContent = " " + ((res && res.error) || "Download failed — check your internet connection.");
        msg.appendChild(err);
      }
    }
  });
  banner.hidden = false;
}

function showSetupBanner(banner, isErr, msgHtml, onDismiss) {
  banner.classList.toggle("err", isErr);
  banner.innerHTML =
    `<span class="sb-msg">${msgHtml}</span>` +
    `<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>`;
  banner.querySelector(".sb-x").addEventListener("click", () => {
    banner.hidden = true;
    if (onDismiss) onDismiss();
  });
  banner.hidden = false;
}

// --- offline notice ------------------------------------------------------
// Hits /api/connectivity (cached server-side reachability probe). When there's no internet, the
// network-only Lichess and tablebase features will not work. Agent and explanation availability is
// reported independently by their backend capabilities.
async function checkOnline() {
  const banner = $("offline-banner");
  if (!banner) return;
  if (sessionStorage.getItem("hideOfflineBanner") === "1") return;
  let info;
  try {
    info = await systemApi.connectivity();
  } catch (_) {
    return; // can't even reach our own server — leave it to the page-load failure to be visible
  }
  if (!info || info.online !== false) return; // online (or unknown) — nothing to warn about

  let msg =
    "<b>You're offline.</b> Local analysis (Stockfish) works as normal, but " +
    "<b>Lichess game fetch</b> and the <b>endgame tablebase</b> need internet and won't be available.";
  msg += " Paste or upload a PGN to review a game.";
  showSetupBanner(banner, false, msg, () =>
    sessionStorage.setItem("hideOfflineBanner", "1")
  );
}

// --- update-available banner (app mode only) -----------------------------
// Hits /api/update-check (throttled GitHub release lookup). Non-blocking, dismissible notice when a
// newer release is out: info-blue for minor/patch, red for a major bump. Self-updatable installs
// (git/zip) get a one-click "Update now" (staged, applied by the launcher on the next reopen); the
// read-only .app gets a download link. Dismissal is remembered per version, so a newer release
// re-notifies. Fire-and-forget; failures are silent.
async function checkUpdates() {
  if (!isAppMode()) return; // only nag end-user app launches, never development sessions
  const banner = $("update-banner");
  if (!banner) return;
  let info;
  try {
    info = await systemApi.updateCheck();
  } catch (_) {
    return;
  }
  if (!info || !info.update_available || !info.latest) return;
  if (localStorage.getItem("hideUpdateBanner") === info.latest) return; // dismissed this version

  const major = info.severity === "major";
  banner.classList.remove("guide");
  banner.classList.toggle("err", major); // red for major, else the info-blue .update
  banner.classList.toggle("update", !major);
  const v = escapeHtml(info.latest);
  const lead = major ? `<b>Major update v${v} available.</b>` : `<b>Update available — v${v}.</b>`;
  // git/zip self-update in place; the read-only .app needs a guided manual download.
  const how = info.can_self_update
    ? "Click Update now, then reopen the app to finish. Your games &amp; settings are kept."
    : "A new version is ready.";
  const action = info.can_self_update
    ? `<button class="sb-btn" type="button" id="update-now">Update now</button>`
    : `<button class="sb-btn" type="button" id="update-guide">How to update</button>`;

  banner.innerHTML =
    `<span class="sb-msg">${lead} ${how}</span>` +
    action +
    dismissX();
  wireDismiss(banner, info);
  const now = banner.querySelector("#update-now");
  if (now) now.addEventListener("click", () => applyUpdate(now));
  const guide = banner.querySelector("#update-guide");
  if (guide) guide.addEventListener("click", () => showAppUpdateGuide(banner, info));
  banner.hidden = false;
}

function dismissX() {
  return `<button class="sb-x" type="button" aria-label="Dismiss" title="Dismiss">×</button>`;
}
function wireDismiss(banner, info) {
  banner.querySelector(".sb-x").addEventListener("click", () => {
    banner.hidden = true;
    localStorage.setItem("hideUpdateBanner", info.latest); // remember per version
  });
}

// The .app is read-only at runtime, so it can't self-update — expand the banner into step-by-step
// install instructions (incl. the one-time unsigned-app "Open" step) and reassure that data is kept.
function showAppUpdateGuide(banner, info) {
  const url = escapeHtml(info.release_url || "#");
  banner.classList.add("guide"); // full-width stacked layout
  banner.innerHTML =
    `<span class="sb-msg"><b>Update to v${escapeHtml(info.latest)}</b>` +
    `<ol class="sb-steps">` +
    `<li><a href="${url}" target="_blank" rel="noopener">Download the latest version</a> from the Releases page (the <code>…-macos.zip</code>).</li>` +
    `<li>Double-click the downloaded <code>.zip</code> to unzip it.</li>` +
    `<li>Drag <b>Chess Review Coach.app</b> into your <b>Applications</b> folder, replacing the old one. (Any location works — it doesn't have to be Applications.)</li>` +
    `<li>First open: right-click the app → <b>Open</b> → <b>Open</b> (macOS asks once because the app isn't Apple-signed).</li>` +
    `<li>That's it — your games, analysis, and settings carry over automatically.</li>` +
    `</ol></span>` +
    dismissX();
  wireDismiss(banner, info);
}

// Stage a one-click update: POST /api/apply-update writes a sentinel the launcher applies on the
// next start. We can't reliably relaunch from a browser tab, so we just tell the user to reopen.
async function applyUpdate(btn) {
  btn.disabled = true;
  btn.textContent = "Staging…";
  const msg = $("update-banner").querySelector(".sb-msg");
  let res = {};
  try {
    res = await systemApi.applyUpdate();
  } catch (_) {}
  if (res && res.ok) {
    if (msg) msg.innerHTML = "<b>Update staged.</b> Quit and reopen the app to finish updating.";
    btn.textContent = "Quit now";
    btn.disabled = false;
    btn.onclick = () => {
      try {
        systemApi.closing(); // app-liveness then shuts the server down
      } catch (_) {}
      window.close(); // works if the tab was script-opened; otherwise the message guides the user
    };
  } else {
    if (msg)
      msg.innerHTML =
        "<b>Couldn't stage the update.</b> " + escapeHtml((res && res.error) || "Try again later.");
    btn.textContent = "Update now";
    btn.disabled = false;
  }
}

// --- app mode: auto-open the latest Lichess game -------------------------
// App mode: let the server know when this tab is really gone, so it (and its terminal window) can
// quit. The reliable signal is `pagehide` → a close beacon; a slow heartbeat is just a backstop for
// the rare case pagehide never fires. We deliberately do NOT treat "lost focus / backgrounded" as
// closed — background tabs throttle timers, so a short heartbeat would false-quit during use.
let heartbeatTimer = null;
function startHeartbeat() {
  if (heartbeatTimer) return;
  const ping = () => systemApi.ping().catch(() => {});
  ping();
  heartbeatTimer = setInterval(ping, 15000); // backstop only; server tolerates minutes of silence
  // Fires on tab close, navigation, and refresh. sendBeacon delivers even as the page unloads.
  window.addEventListener("pagehide", () => {
    try {
      systemApi.closing();
    } catch (_) {}
  });
}

// App-mode empty board: first try the chess.com auto-sync (new games found -> the board shows the
// first of them while the rest analyze); else auto-load the configured user's latest game
// (Lichess, then chess.com); otherwise prompt for a username.

  return { checkSetup, checkOnline, checkUpdates, startHeartbeat };
}
