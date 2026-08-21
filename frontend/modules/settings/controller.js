import { gamesApi } from "../api/games.js";
import { byId } from "../core/dom.js";

export function createSettingsController({ isPuzzleMode, onSaved }) {
  const $ = byId;

const PROFILE_PRESETS = {
  balanced: {
    recent: "100", lifetime: "all",
    hint: "Coaches on your last 100 games for current form, plus your whole history for long-term patterns.",
  },
  recent: {
    recent: "100", lifetime: "0",
    hint: "Focuses only on your last 100 games; older games are ignored.",
  },
  all: {
    recent: "0", lifetime: "all",
    hint: "Weighs every game you've played equally, recent or old.",
  },
};

// The two Advanced number fields are the source of truth; the dropdown is a convenience that
// fills them. Match the current field values to a preset (or "custom" for any other combination).
function profileModeFromFields() {
  const r = $("set-recent").value.trim();
  const l = $("set-lifetime").value.trim().toLowerCase() || "all"; // blank lifetime == "all"
  for (const [mode, p] of Object.entries(PROFILE_PRESETS)) {
    if (r === p.recent && l === p.lifetime) return mode;
  }
  return "custom";
}

// Picking a named preset writes its windows into the Advanced fields.
function applyProfilePreset() {
  const p = PROFILE_PRESETS[$("set-profile-mode").value];
  if (p) {
    $("set-recent").value = p.recent;
    $("set-lifetime").value = p.lifetime;
  }
  updateProfileHint();
}

// Editing a window by hand flips the dropdown to the matching preset (or "Custom").
function syncProfileModeFromFields() {
  $("set-profile-mode").value = profileModeFromFields();
  updateProfileHint();
}

function updateProfileHint() {
  const mode = $("set-profile-mode").value;
  $("set-profile-hint").textContent = PROFILE_PRESETS[mode]
    ? PROFILE_PRESETS[mode].hint
    : "Using your own window sizes from the fields below.";
}

// Mistake sensitivity: a checkbox auto-scales it to each game's PGN rating (default on). Unchecking
// reveals a slider whose value is a representative Elo (stored as `player_elo`; blank = Auto). The
// tier label tracks the same casual/intermediate/advanced/master bands the backend tunes against.
const SKILL_DEFAULT_ELO = "1200"; // where the slider starts when first switching to manual

// Named levels along the slider. Each entry is [minimum Elo, label]; the label shown is the highest
// tier whose minimum the value has reached. Anchors: Beginner 400, Intermediate 1200, Advanced 1800,
// Master 2300, with in-between levels so every slider stop reads as a recognisable strength.
const SKILL_TIERS = [
  [400, "Beginner"],
  [700, "Novice"],
  [1000, "Casual"],
  [1200, "Intermediate"],
  [1500, "Club player"],
  [1800, "Advanced"],
  [2100, "Expert"],
  [2300, "Master"],
];

function eloTier(elo) {
  let label = SKILL_TIERS[0][1];
  for (const [min, name] of SKILL_TIERS) {
    if (elo >= min) label = name;
  }
  return label;
}

// Show/hide the "how many recent games to check" field to match the auto-sync checkbox.
function updateChesscomSyncUI() {
  $("chesscom-sync-max-field").hidden = !$("set-chesscom-sync").checked;
}

// Show/hide the slider to match the checkbox and refresh the readout.
function updateSkillUI() {
  const auto = $("set-skill-auto").checked;
  $("skill-manual").hidden = auto;
  if (!auto) {
    const elo = parseInt($("set-elo").value, 10);
    $("set-elo-label").textContent = `${eloTier(elo)} · ~${elo} Elo`;
  }
}

async function openSettings() {
  $("settings-status").textContent = "";
  let data;
  try {
    data = await gamesApi.settings();
  } catch (_) {
    $("settings-status").textContent = "Could not load settings.";
    $("settings").hidden = false;
    return;
  }
  const s = data.settings || {};
  $("set-username").value = s.username || "";
  $("set-chesscom").value = s.chesscom_username || "";
  $("set-chesscom-sync").checked = s.chesscom_sync !== false; // auto-sync new games (default on)
  $("set-chesscom-sync-max").value = s.chesscom_sync_max || "5";
  updateChesscomSyncUI();
  $("set-aliases").value = s.aliases || "";
  $("set-token").value = s.lichess_token || "";
  // Coaching memory: load the raw windows into the Advanced fields, then point the
  // dropdown at whichever preset they match (or "Custom" for any other combination).
  $("set-recent").value = s.profile_recent || "";
  $("set-lifetime").value = s.profile_lifetime || "";
  syncProfileModeFromFields();
  // Mistake sensitivity: a stored rating means manual (slider shown); blank means auto-scale.
  const elo = (s.player_elo || "").trim();
  $("set-skill-auto").checked = !elo;
  $("set-elo").value = elo || SKILL_DEFAULT_ELO;
  updateSkillUI();
  $("set-stockfish").value = s.stockfish_path || "";
  $("set-local-llm-url").value = s.local_llm_base_url || "";
  $("set-local-llm-model").value = s.local_llm_model || "";
  $("set-review-side").value = s.default_review_side || "auto";
  $("set-board-orientation").value = s.board_orientation || "review";
  $("set-analysis-preset").value = s.analysis_preset || "balanced";
  $("set-explanation-provider").value = s.explanation_provider || "auto";
  $("set-explanation-language").value = s.explanation_language || "zh-CN";
  $("set-show-threats").checked = s.show_threat_arrows === true;
  $("set-data-dir").textContent = data.data_dir || "—";
  const engineConfig = data.engine || {};
  $("set-engine-details").textContent =
    `Threads ${engineConfig.threads ?? "—"} · Hash ${engineConfig.hash_mb ?? "—"} MB · ` +
    `base scan depth ${engineConfig.scan_depth ?? "—"} · deep depth ${engineConfig.deep_depth ?? "—"}`;
  $("set-ollama-status").textContent = "";
  $("set-ollama-pick-row").hidden = true; // picker only appears after a successful Detect
  $("set-coach-ai-auto").checked = !!s.coach_ai_auto; // auto-generate per game (default off)
  $("set-coach-ai-persist").checked = s.coach_ai_persist !== false; // remember summaries (default on)
  $("set-personalize").checked = s.personalize_history !== false; // personalize chat (default on)
  $("set-puzzle-animations").checked = s.puzzle_animations !== false; // solve animations (default on)
  $("set-puzzle-auto-advance").checked = s.puzzle_auto_advance === true; // auto-next after solve (default off)
  $("set-puzzle-interleave").checked = s.puzzle_mistake_interleave !== false; // mix in own-game mistakes (default on)
  $("set-sf-status").textContent = data.stockfish_ok
    ? "Stockfish engine found ✓"
    : "Stockfish not found — analysis won't run until this points at the engine.";
  // Open on the tab most relevant to what the user is doing (Puzzles while solving, else Account).
  activateSettingsTab(isPuzzleMode() ? "puzzles" : "account");
  $("settings").hidden = false;
  if (!isPuzzleMode()) $("set-username").focus();
}

// Switch the Settings panel to one category tab.
function activateSettingsTab(name) {
  document
    .querySelectorAll(".set-tab-btn")
    .forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document
    .querySelectorAll(".set-panel")
    .forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
}

// One-click Ollama setup: fill in the default URL if blank, ask the backend what models Ollama
// has pulled, populate the model picker, and auto-select the first one if none is chosen yet.
async function detectOllama() {
  const status = $("set-ollama-status");
  status.textContent = "Looking for Ollama…";
  const url = $("set-local-llm-url").value.trim();
  let data;
  try {
    data = await gamesApi.ollamaModels(url);
  } catch (_) {
    status.textContent = "Could not reach the server.";
    return;
  }
  if (!data.ok) {
    status.textContent = data.error || "No Ollama found.";
    return;
  }
  if (!url) $("set-local-llm-url").value = data.base_url; // adopt the URL we found it at
  const sel = $("set-ollama-model-select");
  sel.innerHTML = "";
  for (const name of data.models) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  if (!data.models.length) {
    $("set-ollama-pick-row").hidden = true;
    status.textContent = "Ollama is running but has no models. Pull one: ollama pull qwen2.5-coder";
    return;
  }
  // Show the picker; keep the existing model if it's one Ollama has, else default to the first.
  $("set-ollama-pick-row").hidden = false;
  const current = $("set-local-llm-model").value.trim();
  const chosen = data.models.includes(current) ? current : data.models[0];
  sel.value = chosen;
  $("set-local-llm-model").value = chosen;
  status.textContent = `Found ${data.models.length} model${data.models.length === 1 ? "" : "s"} ✓ — pick one and Save.`;
}

async function saveSettings(e) {
  e.preventDefault();
  $("settings-status").textContent = "Saving…";
  const patch = {
    username: $("set-username").value.trim(),
    chesscom_username: $("set-chesscom").value.trim(),
    chesscom_sync: $("set-chesscom-sync").checked,
    chesscom_sync_max: $("set-chesscom-sync-max").value.trim(),
    aliases: $("set-aliases").value.trim(),
    lichess_token: $("set-token").value.trim(),
    stockfish_path: $("set-stockfish").value.trim(),
    local_llm_base_url: $("set-local-llm-url").value.trim(),
    local_llm_model: $("set-local-llm-model").value.trim(),
    coach_ai_auto: $("set-coach-ai-auto").checked,
    coach_ai_persist: $("set-coach-ai-persist").checked,
    personalize_history: $("set-personalize").checked,
    puzzle_animations: $("set-puzzle-animations").checked,
    puzzle_auto_advance: $("set-puzzle-auto-advance").checked,
    puzzle_mistake_interleave: $("set-puzzle-interleave").checked,
    default_review_side: $("set-review-side").value,
    board_orientation: $("set-board-orientation").value,
    analysis_preset: $("set-analysis-preset").value,
    explanation_provider: $("set-explanation-provider").value,
    explanation_language: $("set-explanation-language").value,
    show_threat_arrows: $("set-show-threats").checked,
  };
  // The Advanced fields are the source of truth (the dropdowns just fill them).
  patch.profile_recent = $("set-recent").value.trim();
  patch.profile_lifetime = $("set-lifetime").value.trim();
  // Auto-scale on -> blank (read each game's Elo); off -> the slider's chosen Elo.
  patch.player_elo = $("set-skill-auto").checked ? "" : $("set-elo").value.trim();
  let res;
  try {
    res = await gamesApi.saveSettings(patch);
  } catch (_) {
    $("settings-status").textContent = "Could not save settings.";
    return;
  }
  if (res.error) {
    $("settings-status").textContent = res.error;
    return;
  }
  $("settings").hidden = true;
  onSaved(res.settings || {});
}


  function mount() {
    $("settings-toggle").addEventListener("click", openSettings);
    $("settings-cancel").addEventListener("click", () => ($("settings").hidden = true));
    $("settings-form").addEventListener("submit", saveSettings);
    document
      .querySelectorAll(".set-tab-btn")
      .forEach((button) => button.addEventListener("click", () => activateSettingsTab(button.dataset.tab)));
    $("set-profile-mode").addEventListener("change", applyProfilePreset);
    $("set-recent").addEventListener("input", syncProfileModeFromFields);
    $("set-lifetime").addEventListener("input", syncProfileModeFromFields);
    $("set-skill-auto").addEventListener("change", updateSkillUI);
    $("set-chesscom-sync").addEventListener("change", updateChesscomSyncUI);
    $("set-elo").addEventListener("input", updateSkillUI);
    $("set-ollama-detect").addEventListener("click", detectOllama);
    $("set-ollama-model-select").addEventListener("change", (event) => {
      $("set-local-llm-model").value = event.target.value;
    });
  }

  return { mount, open: openSettings };
}
