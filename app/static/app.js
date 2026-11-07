"use strict";

const STATUS_LABELS = {
  live: "Live",
  starting: "Tuning",
  standby: "On demand",
  retrying: "Retrying",
  stopped: "Off air",
};

const ICONS = {
  play: '<path d="M8 5.4v13.2L19 12 8 5.4Z" fill="currentColor"/>',
  pause:
    '<path d="M9 5v14M15 5v14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/>',
  edit:
    '<path d="m14.5 5.5 4 4M5 19l1-4.2L15.7 5a1.8 1.8 0 0 1 2.5 0l.8.8a1.8 1.8 0 0 1 0 2.5L9.2 18 5 19Z" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
  logs:
    '<path d="m5 7 4 4-4 4M12 17h7" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
  trash:
    '<path d="M5 7h14M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7m-8 0 .8 11a2 2 0 0 0 2 1.9h4.4a2 2 0 0 0 2-1.9L17 7M10 11v5M14 11v5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
  copy:
    '<rect x="8" y="8" width="11" height="11" rx="2.2" stroke="currentColor" stroke-width="1.7"/><path d="M16 8V6.2A2.2 2.2 0 0 0 13.8 4H6.2A2.2 2.2 0 0 0 4 6.2v7.6A2.2 2.2 0 0 0 6.2 16H8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
  spinner:
    '<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="2" opacity=".22"/><path d="M20.5 12a8.5 8.5 0 0 0-8.5-8.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  retry:
    '<path d="M19 8a8 8 0 1 0 1.5 6M19 4v4h-4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
  check:
    '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="m8 12.3 2.6 2.6L16 9.5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
  alert:
    '<path d="M12 8v5m0 3.2v.1M10.3 4.5 2.9 17.4A2 2 0 0 0 4.6 20h14.8a2 2 0 0 0 1.7-2.6L13.7 4.5a2 2 0 0 0-3.4 0Z" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
  info: '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7"/><path d="M12 11v6M12 7.2v.1" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
};

const state = {
  stations: [],
  playingId: null,
  audio: null,
  busy: new Set(),
  editingId: null,
  logsId: null,
  logsTimer: null,
  loading: false,
  reloadRequested: false,
  loadErrorShown: false,
  pendingImport: null,
};

const els = {};

const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

function icon(name, extraClass = "") {
  const className = `icon${extraClass ? ` ${extraClass}` : ""}`;
  return `<svg class="${className}" viewBox="0 0 24 24" fill="none" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

function cacheElements() {
  const ids = [
    "engine-chip",
    "engine-label",
    "engine-banner",
    "engine-banner-text",
    "stat-live",
    "stat-listeners",
    "stat-listeners-label",
    "stat-stations",
    "stat-stations-label",
    "station-grid",
    "station-count",
    "empty-state",
    "open-add",
    "empty-add",
    "station-dialog",
    "station-form",
    "station-dialog-title",
    "station-name",
    "station-url",
    "station-codec",
    "codec-hint",
    "station-mode",
    "mode-hint",
    "station-autostart",
    "autostart-wrap",
    "autostart-label",
    "station-submit",
    "form-error",
    "logs-dialog",
    "logs-dialog-title",
    "log-status",
    "log-view",
    "logs-refresh",
    "logs-copy",
    "confirm-dialog",
    "confirm-title",
    "confirm-message",
    "confirm-ok",
    "confirm-cancel",
    "import-stations",
    "export-stations",
    "import-file",
    "import-dialog",
    "import-form",
    "import-summary",
    "import-replace",
    "import-warning",
    "import-error",
    "import-submit",
    "toast-host",
  ];
  for (const id of ids) {
    els[id.replace(/-([a-z])/g, (_, char) => char.toUpperCase())] =
      document.getElementById(id);
  }
}

function init() {
  cacheElements();

  els.openAdd.addEventListener("click", openAddDialog);
  els.emptyAdd.addEventListener("click", openAddDialog);
  els.stationGrid.addEventListener("click", onGridClick);
  els.stationForm.addEventListener("submit", onStationSubmit);
  els.stationMode.addEventListener("change", updateModeUI);
  els.stationCodec.addEventListener("change", updateCodecUI);
  els.importStations.addEventListener("click", () => els.importFile.click());
  els.exportStations.addEventListener("click", exportStations);
  els.importFile.addEventListener("change", onImportFileSelected);
  els.importForm.addEventListener("submit", onImportSubmit);
  els.importReplace.addEventListener("change", updateImportUI);
  els.logsRefresh.addEventListener("click", () => fetchLogs(true));
  els.logsCopy.addEventListener("click", () => copyText(els.logView.textContent, "Logs copied"));
  els.confirmCancel.addEventListener("click", () => els.confirmDialog.close());
  els.confirmOk.addEventListener("click", () => {
    els.confirmDialog.returnValue = "confirm";
    els.confirmDialog.close();
  });

  for (const button of document.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => {
      const dialog = button.closest("dialog");
      if (dialog) dialog.close();
    });
  }

  for (const dialog of document.querySelectorAll("dialog.modal")) {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }

  els.stationDialog.addEventListener("close", () => {
    els.formError.hidden = true;
    setFormBusy(false);
  });

  els.logsDialog.addEventListener("close", stopLogsPolling);
  els.importDialog.addEventListener("close", () => {
    state.pendingImport = null;
    els.importError.hidden = true;
    els.importFile.value = "";
    els.importSubmit.disabled = false;
    els.importSubmit.textContent = "Import";
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      loadStations();
      checkHealth();
    }
  });

  loadStations();
  checkHealth();
  window.setInterval(() => {
    if (!document.hidden) loadStations();
  }, 4000);
  window.setInterval(() => {
    if (!document.hidden) checkHealth();
  }, 10000);
}

async function api(path, options = {}) {
  const { headers: extraHeaders, ...rest } = options;
  const headers = { "X-Requested-With": "yt-radio", ...(extraHeaders || {}) };
  if (rest.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...rest,
    headers,
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") {
        message = payload.detail;
      } else if (Array.isArray(payload.detail)) {
        message = payload.detail
          .map((item) => String(item.msg || item).replace(/^Value error,\s*/i, ""))
          .join(" ");
      }
    } catch {
      // Keep the generic message for non-JSON errors.
    }
    throw new Error(message);
  }

  if (response.status === 204) return null;
  return response.json();
}

async function loadStations() {
  if (state.loading) {
    state.reloadRequested = true;
    return;
  }
  state.loading = true;
  try {
    const stations = await api("/api/stations");
    state.stations = Array.isArray(stations) ? stations : [];
    state.loadErrorShown = false;
    render();
  } catch (error) {
    if (!state.loadErrorShown) {
      toast(error.message, "error");
      state.loadErrorShown = true;
      window.setTimeout(() => {
        state.loadErrorShown = false;
      }, 15000);
    }
  } finally {
    state.loading = false;
    if (state.reloadRequested) {
      state.reloadRequested = false;
      loadStations();
    }
  }
}

async function checkHealth() {
  try {
    const health = await api("/healthz");
    setEngine(Boolean(health.icecast), health.error);
  } catch {
    setEngine(false, "The control server is unreachable.");
  }
}

function setEngine(online, error) {
  els.engineChip.dataset.state = online ? "online" : "offline";
  els.engineLabel.textContent = online ? "Engine online" : "Engine offline";
  els.engineBanner.hidden = online;
  if (!online) {
    els.engineBannerText.textContent =
      error || "Stations will reconnect automatically once the stream engine is available.";
  }
}

async function exportStations() {
  try {
    const data = await api("/api/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    link.href = url;
    link.download = `yt-radio-stations-${stamp}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Streams exported", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function onImportFileSelected(event) {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;

  try {
    const data = JSON.parse(await file.text());
    if (!data || !Array.isArray(data.stations)) {
      throw new Error("That file is not a YT Radio stream export.");
    }
    state.pendingImport = data;
    const count = data.stations.length;
    els.importSummary.textContent = `${count} ${
      count === 1 ? "station" : "stations"
    } found in ${file.name}.`;
    els.importReplace.checked = false;
    els.importError.hidden = true;
    updateImportUI();
    showDialog(els.importDialog);
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateImportUI() {
  const replace = els.importReplace.checked;
  els.importWarning.hidden = !replace;
  els.importSubmit.textContent = replace ? "Replace & import" : "Import";
}

async function onImportSubmit(event) {
  event.preventDefault();
  if (!state.pendingImport) return;

  const replace = els.importReplace.checked;
  els.importSubmit.disabled = true;
  els.importSubmit.textContent = "Importing…";
  els.importError.hidden = true;
  try {
    const result = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ ...state.pendingImport, replace }),
    });
    els.importDialog.close();
    const count = Number(result.imported) || 0;
    toast(`Imported ${count} ${count === 1 ? "station" : "stations"}`, "success");
    await loadStations();
  } catch (error) {
    els.importError.textContent = error.message;
    els.importError.hidden = false;
    els.importSubmit.disabled = false;
    updateImportUI();
  }
}

function render() {
  const active = document.activeElement;
  const activeCard = active && active.closest ? active.closest(".station-card") : null;
  const focusState =
    activeCard && active.dataset && active.dataset.action
      ? { id: activeCard.dataset.id, action: active.dataset.action }
      : null;

  if (state.playingId !== null) {
    const playing = state.stations.find((station) => station.id === state.playingId);
    if (!playing || playing.status === "stopped") {
      pausePlayback(false);
    }
  }

  const live = state.stations.filter((station) => station.status === "live");
  const listeners = state.stations.reduce(
    (total, station) => total + (Number(station.listeners) || 0),
    0,
  );
  els.statLive.textContent = String(live.length);
  els.statListeners.textContent = String(listeners);
  els.statStations.textContent = String(state.stations.length);
  els.statListenersLabel.textContent = listeners === 1 ? "listener" : "listeners";
  els.statStationsLabel.textContent =
    state.stations.length === 1 ? "station" : "stations";
  els.stationCount.textContent = `${state.stations.length} ${
    state.stations.length === 1 ? "station" : "stations"
  }`;
  els.emptyState.hidden = state.stations.length > 0;
  els.stationGrid.innerHTML = state.stations.map(stationCard).join("");

  if (focusState) {
    const restored = els.stationGrid.querySelector(
      `.station-card[data-id="${focusState.id}"] [data-action="${focusState.action}"]`,
    );
    if (restored) restored.focus({ preventScroll: true });
  }
}

function stationCard(station) {
  const status = Object.hasOwn(STATUS_LABELS, station.status)
    ? station.status
    : "stopped";
  const playing = state.playingId === station.id;
  const busy = state.busy.has(station.id);
  const streamUrl = `${window.location.origin}${station.stream_path}`;
  const displayUrl = streamUrl.replace(/^https?:\/\//, "");
  const name = escapeHtml(station.name);
  const initial = escapeHtml(Array.from(String(station.name).trim())[0] || "?");
  const meta = metaText(station, status);
  const codec = station.codec === "aac_he" ? "aac_he" : "aac";
  const codecLabel = codec === "aac_he" ? "HE-AAC 49k" : "AAC copy";
  const formatLine = `${codecLabel}${
    station.on_demand ? ' <span class="dot-sep">·</span> On demand' : ""
  }`;
  const error =
    station.last_error &&
    status !== "live" &&
    status !== "stopped" &&
    status !== "standby"
      ? `<p class="card-error" title="${escapeHtml(station.last_error)}">${escapeHtml(
          station.last_error,
        )}</p>`
      : "";
  const active = status !== "stopped";

  const primary = primaryAction(station, status, playing, busy);
  const stopButton = active
    ? iconButton(
        "stop",
        station.on_demand ? "Disable station" : "Stop station",
        "stop",
        busy,
      )
    : "";

  return `
    <article class="station-card status-${status}${busy ? " is-busy" : ""}" data-id="${station.id}" aria-labelledby="station-title-${station.id}">
      <span class="card-glow" aria-hidden="true"></span>
      <div class="station-head">
        <div class="station-id">
          <span class="station-art art-${station.id % 6}" aria-hidden="true">${initial}</span>
          <div class="station-titles">
            <h3 id="station-title-${station.id}" title="${name}">${name}</h3>
            <p>${formatLine}</p>
          </div>
        </div>
        ${statusPill(status)}
      </div>
      <div class="station-live-row">
        <div class="eq${status === "live" ? " is-live" : ""}" aria-hidden="true">
          <span></span><span></span><span></span><span></span>
        </div>
        <span class="station-meta-text">${meta}</span>
      </div>
      <div class="url-row">
        <code title="${escapeHtml(streamUrl)}">${escapeHtml(displayUrl)}</code>
        <button type="button" class="icon-btn" data-action="copy" title="Copy stream URL" aria-label="Copy stream URL">${icon(
          "copy",
        )}</button>
      </div>
      ${error}
      <div class="station-actions">
        ${primary}
        ${stopButton}
        ${iconButton("edit", "Edit station", "edit", busy)}
        ${iconButton("logs", "View logs", "logs", busy)}
        ${iconButton("delete", "Delete station", "trash", busy, "danger")}
      </div>
    </article>
  `;
}

function statusPill(status) {
  const label = STATUS_LABELS[status] || "Off air";
  const marker =
    status === "starting"
      ? '<span class="status-spinner"></span>'
      : '<span class="status-dot"></span>';
  return `<span class="status status-${status}">${marker}${label}</span>`;
}

function primaryAction(station, status, playing, busy) {
  const disabled = busy ? " disabled" : "";
  if (playing) {
    return `<button type="button" class="btn btn-primary" data-action="pause"${disabled}>${icon(
      "pause",
    )}<span>Pause</span></button>`;
  }
  if (status === "live" || status === "standby") {
    return `<button type="button" class="btn btn-primary" data-action="listen"${disabled}>${icon(
      "play",
    )}<span>Listen</span></button>`;
  }
  if (status === "starting") {
    return `<button type="button" class="btn btn-primary" disabled>${icon(
      "spinner",
      "spin",
    )}<span>${station.on_demand ? "Tuning…" : "Tuning in…"}</span></button>`;
  }
  if (status === "retrying") {
    return `<button type="button" class="btn btn-primary" disabled>${icon(
      "retry",
    )}<span>Retrying…</span></button>`;
  }
  return `<button type="button" class="btn btn-primary" data-action="start"${disabled}>${icon(
    "play",
  )}<span>${station.on_demand ? "Enable" : "Start"}</span></button>`;
}

function iconButton(action, label, iconName, disabled, extraClass = "") {
  return `<button type="button" class="icon-btn${
    extraClass ? ` ${extraClass}` : ""
  }" data-action="${action}" title="${label}" aria-label="${label}"${
    disabled ? " disabled" : ""
  }>${icon(iconName)}</button>`;
}

function metaText(station, status) {
  if (status === "live") {
    const count = Number(station.listeners) || 0;
    const connections = Number(station.connections) || 0;
    const idleSeconds = Number(station.idle_stop_in_seconds);
    if (
      station.on_demand &&
      connections === 0 &&
      Number.isFinite(idleSeconds) &&
      station.idle_stop_in_seconds !== null
    ) {
      return `No listeners <span class="dot-sep">·</span> stops in ${idleSeconds}s`;
    }
    let listenerText =
      count === 0 ? "No listeners" : `${count} ${count === 1 ? "listener" : "listeners"}`;
    if (connections > count) {
      listenerText += ` <span class="dot-sep">·</span> ${connections} connections`;
    }
    return `${listenerText} <span class="dot-sep">·</span> live ${formatUptime(
      station.uptime_seconds,
    )}`;
  }
  if (status === "standby") {
    return "Starts when the first listener connects";
  }
  if (status === "starting") {
    return station.on_demand ? "Tuning on demand…" : "Tuning in to YouTube…";
  }
  if (status === "retrying") {
    const seconds = Number(station.retry_in_seconds);
    return Number.isFinite(seconds) && seconds >= 0
      ? `Next attempt in ${seconds}s`
      : "Waiting to reconnect…";
  }
  return station.on_demand ? "Disabled" : "Off air";
}

function formatUptime(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  if (total < 60) return `${Math.floor(total)}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function onGridClick(event) {
  const actionElement = event.target.closest("[data-action]");
  if (!actionElement) return;
  const card = actionElement.closest(".station-card");
  if (!card) return;
  const id = Number(card.dataset.id);
  const station = state.stations.find((item) => item.id === id);
  if (!station) return;
  handleAction(actionElement.dataset.action, station);
}

async function handleAction(action, station) {
  switch (action) {
    case "copy":
      await copyText(
        `${window.location.origin}${station.stream_path}`,
        "Stream URL copied",
      );
      return;
    case "listen":
      playStation(station);
      return;
    case "pause":
      pausePlayback();
      return;
    case "edit":
      openEditDialog(station);
      return;
    case "logs":
      openLogs(station);
      return;
    case "delete":
      await deleteStation(station);
      return;
    case "start":
      await stationCommand(
        station,
        "start",
        station.on_demand ? "On-demand station enabled" : "Station starting",
      );
      return;
    case "stop":
      if (state.playingId === station.id) pausePlayback(false);
      await stationCommand(
        station,
        "stop",
        station.on_demand ? "On-demand station disabled" : "Station stopped",
      );
      return;
    default:
      return;
  }
}

async function stationCommand(station, command, successMessage) {
  if (state.busy.has(station.id)) return;
  state.busy.add(station.id);
  render();
  try {
    await api(`/api/stations/${station.id}/${command}`, { method: "POST" });
    toast(successMessage, "success");
    await loadStations();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy.delete(station.id);
    render();
  }
}

async function deleteStation(station) {
  const confirmed = await askConfirm(
    "Delete station?",
    `“${station.name}” will be removed and its stream will stop immediately. This cannot be undone.`,
    "Delete station",
  );
  if (!confirmed) return;

  if (state.playingId === station.id) pausePlayback(false);
  state.busy.add(station.id);
  render();
  try {
    await api(`/api/stations/${station.id}`, { method: "DELETE" });
    state.stations = state.stations.filter((item) => item.id !== station.id);
    toast("Station deleted", "success");
    render();
    await loadStations();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.busy.delete(station.id);
    render();
  }
}

function playStation(station) {
  if (state.playingId === station.id) {
    pausePlayback();
    return;
  }

  pausePlayback(false);
  const audio = new Audio();
  audio.preload = "none";
  state.audio = audio;
  state.playingId = station.id;
  let failed = false;

  const fail = () => {
    if (failed || state.audio !== audio) return;
    failed = true;
    toast("Could not open the stream yet. Check that the station is live.", "error");
    pausePlayback();
  };

  audio.addEventListener("playing", () => {
    if (state.audio === audio) {
      state.playingId = station.id;
      render();
    }
  });
  audio.addEventListener("error", fail);
  audio.src = `${window.location.origin}${station.stream_path}?t=${Date.now()}`;
  audio.play().catch(fail);
  render();
  window.setTimeout(() => loadStations(), 1200);
}

function pausePlayback(renderAfter = true) {
  if (state.audio) {
    const audio = state.audio;
    state.audio = null;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }
  state.playingId = null;
  if (renderAfter) render();
}

function openAddDialog() {
  state.editingId = null;
  els.stationForm.reset();
  els.stationCodec.value = "aac";
  els.stationMode.value = "always";
  els.autostartWrap.hidden = false;
  els.stationAutostart.checked = true;
  updateCodecUI();
  updateModeUI();
  els.stationDialogTitle.textContent = "Add a station";
  els.formError.hidden = true;
  setFormBusy(false);
  showDialog(els.stationDialog);
  window.setTimeout(() => els.stationName.focus(), 40);
}

function openEditDialog(station) {
  state.editingId = station.id;
  els.stationName.value = station.name;
  els.stationUrl.value = station.url;
  els.stationCodec.value = station.codec === "aac_he" ? "aac_he" : "aac";
  els.stationMode.value = station.on_demand ? "on_demand" : "always";
  els.autostartWrap.hidden = true;
  updateCodecUI();
  updateModeUI();
  els.stationDialogTitle.textContent = "Edit station";
  els.formError.hidden = true;
  setFormBusy(false);
  showDialog(els.stationDialog);
  window.setTimeout(() => els.stationName.focus(), 40);
}

function updateCodecUI() {
  const codec = els.stationCodec.value;
  const onDemandOption = els.stationMode.querySelector(
    'option[value="on_demand"]',
  );
  if (codec === "aac_he") {
    onDemandOption.disabled = true;
    els.stationMode.value = "always";
    els.codecHint.textContent =
      "HE-AAC 49k is copied from YouTube without re-encoding. Always-on only; the stream URL ends in .aac.";
  } else {
    onDemandOption.disabled = false;
    els.codecHint.textContent =
      "AAC-LC is copied from YouTube without re-encoding (about 130 kbps). Always-on or on-demand; the stream URL ends in .aac.";
  }
  updateModeUI();
}

function updateModeUI() {
  if (els.stationCodec.value === "aac_he") {
    els.stationMode.value = "always";
  }
  const onDemand = els.stationMode.value === "on_demand";
  els.autostartLabel.textContent = onDemand
    ? "Enable for listeners"
    : "Start streaming now";
  els.modeHint.textContent = onDemand
    ? "Starts when the first listener connects; stops after the last listener leaves."
    : "Always-on stations stream even when nobody is listening.";
}

function showDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

function setFormBusy(busy) {
  els.stationSubmit.disabled = busy;
  const label =
    state.editingId === null
      ? "Add station"
      : "Save changes";
  els.stationSubmit.textContent = busy ? "Saving…" : label;
}

async function onStationSubmit(event) {
  event.preventDefault();
  if (!els.stationForm.reportValidity()) return;

  const payload = {
    name: els.stationName.value.trim(),
    url: els.stationUrl.value.trim(),
    codec: els.stationCodec.value,
    on_demand:
      els.stationCodec.value === "aac_he"
        ? false
        : els.stationMode.value === "on_demand",
  };
  const editingId = state.editingId;
  if (editingId === null) {
    payload.autostart = els.stationAutostart.checked;
  }

  els.formError.hidden = true;
  setFormBusy(true);
  try {
    if (editingId === null) {
      await api("/api/stations", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast("Station added", "success");
    } else {
      await api(`/api/stations/${editingId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      toast("Station updated", "success");
    }
    els.stationDialog.close();
    await loadStations();
  } catch (error) {
    els.formError.textContent = error.message;
    els.formError.hidden = false;
  } finally {
    setFormBusy(false);
  }
}

function openLogs(station) {
  state.logsId = station.id;
  els.logsDialogTitle.textContent = `${station.name} — logs`;
  els.logStatus.textContent = "Loading…";
  els.logView.textContent = "";
  showDialog(els.logsDialog);
  fetchLogs(true);
  stopLogsTimerOnly();
  state.logsTimer = window.setInterval(() => fetchLogs(false), 3000);
}

function stopLogsTimerOnly() {
  if (state.logsTimer !== null) {
    window.clearInterval(state.logsTimer);
    state.logsTimer = null;
  }
}

function stopLogsPolling() {
  stopLogsTimerOnly();
  state.logsId = null;
}

async function fetchLogs(forceScroll) {
  if (state.logsId === null) return;
  const id = state.logsId;
  try {
    const data = await api(`/api/stations/${id}/logs`);
    if (state.logsId !== id) return;
    const lines =
      data.lines && data.lines.length
        ? data.lines.join("\n")
        : "No output yet. Start the station to see yt-dlp and FFmpeg output.";
    const nearBottom =
      els.logView.scrollTop + els.logView.clientHeight >=
      els.logView.scrollHeight - 24;
    if (els.logView.textContent !== lines) {
      els.logView.textContent = lines;
    }
    const label = STATUS_LABELS[data.status] || data.status || "Unknown";
    els.logStatus.textContent = `${label} · updates every 3s`;
    if (forceScroll || nearBottom) {
      els.logView.scrollTop = els.logView.scrollHeight;
    }
  } catch (error) {
    els.logStatus.textContent = error.message;
  }
}

function askConfirm(title, message, confirmLabel) {
  els.confirmTitle.textContent = title;
  els.confirmMessage.textContent = message;
  els.confirmOk.textContent = confirmLabel;
  els.confirmDialog.returnValue = "";
  return new Promise((resolve) => {
    const onClose = () => {
      els.confirmDialog.removeEventListener("close", onClose);
      resolve(els.confirmDialog.returnValue === "confirm");
    };
    els.confirmDialog.addEventListener("close", onClose);
    showDialog(els.confirmDialog);
    els.confirmCancel.focus();
  });
}

async function copyText(text, successMessage) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopy(text);
    }
    toast(successMessage, "success");
  } catch {
    toast("Could not copy automatically. Select the text and copy it manually.", "error");
  }
}

function fallbackCopy(text) {
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.className = "offscreen";
  document.body.append(helper);
  helper.select();
  const copied = document.execCommand("copy");
  helper.remove();
  if (!copied) throw new Error("Copy failed");
}

function toast(message, kind = "info") {
  const node = document.createElement("div");
  node.className = `toast toast-${kind}`;
  const iconWrap = document.createElement("span");
  iconWrap.className = "toast-icon";
  iconWrap.innerHTML = icon(kind === "success" ? "check" : kind === "error" ? "alert" : "info");
  const text = document.createElement("span");
  text.textContent = message;
  node.append(iconWrap, text);

  while (els.toastHost.children.length >= 4) {
    els.toastHost.firstElementChild.remove();
  }
  els.toastHost.append(node);

  window.setTimeout(() => {
    node.classList.add("is-leaving");
    window.setTimeout(() => node.remove(), 240);
  }, 4200);
}

document.addEventListener("DOMContentLoaded", init);
