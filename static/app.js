const state = {
  view: "review",
  status: "pending",
  albums: [],
  total: 0,
  currentIndex: -1,
  currentAlbum: null,
  candidates: [],
  selectedCandidate: null,
  minSize: 1000,
  candidateChecks: { total: 0, completed: 0, accepted: 0, filtered: 0, failed: 0 },
  candidateSearchController: null,
  candidateGeneration: 0,
  dimensionQueue: [],
  activeDimensionChecks: 0,
  searchPartial: false,
  coverPreloads: new Map(),
  scanTimer: null,
  filterTimer: null,
  batchItems: [],
  batchTimer: null,
  backgroundTimer: null,
  batchApplyTimer: null,
  lastBackgroundProcessed: -1,
};

const el = (id) => document.getElementById(id);

function currentCoverUrl(album) {
  const version = encodeURIComponent(album.cover_version || "0");
  return `/api/albums/${album.id}/current?v=${version}`;
}

function updateActiveAlbumItem() {
  document.querySelectorAll(".album-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.albumId === state.currentAlbum?.id);
  });
}

function preloadAdjacentAlbumCovers(index) {
  [index - 1, index + 1].forEach((candidateIndex) => {
    const album = state.albums[candidateIndex];
    if (!album?.has_current) return;
    const url = currentCoverUrl(album);
    if (state.coverPreloads.has(url)) return;
    const image = new Image();
    image.src = url;
    state.coverPreloads.set(url, image);
    if (state.coverPreloads.size > 12) {
      const oldest = state.coverPreloads.keys().next().value;
      state.coverPreloads.delete(oldest);
    }
  });
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `Erreur HTTP ${response.status}`);
  }
  return payload;
}

function showToast(message, type = "") {
  const toast = el("toast");
  toast.textContent = message;
  toast.className = `toast ${type}`.trim();
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.add("hidden"), 4500);
}

function setBusy(button, busy, label = "Traitement") {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
  }
}

async function loadSettings() {
  const settings = await api("/api/settings");
  el("libraryRoot").value = settings.library_root || "";
  el("minSize").value = settings.min_size || "1000";
  state.minSize = Number(settings.min_size || 1000);
  el("maxCandidates").value = settings.max_candidates || "16";
  el("batchCandidates").value = settings.batch_candidates || "4";
  el("includeMissing").checked = settings.include_missing === "1";
  el("saveExternalCover").checked = settings.save_external_cover !== "0";
  el("embedCover").checked = settings.embed_cover === "1";
  el("embedMaxSize").value = settings.embed_max_size || "1000";
  el("embedQuality").value = settings.embed_quality || "88";
  el("sourceMusicBrainz").checked = settings.source_musicbrainz !== "0";
  el("sourceTheAudioDB").checked = settings.source_theaudiodb !== "0";
  el("sourceFanart").checked = settings.source_fanart === "1";
  el("fanartApiKey").value = settings.fanart_api_key || "";
  el("fanartClientKey").value = settings.fanart_client_key || "";
  if (!settings.library_root) {
    el("settingsPanel").classList.remove("hidden");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = event.submitter;
  setBusy(button, true, "Enregistrement");
  try {
    const settings = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        library_root: el("libraryRoot").value.trim(),
        min_size: Number(el("minSize").value),
        max_candidates: Number(el("maxCandidates").value),
        batch_candidates: Number(el("batchCandidates").value),
        include_missing: el("includeMissing").checked,
        save_external_cover: el("saveExternalCover").checked,
        embed_cover: el("embedCover").checked,
        embed_max_size: Number(el("embedMaxSize").value),
        embed_quality: Number(el("embedQuality").value),
        source_musicbrainz: el("sourceMusicBrainz").checked,
        source_theaudiodb: el("sourceTheAudioDB").checked,
        source_fanart: el("sourceFanart").checked,
        fanart_api_key: el("fanartApiKey").value.trim(),
        fanart_client_key: el("fanartClientKey").value.trim(),
      }),
    });
    state.minSize = Number(settings.min_size || 1000);
    showToast("Réglages enregistrés.", "success");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function loadStats() {
  const stats = await api("/api/stats");
  el("pendingCount").textContent = stats.pending;
  el("approvedCount").textContent = stats.approved;
  el("skippedCount").textContent = stats.skipped;
  el("batchReadyCount").textContent = stats.batch_ready || 0;
  return stats;
}

async function loadAlbums(preferredId = null, preferredIndex = null) {
  const q = el("albumFilter").value.trim();
  const params = new URLSearchParams({ status: state.status, limit: "500" });
  if (q) params.set("q", q);
  const result = await api(`/api/albums?${params}`);
  state.albums = result.albums;
  state.total = result.total;

  let nextIndex = 0;
  if (preferredId) {
    const found = state.albums.findIndex((album) => album.id === preferredId);
    if (found >= 0) nextIndex = found;
  }
  if (preferredId === null && preferredIndex !== null && state.albums.length) {
    nextIndex = Math.min(preferredIndex, state.albums.length - 1);
  }
  if (state.albums.length === 0) nextIndex = -1;
  renderAlbumList();
  if (nextIndex >= 0) {
    await selectAlbum(nextIndex);
  } else {
    state.currentIndex = -1;
    state.currentAlbum = null;
    el("reviewContent").classList.add("hidden");
    el("emptyState").classList.remove("hidden");
    el("albumPosition").textContent = `0 / ${state.total}`;
  }
}

function renderAlbumList() {
  const list = el("albumList");
  list.replaceChildren();
  state.albums.forEach((album, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `album-item${index === state.currentIndex ? " active" : ""}`;
    button.dataset.albumId = album.id;
    button.addEventListener("click", () => selectAlbum(index));

    const img = document.createElement("img");
    img.className = "album-thumb";
    img.alt = "";
    if (album.has_current) {
      img.loading = "lazy";
      img.src = currentCoverUrl(album);
    }

    const text = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = album.album;
    const artist = document.createElement("span");
    artist.textContent = album.artist;
    text.append(strong, artist);
    button.append(img, text);
    list.append(button);
  });
}

async function selectAlbum(index) {
  if (index < 0 || index >= state.albums.length) return;
  state.currentIndex = index;
  state.currentAlbum = state.albums[index];
  state.selectedCandidate = null;
  state.candidates = [];
  updateActiveAlbumItem();
  renderCurrentAlbum();
  preloadAdjacentAlbumCovers(index);
  // La recherche réseau ne doit pas bloquer le changement visuel d’album.
  void searchCandidates(false);
}


function renderCurrentAlbum() {
  const album = state.currentAlbum;
  if (!album) return;

  el("emptyState").classList.add("hidden");
  el("reviewContent").classList.remove("hidden");
  el("albumArtist").textContent = album.artist;
  el("albumTitle").textContent = album.album;
  el("albumMeta").textContent = [album.year, album.current_source === "embedded" ? "pochette intégrée" : album.current_source === "external" ? "fichier externe" : "sans pochette"].filter(Boolean).join(" · ");
  el("albumPath").textContent = album.album_root;
  el("searchArtist").value = album.artist;
  el("searchAlbum").value = album.album;
  el("albumPosition").textContent = `${state.currentIndex + 1} / ${state.total}`;

  const currentImage = el("currentImage");
  const currentMissing = el("currentMissing");
  if (album.has_current) {
    const url = currentCoverUrl(album);
    currentImage.classList.add("switching");
    currentImage.onload = () => {
      if (state.currentAlbum?.id === album.id) currentImage.classList.remove("switching");
    };
    currentImage.onerror = () => {
      if (state.currentAlbum?.id !== album.id) return;
      currentImage.classList.add("hidden");
      currentMissing.textContent = "Impossible de charger la pochette";
      currentMissing.classList.remove("hidden");
    };
    currentImage.src = url;
    currentImage.classList.remove("hidden");
    currentMissing.classList.add("hidden");
    if (currentImage.complete) currentImage.classList.remove("switching");
  } else {
    currentMissing.textContent = "Aucune pochette détectée";
    currentImage.removeAttribute("src");
    currentImage.classList.add("hidden");
    currentMissing.classList.remove("hidden");
  }
  el("currentDimensions").textContent = album.current_width && album.current_height
    ? `${album.current_width} × ${album.current_height} px`
    : "Dimensions indisponibles";

  el("undoButton").classList.toggle("hidden", !album.can_undo);
  const skipButton = el("skipButton");
  if (state.status === "pending") {
    skipButton.textContent = "Ignorer";
  } else {
    skipButton.textContent = "Remettre à vérifier";
  }
  clearCandidates();
}

function clearCandidates() {
  el("candidateGrid").replaceChildren();
  el("candidateMessage").classList.add("hidden");
  el("selectionBar").classList.add("hidden");
  state.selectedCandidate = null;
  state.candidateChecks = { total: 0, completed: 0, accepted: 0, filtered: 0, failed: 0 };
  state.candidateGeneration += 1;
  state.dimensionQueue = [];
  state.activeDimensionChecks = 0;
  state.searchPartial = false;
}

async function searchCandidates(refresh) {
  const album = state.currentAlbum;
  if (!album) return;
  const artist = el("searchArtist").value.trim();
  const title = el("searchAlbum").value.trim();

  if (state.candidateSearchController) state.candidateSearchController.abort();
  const controller = new AbortController();
  state.candidateSearchController = controller;

  clearCandidates();
  el("candidateLoading").classList.remove("hidden");

  try {
    const params = new URLSearchParams({ artist, album: title });
    if (refresh) params.set("refresh", "1");
    const result = await api(`/api/albums/${album.id}/candidates?${params}`, { signal: controller.signal });
    if (controller.signal.aborted || !state.currentAlbum || state.currentAlbum.id !== album.id) return;
    state.candidates = result.candidates;
    state.searchPartial = result.partial === true;
    renderCandidates();
  } catch (error) {
    if (error.name === "AbortError") return;
    const notice = el("candidateMessage");
    notice.textContent = error.message;
    notice.className = "notice error";
  } finally {
    if (state.candidateSearchController === controller) {
      state.candidateSearchController = null;
      el("candidateLoading").classList.add("hidden");
    }
  }
}


function eligibleCandidates() {
  return state.candidates.filter((candidate) => candidate.eligible === true);
}

function clearCandidateSelection(candidate = null) {
  if (candidate && state.selectedCandidate?.id !== candidate.id) return;
  state.selectedCandidate = null;
  el("selectionBar").classList.add("hidden");
  document.querySelectorAll(".candidate-card.selected").forEach((card) => {
    card.classList.remove("selected");
  });
}

function renumberEligibleCandidates() {
  eligibleCandidates().forEach((candidate, index) => {
    candidate.shortcutNumber = index + 1;
    const card = document.querySelector(`[data-candidate-id="${candidate.id}"]`);
    const number = card?.querySelector(".candidate-number");
    if (number) number.textContent = index < 9 ? String(index + 1) : "";
  });
}

function updateCandidateFilterNotice() {
  const notice = el("candidateMessage");
  const checks = state.candidateChecks;
  const minimum = `${state.minSize} × ${state.minSize} px`;
  const partialSuffix = state.searchPartial
    ? " La recherche réseau est partielle ; relance Recherche pour compléter les résultats."
    : "";

  if (checks.completed < checks.total) {
    notice.textContent = `Contrôle des dimensions : ${checks.completed} / ${checks.total}`;
    notice.className = "notice";
    return;
  }

  if (checks.accepted === 0) {
    const details = [];
    if (checks.filtered) details.push(`${checks.filtered} trop petite${checks.filtered > 1 ? "s" : ""}`);
    if (checks.failed) details.push(`${checks.failed} non vérifiable${checks.failed > 1 ? "s" : ""}`);
    notice.textContent = `Aucune proposition n’atteint le minimum de ${minimum}${details.length ? ` (${details.join(", ")})` : ""}. Modifie la recherche ou utilise une image manuelle.${partialSuffix}`;
    notice.className = "notice";
    return;
  }

  if (checks.filtered || checks.failed) {
    const hidden = checks.filtered + checks.failed;
    notice.textContent = `${checks.accepted} proposition${checks.accepted > 1 ? "s" : ""} conforme${checks.accepted > 1 ? "s" : ""}. ${hidden} image${hidden > 1 ? "s" : ""} sous le minimum ou non vérifiable${hidden > 1 ? "s" : ""} masquée${hidden > 1 ? "s" : ""}.${partialSuffix}`;
    notice.className = "notice";
  } else {
    notice.textContent = `${checks.accepted} proposition${checks.accepted > 1 ? "s" : ""} conforme${checks.accepted > 1 ? "s" : ""} trouvée${checks.accepted > 1 ? "s" : ""}.${partialSuffix}`;
    notice.className = "notice";
  }
}

function enqueueDimensionCheck(candidate, card, dimensions, generation) {
  state.dimensionQueue.push({ candidate, card, dimensions, generation });
  pumpDimensionChecks();
}

function pumpDimensionChecks() {
  while (state.activeDimensionChecks < 4 && state.dimensionQueue.length > 0) {
    const task = state.dimensionQueue.shift();
    if (task.generation !== state.candidateGeneration) continue;

    state.activeDimensionChecks += 1;
    const probe = new Image();
    const finish = (accepted, failed = false) => {
      if (task.generation !== state.candidateGeneration) return;
      state.activeDimensionChecks = Math.max(0, state.activeDimensionChecks - 1);
      if (task.card.isConnected) {
        if (probe.naturalWidth && probe.naturalHeight) {
          task.candidate.width = probe.naturalWidth;
          task.candidate.height = probe.naturalHeight;
          task.dimensions.textContent = `${probe.naturalWidth} × ${probe.naturalHeight} px`;
          task.dimensions.classList.remove("loading", "unavailable");
        } else {
          task.dimensions.textContent = "Dimensions indisponibles";
          task.dimensions.classList.remove("loading");
          task.dimensions.classList.add("unavailable");
        }
        completeCandidateCheck(task.candidate, task.card, accepted, failed);
      }
      pumpDimensionChecks();
    };

    probe.onload = () => {
      const accepted = probe.naturalWidth >= state.minSize && probe.naturalHeight >= state.minSize;
      finish(accepted, false);
    };
    probe.onerror = () => finish(false, true);
    probe.src = task.candidate.download_url || task.candidate.preview_url;
  }
}

function completeCandidateCheck(candidate, card, accepted, failed = false) {
  if (candidate.dimensionChecked) return;
  candidate.dimensionChecked = true;
  candidate.eligible = accepted;
  state.candidateChecks.completed += 1;

  card.classList.remove("checking");
  if (accepted) {
    state.candidateChecks.accepted += 1;
    card.classList.add("eligible");
  } else {
    if (failed) state.candidateChecks.failed += 1;
    else state.candidateChecks.filtered += 1;
    clearCandidateSelection(candidate);
    card.remove();
  }

  renumberEligibleCandidates();
  updateCandidateFilterNotice();
}

function renderCandidates() {
  const grid = el("candidateGrid");
  grid.replaceChildren();
  const notice = el("candidateMessage");

  if (state.candidates.length === 0) {
    notice.textContent = "Aucune pochette trouvée. Modifie la recherche ou utilise une image locale ou une URL.";
    notice.className = "notice";
    return;
  }

  state.candidateChecks = {
    total: state.candidates.length,
    completed: 0,
    accepted: 0,
    filtered: 0,
    failed: 0,
  };
  updateCandidateFilterNotice();

  state.candidates.forEach((candidate) => {
    candidate.eligible = false;
    candidate.dimensionChecked = false;
    candidate.shortcutNumber = null;

    const card = document.createElement("div");
    card.className = "candidate-card checking";
    card.dataset.candidateId = candidate.id;
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.addEventListener("click", () => {
      if (candidate.eligible) selectCandidate(candidate);
    });
    card.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && candidate.eligible) {
        event.preventDefault();
        selectCandidate(candidate);
      }
    });

    const number = document.createElement("span");
    number.className = "candidate-number";

    const img = document.createElement("img");
    // La miniature légère s’affiche immédiatement. Le fichier qui sera
    // enregistré est contrôlé séparément, avec quatre vérifications simultanées.
    img.alt = `Pochette proposée pour ${candidate.title}`;
    img.loading = "eager";
    img.src = candidate.preview_url || candidate.download_url;

    const dimensions = document.createElement("span");
    dimensions.className = "candidate-dimensions loading";
    dimensions.textContent = "Vérification…";

    img.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      if (candidate.eligible) openImageDialog(candidate);
    });

    const title = document.createElement("strong");
    title.textContent = candidate.title;
    const artist = document.createElement("span");
    artist.className = "candidate-artist";
    artist.textContent = candidate.artist;
    const meta = document.createElement("span");
    meta.className = "candidate-meta";
    meta.textContent = [candidate.source_type, candidate.date, candidate.country, candidate.format, candidate.score ? `${candidate.score} %` : ""].filter(Boolean).join(" · ");

    const links = document.createElement("div");
    links.className = "candidate-links";
    const zoom = document.createElement("a");
    zoom.href = candidate.original_url;
    zoom.target = "_blank";
    zoom.rel = "noreferrer";
    zoom.textContent = "Grande image";
    zoom.addEventListener("click", (event) => event.stopPropagation());
    const mb = document.createElement("a");
    mb.href = candidate.source_url || candidate.musicbrainz_url;
    mb.target = "_blank";
    mb.rel = "noreferrer";
    mb.textContent = candidate.source_link_label || "Source";
    mb.addEventListener("click", (event) => event.stopPropagation());
    links.append(zoom, mb);

    card.append(number, img, dimensions, title, artist, meta, links);
    grid.append(card);
    if (candidate.width && candidate.height) {
      dimensions.textContent = `${candidate.width} × ${candidate.height} px`;
      dimensions.classList.remove("loading", "unavailable");
      completeCandidateCheck(
        candidate,
        card,
        candidate.width >= state.minSize && candidate.height >= state.minSize,
        false,
      );
    } else {
      enqueueDimensionCheck(candidate, card, dimensions, state.candidateGeneration);
    }
  });
  const firstReady = state.candidates.find((candidate) => candidate.eligible === true);
  if (firstReady) selectCandidate(firstReady);
}

function candidateDimensionText(candidate) {
  return candidate.width && candidate.height
    ? `${candidate.width} × ${candidate.height} px`
    : "dimensions en cours de lecture";
}

function updateSelectedDetails(candidate) {
  el("selectedLabel").textContent = `${candidate.artist} : ${candidate.title}`;
  el("selectedDetails").textContent = [
    candidateDimensionText(candidate),
    candidate.source_type,
    candidate.date,
    candidate.country,
    candidate.format,
  ].filter(Boolean).join(" · ");
}

function selectCandidate(candidate) {
  if (!candidate?.eligible) return;
  state.selectedCandidate = candidate;
  document.querySelectorAll(".candidate-card").forEach((card) => {
    card.classList.toggle("selected", card.dataset.candidateId === candidate.id);
  });
  updateSelectedDetails(candidate);
  el("selectionBar").classList.remove("hidden");
}

async function approveSelected() {
  const album = state.currentAlbum;
  const candidate = state.selectedCandidate;
  if (!album || !candidate) return;

  const button = el("approveButton");
  setBusy(button, true, "Enregistrement");
  try {
    await approveUrl(
      candidate.download_url,
      `${candidate.source} : ${candidate.source_type}`,
      false,
      candidate.original_url || "",
    );
  } finally {
    setBusy(button, false);
  }
}

async function approveUrl(url, source, allowSmall, fallbackUrl = "") {
  const album = state.currentAlbum;
  if (!album) return;
  try {
    const result = await api(`/api/albums/${album.id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, fallback_url: fallbackUrl, source, allow_small: allowSmall }),
    });
    showToast(formatWriteResult(result), "success");
    await afterAction(album.id);
  } catch (error) {
    if (!allowSmall && error.message.includes("moins que le minimum") && window.confirm(`${error.message}\n\nL’utiliser quand même ?`)) {
      return approveUrl(url, source, true, fallbackUrl);
    }
    showToast(error.message, "error");
  }
}

function formatWriteResult(result) {
  const parts = [`Pochette enregistrée en ${result.width} × ${result.height} px`];
  if ((result.embedded_written || 0) > 0) {
    parts.push(`intégrée dans ${result.embedded_written} fichier${result.embedded_written === 1 ? "" : "s"}`);
  }
  if ((result.embedded_skipped || 0) > 0) {
    parts.push(`${result.embedded_skipped} format${result.embedded_skipped === 1 ? "" : "s"} non pris en charge`);
  }
  return `${parts.join(" · ")}.`;
}

async function uploadImage(event) {
  event.preventDefault();
  const album = state.currentAlbum;
  const fileInput = el("manualFile");
  const file = fileInput.files[0];
  if (!album || !file) {
    showToast("Sélectionne un fichier image.", "error");
    return;
  }
  if (!window.confirm(`Utiliser « ${file.name} » pour ${album.artist} : ${album.album} ?`)) return;

  const button = event.submitter;
  setBusy(button, true, "Import");
  const data = new FormData();
  data.append("file", file);
  try {
    const result = await api(`/api/albums/${album.id}/upload`, { method: "POST", body: data });
    showToast(formatWriteResult(result), "success");
    fileInput.value = "";
    await afterAction(album.id);
  } catch (error) {
    if (error.message.includes("moins que le minimum") && window.confirm(`${error.message}\n\nL’utiliser quand même ?`)) {
      data.set("allow_small", "1");
      try {
        const result = await api(`/api/albums/${album.id}/upload`, { method: "POST", body: data });
        showToast(formatWriteResult(result), "success");
        await afterAction(album.id);
      } catch (retryError) {
        showToast(retryError.message, "error");
      }
    } else {
      showToast(error.message, "error");
    }
  } finally {
    setBusy(button, false);
  }
}

async function useManualUrl(event) {
  event.preventDefault();
  const url = el("manualUrl").value.trim();
  if (!url) {
    showToast("Colle une URL directe vers une image.", "error");
    return;
  }
  if (!window.confirm("Utiliser cette image pour l’album actuel ?")) return;
  const button = event.submitter;
  setBusy(button, true, "Téléchargement");
  try {
    await approveUrl(url, "URL manuelle", false);
    el("manualUrl").value = "";
  } finally {
    setBusy(button, false);
  }
}

async function skipOrRestore() {
  const album = state.currentAlbum;
  if (!album) return;
  try {
    if (state.status === "pending") {
      await api(`/api/albums/${album.id}/skip`, { method: "POST" });
      showToast("Album ignoré.", "success");
    } else {
      await api(`/api/albums/${album.id}/pending`, { method: "POST" });
      showToast("Album remis dans la file de validation.", "success");
    }
    await afterAction(album.id);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function undoCurrent() {
  const album = state.currentAlbum;
  if (!album || !window.confirm("Restaurer les anciennes pochettes sauvegardées ?")) return;
  const button = el("undoButton");
  setBusy(button, true, "Restauration");
  try {
    await api(`/api/albums/${album.id}/undo`, { method: "POST" });
    showToast("Ancienne pochette restaurée.", "success");
    await loadStats();
    state.status = "pending";
    updateTabs();
    await loadAlbums(album.id);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function afterAction(previousId) {
  const previousIndex = state.currentIndex;
  await loadStats();
  await loadAlbums(null, previousIndex);
}

async function startScan() {
  const button = el("scanButton");
  setBusy(button, true, "Démarrage");
  try {
    await api("/api/scan", { method: "POST" });
    el("scanStatus").classList.remove("hidden");
    pollScan();
  } catch (error) {
    showToast(error.message, "error");
    setBusy(button, false);
  }
}

async function pollScan() {
  window.clearTimeout(state.scanTimer);
  try {
    const scan = await api("/api/scan/status");
    el("scanMessage").textContent = scan.message || "Analyse en cours";
    el("scanDetails").textContent = `${scan.folders_seen || 0} dossiers parcourus, ${scan.albums_found || 0} albums à vérifier`;
    if (scan.error) {
      showToast(scan.error, "error");
    }
    if (scan.running) {
      state.scanTimer = window.setTimeout(pollScan, 900);
    } else {
      setBusy(el("scanButton"), false);
      window.setTimeout(() => el("scanStatus").classList.add("hidden"), 2500);
      await loadStats();
      await loadAlbums();
    }
  } catch (error) {
    setBusy(el("scanButton"), false);
    showToast(error.message, "error");
  }
}

function batchCandidateSource(candidate) {
  return [
    candidate.source,
    candidate.source_type,
    candidate.date,
    candidate.country,
    candidate.format,
  ].filter(Boolean).join(" · ");
}

async function updateBatchItem(albumId, selectedIndex, checked) {
  await api(`/api/batch/items/${albumId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected_index: selectedIndex, checked }),
  });
}

function renderBatchItems() {
  const list = el("batchList");
  list.replaceChildren();
  const items = state.batchItems;
  el("batchEmpty").classList.toggle("hidden", items.length > 0);

  const checkedCount = items.filter((item) => item.checked).length;
  el("batchSummary").textContent = items.length
    ? `${checkedCount} album${checkedCount > 1 ? "s" : ""} coché${checkedCount > 1 ? "s" : ""} sur ${items.length}`
    : "Aucun candidat préparé";
  el("batchCheckAll").checked = items.length > 0 && checkedCount === items.length;
  el("batchCheckAll").indeterminate = checkedCount > 0 && checkedCount < items.length;
  el("batchApplyButton").disabled = checkedCount === 0;

  items.forEach((item) => {
    const album = item.album;
    const selected = item.candidates[item.selected_index] || item.candidates[0];
    if (!selected) return;

    const article = document.createElement("article");
    article.className = `batch-item${item.checked ? " checked" : ""}`;
    article.dataset.albumId = album.id;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "batch-item-checkbox";
    checkbox.checked = item.checked;
    checkbox.setAttribute("aria-label", `Valider ${album.artist} : ${album.album}`);
    checkbox.addEventListener("change", async () => {
      item.checked = checkbox.checked;
      article.classList.toggle("checked", item.checked);
      try {
        await updateBatchItem(album.id, item.selected_index, item.checked);
        renderBatchItems();
      } catch (error) {
        item.checked = !checkbox.checked;
        showToast(error.message, "error");
        renderBatchItems();
      }
    });

    const details = document.createElement("div");
    details.className = "batch-album-details";
    const artist = document.createElement("p");
    artist.className = "eyebrow";
    artist.textContent = album.artist;
    const title = document.createElement("h3");
    title.textContent = album.album;
    const currentDimensions = document.createElement("p");
    currentDimensions.className = "muted batch-current-dimensions";
    currentDimensions.textContent = album.current_width && album.current_height
      ? `Actuelle : ${album.current_width} × ${album.current_height} px`
      : "Pochette actuelle absente";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "button secondary compact";
    open.textContent = "Ouvrir en revue";
    open.addEventListener("click", async () => {
      state.view = "review";
      state.status = "pending";
      updateTabs();
      showCurrentView();
      await loadAlbums(album.id);
    });
    details.append(artist, title, currentDimensions, open);

    const currentBox = document.createElement("div");
    currentBox.className = "batch-cover-box";
    const currentLabel = document.createElement("span");
    currentLabel.textContent = "Actuelle";
    if (album.has_current) {
      const currentImage = document.createElement("img");
      currentImage.loading = "lazy";
      currentImage.src = currentCoverUrl(album);
      currentImage.alt = `Pochette actuelle de ${album.album}`;
      currentBox.append(currentImage);
    } else {
      const missing = document.createElement("div");
      missing.className = "batch-missing";
      missing.textContent = "Aucune";
      currentBox.append(missing);
    }
    currentBox.append(currentLabel);

    const arrow = document.createElement("div");
    arrow.className = "batch-arrow";
    arrow.textContent = "→";

    const candidateBox = document.createElement("div");
    candidateBox.className = "batch-candidate-box";
    const mainImage = document.createElement("img");
    mainImage.loading = "lazy";
    mainImage.src = selected.preview_url || selected.download_url;
    mainImage.alt = `Candidat pour ${album.album}`;
    mainImage.addEventListener("dblclick", () => openImageDialog(selected));
    const candidateMeta = document.createElement("div");
    candidateMeta.className = "batch-candidate-meta";
    const dimensions = document.createElement("strong");
    dimensions.textContent = `${selected.width} × ${selected.height} px`;
    const source = document.createElement("span");
    source.textContent = batchCandidateSource(selected);
    candidateMeta.append(dimensions, source);
    candidateBox.append(mainImage, candidateMeta);

    if (item.candidates.length > 1) {
      const choices = document.createElement("div");
      choices.className = "batch-candidate-choices";
      item.candidates.forEach((candidate, index) => {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = `batch-choice${index === item.selected_index ? " selected" : ""}`;
        choice.title = `${candidate.width} × ${candidate.height} px · ${batchCandidateSource(candidate)}`;
        const thumb = document.createElement("img");
        thumb.loading = "lazy";
        thumb.src = candidate.preview_url || candidate.download_url;
        thumb.alt = `Candidat ${index + 1}`;
        choice.append(thumb);
        choice.addEventListener("click", async () => {
          const previous = item.selected_index;
          item.selected_index = index;
          try {
            await updateBatchItem(album.id, index, item.checked);
            renderBatchItems();
          } catch (error) {
            item.selected_index = previous;
            showToast(error.message, "error");
          }
        });
        choices.append(choice);
      });
      candidateBox.append(choices);
    }

    if (item.error) {
      const error = document.createElement("p");
      error.className = "batch-item-error";
      error.textContent = item.error;
      candidateBox.append(error);
    }

    article.append(checkbox, details, currentBox, arrow, candidateBox);
    list.append(article);
  });
}

async function loadBatchItems() {
  const result = await api("/api/batch/items");
  state.batchItems = result.items || [];
  renderBatchItems();
  if (result.counts) updateBackgroundCounts(result.counts);
}

function updateBackgroundCounts(counts = {}) {
  const parts = [
    `${counts.ready || 0} prêt${counts.ready === 1 ? "" : "s"}`,
    `${counts.empty || 0} sans résultat`,
    `${counts.error || 0} erreur${counts.error === 1 ? "" : "s"}`,
    `${(counts.not_started || 0) + (counts.queued || 0) + (counts.searching || 0)} restant${((counts.not_started || 0) + (counts.queued || 0) + (counts.searching || 0)) === 1 ? "" : "s"}`,
  ];
  el("backgroundSearchCounts").textContent = parts.join(" · ");
  el("batchReadyCount").textContent = counts.ready || 0;
}

async function pollBackgroundSearch() {
  window.clearTimeout(state.backgroundTimer);
  try {
    const search = await api("/api/background-search/status");
    const total = Math.max(1, search.total || 0);
    el("backgroundSearchProgress").max = total;
    el("backgroundSearchProgress").value = search.processed || 0;
    el("backgroundSearchNumbers").textContent = search.total
      ? `${search.processed || 0} / ${search.total}`
      : "";
    el("backgroundSearchLabel").textContent = search.running
      ? "Recherche en arrière-plan"
      : search.total
        ? "Recherche terminée"
        : "Recherche inactive";
    el("backgroundSearchCurrent").textContent = search.current || "";
    updateBackgroundCounts(search.counts || {});
    el("stopBackgroundSearchButton").classList.toggle("hidden", !search.running);
    el("backgroundSearchButton").disabled = search.running;
    el("refreshBackgroundSearchButton").disabled = search.running;

    if (state.view === "batch" && state.lastBackgroundProcessed !== (search.processed || 0)) {
      state.lastBackgroundProcessed = search.processed || 0;
      await loadBatchItems();
      await loadStats();
    }
    if (search.running) {
      state.backgroundTimer = window.setTimeout(pollBackgroundSearch, 1200);
    }
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function startBackgroundSearch(refresh = false) {
  const button = refresh ? el("refreshBackgroundSearchButton") : el("backgroundSearchButton");
  setBusy(button, true, "Préparation");
  try {
    const result = await api("/api/background-search/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh }),
    });
    if (result.queued === 0) {
      showToast("Toutes les recherches sont déjà préparées pour les réglages actuels.", "success");
    } else {
      showToast(`${result.queued} album${result.queued > 1 ? "s" : ""} ajouté${result.queued > 1 ? "s" : ""} à la file.`, "success");
    }
    state.lastBackgroundProcessed = -1;
    await pollBackgroundSearch();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function stopBackgroundSearch() {
  try {
    await api("/api/background-search/stop", { method: "POST" });
    showToast("Arrêt demandé. La recherche en cours se terminera avant l’arrêt.", "success");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function checkAllBatch(checked) {
  try {
    await api("/api/batch/check-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ checked }),
    });
    state.batchItems.forEach((item) => { item.checked = checked; });
    renderBatchItems();
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function startBatchApply() {
  const selected = state.batchItems.filter((item) => item.checked).length;
  if (!selected) return;
  const button = el("batchApplyButton");
  setBusy(button, true, "Démarrage");
  try {
    const result = await api("/api/batch/apply/start", { method: "POST" });
    if (!result.total) {
      showToast("Aucun album coché à valider.", "error");
      return;
    }
    el("batchApplyStatus").classList.remove("hidden");
    await pollBatchApply();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function pollBatchApply() {
  window.clearTimeout(state.batchApplyTimer);
  try {
    const apply = await api("/api/batch/apply/status");
    const total = Math.max(1, apply.total || 0);
    el("batchApplyProgress").max = total;
    el("batchApplyProgress").value = apply.processed || 0;
    el("batchApplyNumbers").textContent = `${apply.processed || 0} / ${apply.total || 0}`;
    el("batchApplyLabel").textContent = apply.running
      ? "Validation en cours"
      : `Validation terminée : ${apply.succeeded || 0} réussie${apply.succeeded === 1 ? "" : "s"}, ${apply.failed || 0} échec${apply.failed === 1 ? "" : "s"}`;
    el("batchApplyCurrent").textContent = apply.current || "";
    if (apply.running) {
      state.batchApplyTimer = window.setTimeout(pollBatchApply, 1000);
    } else {
      await loadStats();
      await loadBatchItems();
    }
  } catch (error) {
    showToast(error.message, "error");
  }
}

function showCurrentView() {
  const batch = state.view === "batch";
  el("reviewView").classList.toggle("hidden", batch);
  el("batchView").classList.toggle("hidden", !batch);
}

function updateTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === state.view
      && (state.view === "batch" || tab.dataset.status === state.status);
    tab.classList.toggle("active", active);
  });
}

function openImageDialog(candidate) {
  el("dialogImage").src = candidate.original_url;
  const dimensions = candidate.width && candidate.height
    ? ` · image utilisée : ${candidate.width} × ${candidate.height} px`
    : "";
  el("dialogCaption").textContent = `${candidate.artist} : ${candidate.title}${dimensions}`;
  el("imageDialog").showModal();
}

function navigate(delta) {
  const next = state.currentIndex + delta;
  if (next >= 0 && next < state.albums.length) selectAlbum(next);
}

function handleKeyboard(event) {
  if (state.view !== "review") return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (event.key >= "1" && event.key <= "9") {
    const index = Number(event.key) - 1;
    const candidate = eligibleCandidates()[index];
    if (candidate) selectCandidate(candidate);
  } else if (event.key === "Enter" && state.selectedCandidate) {
    approveSelected();
  } else if (event.key.toLowerCase() === "s" && state.status === "pending") {
    skipOrRestore();
  } else if (event.key === "ArrowDown" || event.key === "ArrowRight") {
    navigate(1);
  } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
    navigate(-1);
  }
}

async function init() {
  el("settingsToggle").addEventListener("click", () => el("settingsPanel").classList.toggle("hidden"));
  el("settingsForm").addEventListener("submit", saveSettings);
  el("scanButton").addEventListener("click", startScan);
  el("searchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    searchCandidates(true);
  });
  el("approveButton").addEventListener("click", approveSelected);
  el("skipButton").addEventListener("click", skipOrRestore);
  el("undoButton").addEventListener("click", undoCurrent);
  el("urlForm").addEventListener("submit", useManualUrl);
  el("uploadForm").addEventListener("submit", uploadImage);
  el("dialogClose").addEventListener("click", () => el("imageDialog").close());
  el("imageDialog").addEventListener("click", (event) => {
    if (event.target === el("imageDialog")) el("imageDialog").close();
  });
  el("albumFilter").addEventListener("input", () => {
    window.clearTimeout(state.filterTimer);
    state.filterTimer = window.setTimeout(() => loadAlbums(), 250);
  });
  el("backgroundSearchButton").addEventListener("click", () => startBackgroundSearch(false));
  el("refreshBackgroundSearchButton").addEventListener("click", () => startBackgroundSearch(true));
  el("stopBackgroundSearchButton").addEventListener("click", stopBackgroundSearch);
  el("batchCheckAll").addEventListener("change", (event) => checkAllBatch(event.target.checked));
  el("batchApplyButton").addEventListener("click", startBatchApply);
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", async () => {
      state.view = tab.dataset.view || "review";
      if (state.view === "review") state.status = tab.dataset.status || "pending";
      updateTabs();
      showCurrentView();
      if (state.view === "batch") {
        await loadBatchItems();
        await pollBackgroundSearch();
        const apply = await api("/api/batch/apply/status");
        if (apply.running) {
          el("batchApplyStatus").classList.remove("hidden");
          await pollBatchApply();
        }
      } else {
        await loadAlbums();
      }
    });
  });
  document.addEventListener("keydown", handleKeyboard);

  try {
    await loadSettings();
    await loadStats();
    showCurrentView();
    await loadAlbums();
    await pollBackgroundSearch();
    const scan = await api("/api/scan/status");
    if (scan.running) {
      el("scanStatus").classList.remove("hidden");
      setBusy(el("scanButton"), true, "Analyse");
      pollScan();
    }
  } catch (error) {
    showToast(error.message, "error");
  }
}

init();
