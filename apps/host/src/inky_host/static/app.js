const displayId = "inky-main";
const state = {
  display: null, sourceFile: null, sourceImage: null, sourceUrl: null, uploadedAsset: null,
  albums: [], albumAssets: new Map(), albumDraftIds: [], editingAlbumId: null,
  pickerAssets: [], pickerOffset: 0, pickerHasMore: true, pickerLoading: false, pickerQuery: "", pickerTimer: null,
  imageEditor: { mode: null, asset: null, file: null, image: null, sourceUrl: null, returnToAlbumPicker: false },
};
const byId = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", '"': "&quot;" })[character]);

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 204) return null;
  if (response.ok) return response.json();
  const responseText = await response.text();
  let payload = {};
  try { payload = JSON.parse(responseText || "{}"); } catch (_) { /* Use the plain response below. */ }
  const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.message;
  throw new Error(detail || responseText || "Request failed (" + response.status + ")");
}
function setMessage(id, message, kind = "") {
  const element = byId(id);
  element.textContent = message;
  element.className = "message " + kind;
}
function showView(view) {
  document.querySelectorAll(".view").forEach((element) => { element.hidden = element.id !== view + "-view"; });
  document.querySelectorAll(".tab").forEach((element) => { element.classList.toggle("active", element.dataset.view === view); });
  if (view === "gallery") loadGallery();
  if (view === "albums") loadAlbums();
}
function previewDimensions() {
  if (!state.display) return [800, 480];
  return [90, 270].includes(Number(state.display.rotation)) ? [state.display.height, state.display.width] : [state.display.width, state.display.height];
}
function renderDisplayImage(targetId, imageData, emptyMessage) {
  const target = byId(targetId);
  const filename = imageData.filename;
  const image = imageData.previewUrl
    ? '<img src="' + imageData.previewUrl + '" alt="' + escapeHtml(filename) + '">'
    : '<div class="current-display-empty">' + emptyMessage + '</div>';
  const name = filename ? '<strong>' + escapeHtml(filename) + '</strong>' : '<strong>Nothing yet</strong>';
  const source = filename ? (imageData.albumName ? 'From album: <strong>' + escapeHtml(imageData.albumName) + '</strong>' : 'Single image') : "";
  target.innerHTML = image + '<div class="current-display-copy">' + name + '<p>' + source + '</p></div>';
}
function renderDisplayStates() {
  const display = state.display;
  renderDisplayImage("current-display", {
    filename: display?.current_asset_filename,
    previewUrl: display?.current_preview_url ? display.current_preview_url + '?v=' + display.current_revision : null,
    albumName: display?.current_album_name,
  }, "The Pi has not confirmed an image yet.");
  renderDisplayImage("requested-display", {
    filename: display?.requested_asset_filename,
    previewUrl: display?.requested_preview_url ? display.requested_preview_url + '?v=' + display.desired_revision : null,
    albumName: display?.requested_album_name,
  }, "No image is available to the Pi.");
  return display?.active_album_name ? 'Running: ' + display.active_album_name : 'Not running';
}
function lastCheckInLabel(value) {
  if (!value) return "Not seen yet";
  const timestamp = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + "Z";
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - new Date(timestamp).getTime()) / 60000));
  if (elapsedMinutes < 60) return elapsedMinutes + " minute" + (elapsedMinutes === 1 ? "" : "s") + " ago";
  if (elapsedMinutes < 48 * 60) {
    const elapsedHours = Math.floor(elapsedMinutes / 60);
    return elapsedHours + " hour" + (elapsedHours === 1 ? "" : "s") + " ago";
  }
  return new Date(value).toLocaleString();
}
function renderDisplay(display) {
  state.display = display;
  byId("orientation").value = display.orientation;
  byId("rotation").value = display.rotation;
  const dimensions = previewDimensions();
  const frame = byId("preview-frame");
  frame.classList.toggle("portrait", dimensions[1] > dimensions[0]);
  frame.classList.toggle("landscape", dimensions[0] >= dimensions[1]);
  byId("preview-meta").textContent = dimensions[0] + " × " + dimensions[1] + " • " + (dimensions[1] > dimensions[0] ? "portrait" : "landscape");
  byId("stop-playback").disabled = !display.active_album_name;
  const albumState = renderDisplayStates();
  byId("display-details").innerHTML = [
    ["Album", albumState],
    ["Last Pi check-in", lastCheckInLabel(display.last_seen_at)],
    ["Last error", display.last_error || "None"],
  ].map(([label, value]) => "<div><dt>" + label + "</dt><dd>" + escapeHtml(value) + "</dd></div>").join("");
  renderLocalPreview();
}
async function loadDisplay() { renderDisplay(await api("/api/v1/displays/" + displayId)); }
async function loadDashboard() {
  const results = await Promise.all([api("/api/v1/displays/" + displayId), api("/api/v1/albums")]);
  state.albums = results[1];
  renderDisplay(results[0]);
}

function rotatedCanvas(image, degrees) {
  const swap = degrees === 90 || degrees === 270;
  const canvas = document.createElement("canvas");
  canvas.width = swap ? image.naturalHeight : image.naturalWidth;
  canvas.height = swap ? image.naturalWidth : image.naturalHeight;
  const context = canvas.getContext("2d");
  context.translate(canvas.width / 2, canvas.height / 2);
  context.rotate((degrees * Math.PI) / 180);
  context.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2);
  return canvas;
}
function drawFramedImage(image, canvas, fitMode, rotation) {
  if (!image || !state.display) return;
  const dimensions = previewDimensions();
  canvas.width = dimensions[0]; canvas.height = dimensions[1];
  const context = canvas.getContext("2d");
  context.imageSmoothingEnabled = true; context.imageSmoothingQuality = "high";
  context.fillStyle = "#ffffff"; context.fillRect(0, 0, dimensions[0], dimensions[1]);
  const source = rotatedCanvas(image, Number(rotation));
  const framing = fitMode;
  if (framing === "stretch") context.drawImage(source, 0, 0, dimensions[0], dimensions[1]);
  else {
    const scale = framing === "crop" ? Math.max(dimensions[0] / source.width, dimensions[1] / source.height) : Math.min(dimensions[0] / source.width, dimensions[1] / source.height);
    const width = Math.round(source.width * scale); const height = Math.round(source.height * scale);
    context.drawImage(source, Math.round((dimensions[0] - width) / 2), Math.round((dimensions[1] - height) / 2), width, height);
  }
}
function renderLocalPreview() {
  if (!state.sourceImage || !state.display) return;
  const canvas = byId("local-preview");
  drawFramedImage(state.sourceImage, canvas, byId("fit-mode").value, byId("content-rotation").value);
  byId("preview-placeholder").hidden = true;
  byId("preview-workspace").hidden = false;
  canvas.hidden = false;
}
function renderSettings() {
  return { fit_mode: byId("fit-mode").value, content_rotation: Number(byId("content-rotation").value), focal_point_x: 0.5, focal_point_y: 0.5, flip_horizontal: false, flip_vertical: false };
}
function isSupportedImage(file) { return file && /^(image\/jpeg|image\/png|image\/webp)$/.test(file.type); }
async function uploadAssetFile(file) {
  if (!isSupportedImage(file)) throw new Error("Choose a JPEG, PNG, or WebP image.");
  const data = new FormData(); data.append("file", file);
  return api("/api/v1/assets", { method: "POST", body: data });
}
async function updateAssetSettings(assetId, settings) {
  return api("/api/v1/assets/" + assetId, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ render_settings: settings }) });
}
async function uploadCurrentImage(settings = renderSettings()) {
  if (!state.sourceFile) throw new Error("Choose an image first");
  if (!state.uploadedAsset) state.uploadedAsset = await uploadAssetFile(state.sourceFile);
  state.uploadedAsset = await updateAssetSettings(state.uploadedAsset.id, settings);
  return state.uploadedAsset;
}
function setUploadButtons(disabled) { byId("upload-button").disabled = disabled; byId("display-button").disabled = disabled; }
function selectSourceFile(file) {
  if (!file) return;
  if (!isSupportedImage(file)) { setMessage("job-message", "Choose a JPEG, PNG, or WebP image.", "error"); return; }
  if (state.sourceUrl) URL.revokeObjectURL(state.sourceUrl);
  state.sourceFile = file; state.sourceUrl = URL.createObjectURL(file); state.uploadedAsset = null;
  byId("file-label").textContent = file.name;
  const image = new Image();
  image.onload = () => {
    state.sourceImage = image; renderLocalPreview(); setUploadButtons(false);
    setMessage("job-message", "This preview is only in your browser. Nothing has been uploaded.");
  };
  image.onerror = () => setMessage("job-message", "This image could not be previewed.", "error");
  image.src = state.sourceUrl;
}
function imageEditorSettings() {
  return {
    fit_mode: byId("image-editor-fit").value,
    content_rotation: Number(byId("image-editor-rotation").value),
    focal_point_x: 0.5, focal_point_y: 0.5, flip_horizontal: false, flip_vertical: false,
  };
}
function renderImageEditorPreview() {
  const editor = state.imageEditor;
  if (!editor.image || !state.display) return;
  const dimensions = previewDimensions();
  const frame = byId("image-editor-frame");
  frame.classList.toggle("portrait", dimensions[1] > dimensions[0]);
  frame.classList.toggle("landscape", dimensions[0] >= dimensions[1]);
  drawFramedImage(editor.image, byId("image-editor-preview"), byId("image-editor-fit").value, byId("image-editor-rotation").value);
}
function closeImageEditor() {
  const editor = state.imageEditor;
  const returnToAlbumPicker = editor.returnToAlbumPicker;
  if (editor.file && editor.sourceUrl) URL.revokeObjectURL(editor.sourceUrl);
  state.imageEditor = { mode: null, asset: null, file: null, image: null, sourceUrl: null, returnToAlbumPicker: false };
  byId("image-editor-dialog").close();
  if (returnToAlbumPicker && !byId("album-picker-dialog").open) byId("album-picker-dialog").showModal();
}
function openImageEditor(mode, asset = null, file = null, returnToAlbumPicker = false) {
  const editor = state.imageEditor;
  if (editor.file && editor.sourceUrl) URL.revokeObjectURL(editor.sourceUrl);
  if (file && !isSupportedImage(file)) { setMessage("gallery-message", "Choose a JPEG, PNG, or WebP image.", "error"); return; }
  const settings = asset?.render_settings || { fit_mode: "crop", content_rotation: 0 };
  state.imageEditor = { mode, asset, file, image: null, sourceUrl: file ? URL.createObjectURL(file) : asset.original_url, returnToAlbumPicker };
  byId("image-editor-eyebrow").textContent = mode === "album-upload" ? "ADD TO ALBUM" : "EDIT IMAGE";
  byId("image-editor-title").textContent = mode === "album-upload" ? "Frame the new image" : "Frame " + asset.original_filename;
  byId("save-image-editor").textContent = mode === "album-upload" ? "Add to album" : "Save image settings";
  byId("image-editor-fit").value = settings.fit_mode || "crop";
  byId("image-editor-rotation").value = String(settings.content_rotation || 0);
  setMessage("image-editor-message", mode === "album-upload" ? "This image is only in your browser until you add it." : "Changes apply whenever this image is displayed.");
  const dialog = byId("image-editor-dialog");
  if (!dialog.open) dialog.showModal();
  const image = new Image();
  image.onload = () => { state.imageEditor.image = image; renderImageEditorPreview(); };
  image.onerror = () => setMessage("image-editor-message", "This image could not be loaded.", "error");
  image.src = state.imageEditor.sourceUrl;
}
async function saveImageEditor() {
  const editor = state.imageEditor;
  if (!editor.mode) return;
  const button = byId("save-image-editor");
  button.disabled = true;
  try {
    const settings = imageEditorSettings();
    if (editor.mode === "asset") {
      const asset = await updateAssetSettings(editor.asset.id, settings);
      state.albumAssets.set(asset.id, asset);
      setMessage("gallery-message", "Image settings saved.");
      closeImageEditor(); await loadGallery();
    } else {
      const asset = await updateAssetSettings((await uploadAssetFile(editor.file)).id, settings);
      state.albumAssets.set(asset.id, asset); state.pickerAssets.unshift(asset);
      if (!state.albumDraftIds.includes(asset.id)) state.albumDraftIds.push(asset.id);
      renderAlbumPlaylist(); renderAlbumPicker(); closeImageEditor();
      setMessage("album-picker-message", '"' + asset.original_filename + '" was added to this album.');
    }
  } catch (error) { setMessage("image-editor-message", error.message, "error"); }
  finally { button.disabled = false; }
}
async function waitForJob(jobId) {
  const job = await api("/api/v1/jobs/" + jobId);
  const labels = {
    queued: "Queued for resize…", rendering: "Resizing the display image…",
    ready: "Ready for the Pi to refresh.", started: "The Pi is refreshing the e-ink panel…",
    completed: "The display refresh completed.", superseded: "Replaced by a newer display request.",
    failed: "Could not prepare the image: " + (job.error_message || "unknown error"),
  };
  setMessage("job-message", labels[job.status] || "Job state: " + job.status, job.status === "failed" ? "error" : "");
  await loadDashboard();
  if (["queued", "rendering", "ready", "started"].includes(job.status)) window.setTimeout(() => waitForJob(jobId).catch((error) => setMessage("job-message", error.message, "error")), 1500);
}
async function displayAsset(assetId, settings = null) {
  const job = await api("/api/v1/displays/" + displayId + "/display-now", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_id: assetId, render_settings: settings }) });
  showView("dashboard");
  await waitForJob(job.id);
}
function assetPreview(asset, className = "") {
  const settings = asset.render_settings || {};
  const previewVersion = [asset.sha256, settings.fit_mode, settings.content_rotation, settings.focal_point_x, settings.focal_point_y, settings.flip_horizontal, settings.flip_vertical].join("-");
  return '<img class="' + className + '" loading="lazy" src="' + asset.preview_url + '?v=' + encodeURIComponent(previewVersion) + '" alt="' + escapeHtml(asset.original_filename) + '">';
}

async function loadGallery() {
  const assets = await api("/api/v1/assets?limit=100");
  byId("gallery-count").textContent = assets.length + " photo" + (assets.length === 1 ? "" : "s") + (assets.length === 100 ? " shown" : "");
  byId("gallery-grid").innerHTML = assets.map((asset) => '<article class="asset-card" data-asset-card="' + asset.id + '"><input class="gallery-select" type="checkbox" value="' + asset.id + '" tabindex="-1"><button class="gallery-image" data-gallery-image="' + asset.id + '" type="button" aria-label="' + escapeHtml(asset.original_filename) + '. Press and hold to select, or press to show actions." aria-expanded="false" aria-pressed="false">' + assetPreview(asset) + '<span class="selected-indicator" aria-hidden="true">✓</span></button><div class="gallery-item-actions" data-gallery-actions="' + asset.id + '" hidden><button data-display="' + asset.id + '" type="button">Display</button><button data-edit="' + asset.id + '" type="button" class="secondary">Edit</button><button data-delete="' + asset.id + '" class="danger" type="button">Delete</button></div></article>').join("") || '<p class="empty">No images here yet. Start from the dashboard.</p>';
  byId("gallery-grid").querySelectorAll("[data-gallery-image]").forEach(bindGalleryImage);
  byId("gallery-grid").querySelectorAll("[data-display]").forEach((button) => button.addEventListener("click", () => displayAsset(button.dataset.display).catch((error) => setMessage("gallery-message", error.message, "error"))));
  byId("gallery-grid").querySelectorAll("[data-edit]").forEach((button) => button.addEventListener("click", () => openImageEditor("asset", assets.find((asset) => asset.id === button.dataset.edit))));
  byId("gallery-grid").querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => deleteAssets([button.dataset.delete]).catch((error) => setMessage("gallery-message", error.message, "error"))));
  updateGallerySelection();
}
function closeGalleryActions() {
  document.querySelectorAll("[data-gallery-actions]").forEach((actions) => { actions.hidden = true; });
  document.querySelectorAll("[data-gallery-image]").forEach((image) => image.setAttribute("aria-expanded", "false"));
}
function setGallerySelected(card, selected) {
  card.querySelector(".gallery-select").checked = selected;
  card.classList.toggle("selected", selected);
  card.querySelector(".gallery-image").setAttribute("aria-pressed", String(selected));
  updateGallerySelection();
}
function updateGallerySelection() {
  const count = selectedGalleryIds().length;
  const actions = byId("gallery-selection-actions");
  actions.hidden = count === 0;
  byId("gallery-selection-count").textContent = count + " selected";
  byId("gallery-grid").classList.toggle("selection-mode", count > 0);
}
function bindGalleryImage(image) {
  const card = image.closest(".asset-card");
  let holdTimer = null;
  let held = false;
  const cancelHold = () => { window.clearTimeout(holdTimer); holdTimer = null; };
  image.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    held = false;
    holdTimer = window.setTimeout(() => {
      held = true;
      closeGalleryActions();
      setGallerySelected(card, !card.classList.contains("selected"));
      if (navigator.vibrate) navigator.vibrate(30);
    }, 500);
  });
  image.addEventListener("pointerup", cancelHold);
  image.addEventListener("pointercancel", cancelHold);
  image.addEventListener("pointerleave", cancelHold);
  image.addEventListener("dragstart", (event) => event.preventDefault());
  image.addEventListener("contextmenu", (event) => event.preventDefault());
  image.addEventListener("keydown", (event) => {
    if (event.key.toLowerCase() !== "s") return;
    event.preventDefault();
    closeGalleryActions();
    setGallerySelected(card, !card.classList.contains("selected"));
  });
  image.addEventListener("click", () => {
    if (held) { held = false; return; }
    if (selectedGalleryIds().length) {
      setGallerySelected(card, !card.classList.contains("selected"));
      return;
    }
    const actions = card.querySelector("[data-gallery-actions]");
    const shouldOpen = actions.hidden;
    closeGalleryActions();
    actions.hidden = !shouldOpen;
    image.setAttribute("aria-expanded", String(shouldOpen));
  });
}
async function deleteAssets(assetIds) {
  if (!assetIds.length || !window.confirm("Permanently delete " + assetIds.length + " image(s)? This cannot be undone.")) return;
  const result = await api("/api/v1/assets/bulk-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_ids: assetIds }) });
  setMessage("gallery-message", "Deleted " + result.deleted_ids.length + " image(s).");
  assetIds.forEach((id) => state.albumAssets.delete(id));
  state.pickerAssets = state.pickerAssets.filter((asset) => !assetIds.includes(asset.id));
  await loadGallery();
  renderAlbumPicker();
}
function selectedGalleryIds() {
  return [...document.querySelectorAll("#gallery-grid input[type=checkbox]:checked")].map((input) => input.value);
}
function openQuickPlay() {
  const assetIds = selectedGalleryIds();
  if (!assetIds.length) { setMessage("gallery-message", "Select at least one image first.", "error"); return; }
  byId("quick-play-count").textContent = assetIds.length + " selected image" + (assetIds.length === 1 ? "" : "s") + ". This does not create a saved album.";
  setMessage("quick-play-message", "");
  byId("quick-play-dialog").showModal();
}
async function startQuickPlay() {
  const assetIds = selectedGalleryIds();
  if (!assetIds.length) { closeQuickPlay(); return; }
  const button = byId("start-quick-play");
  button.disabled = true;
  try {
    await api("/api/v1/displays/" + displayId + "/play-selection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_ids: assetIds, order_mode: byId("quick-play-order").value, interval_seconds: Number(byId("quick-play-interval").value) * 60 }) });
    closeQuickPlay(); showView("dashboard"); await loadDashboard();
    setMessage("job-message", "Selected images are running.");
  } catch (error) { setMessage("quick-play-message", error.message, "error"); }
  finally { button.disabled = false; }
}
function closeQuickPlay() { byId("quick-play-dialog").close(); }

function selectedAssets() { return state.albumDraftIds.map((id) => state.albumAssets.get(id)).filter(Boolean); }
function renderAlbumPlaylist() {
  const selected = selectedAssets();
  state.albumDraftIds = selected.map((asset) => asset.id);
  byId("album-count").textContent = selected.length;
  byId("album-sequence").innerHTML = selected.map((asset, index) => '<article class="sequence-item">' + assetPreview(asset, "sequence-thumb") + '<div><strong>' + escapeHtml(asset.original_filename) + "</strong><span>Position " + (index + 1) + '</span></div><div class="sequence-actions"><button data-move-up="' + index + '" class="icon-button secondary" type="button" aria-label="Move up" ' + (index === 0 ? "disabled" : "") + '>↑</button><button data-move-down="' + index + '" class="icon-button secondary" type="button" aria-label="Move down" ' + (index === selected.length - 1 ? "disabled" : "") + '>↓</button><button data-remove="' + asset.id + '" class="icon-button danger" type="button" aria-label="Remove from album">×</button></div></article>').join("") || '<p class="empty playlist-empty">Use “Add images” to build this album.</p>';
  byId("album-sequence").querySelectorAll("[data-remove]").forEach((button) => button.addEventListener("click", () => { state.albumDraftIds = state.albumDraftIds.filter((id) => id !== button.dataset.remove); renderAlbumPlaylist(); renderAlbumPicker(); }));
  byId("album-sequence").querySelectorAll("[data-move-up]").forEach((button) => button.addEventListener("click", () => moveAlbumItem(Number(button.dataset.moveUp), -1)));
  byId("album-sequence").querySelectorAll("[data-move-down]").forEach((button) => button.addEventListener("click", () => moveAlbumItem(Number(button.dataset.moveDown), 1)));
}
function renderAlbumPicker() {
  const list = byId("album-library");
  if (!list) return;
  byId("album-library-count").textContent = state.pickerAssets.length + " loaded" + (state.pickerHasMore ? " · keep scrolling for more" : "");
  list.innerHTML = state.pickerAssets.map((asset) => {
    const chosen = state.albumDraftIds.includes(asset.id);
    return '<article class="picker-item ' + (chosen ? "selected" : "") + '">' + assetPreview(asset, "picker-thumb") + '<div><strong>' + escapeHtml(asset.original_filename) + '</strong><span>' + (asset.width ? asset.width + " × " + asset.height : "Original image") + '</span></div><button data-add="' + asset.id + '" class="' + (chosen ? "secondary" : "") + '" type="button" ' + (chosen ? "disabled" : "") + ">" + (chosen ? "Added" : "Add") + "</button></article>";
  }).join("") || '<p class="empty">No gallery images match that search.</p>';
  list.querySelectorAll("[data-add]").forEach((button) => button.addEventListener("click", () => {
    if (!state.albumDraftIds.includes(button.dataset.add)) {
      state.albumDraftIds.push(button.dataset.add);
      renderAlbumPlaylist(); renderAlbumPicker();
    }
  }));
  byId("album-picker-sentinel").hidden = !state.pickerHasMore;
}
function moveAlbumItem(index, direction) {
  const next = index + direction;
  if (next < 0 || next >= state.albumDraftIds.length) return;
  [state.albumDraftIds[index], state.albumDraftIds[next]] = [state.albumDraftIds[next], state.albumDraftIds[index]];
  renderAlbumPlaylist();
}
async function resetAlbumPicker() {
  state.pickerAssets = []; state.pickerOffset = 0; state.pickerHasMore = true;
  await loadAlbumPickerPage();
}
async function loadAlbumPickerPage() {
  if (state.pickerLoading || !state.pickerHasMore) return;
  state.pickerLoading = true;
  const params = new URLSearchParams({ limit: "40", offset: String(state.pickerOffset), query: state.pickerQuery });
  try {
    const assets = await api("/api/v1/assets?" + params);
    assets.forEach((asset) => state.albumAssets.set(asset.id, asset));
    state.pickerAssets.push(...assets);
    state.pickerOffset += assets.length;
    state.pickerHasMore = assets.length === 40;
    renderAlbumPicker();
  } catch (error) { setMessage("album-picker-message", error.message, "error"); }
  finally { state.pickerLoading = false; }
}
async function openAlbumPicker() {
  const dialog = byId("album-picker-dialog");
  if (!dialog.open) dialog.showModal();
  state.pickerQuery = "";
  byId("album-search").value = "";
  byId("album-search").focus();
  await resetAlbumPicker();
}
function closeAlbumPicker() { byId("album-picker-dialog").close(); }
function showAlbumEditor() { byId("album-listing").hidden = true; byId("album-editor").hidden = false; byId("album-name").focus(); }
function closeAlbumEditor() {
  closeAlbumPicker();
  byId("album-editor").hidden = true; byId("album-listing").hidden = false;
  state.editingAlbumId = null; state.albumDraftIds = []; state.albumAssets.clear();
}
function resetAlbumEditor() {
  state.editingAlbumId = null; state.albumDraftIds = []; state.albumAssets.clear();
  byId("album-form").reset(); byId("album-interval").value = 20;
  byId("album-form-title").textContent = "Create an album"; byId("album-form-eyebrow").textContent = "NEW ALBUM"; byId("save-album").textContent = "Create album";
  setMessage("album-picker-message", ""); renderAlbumPlaylist();
}
function startNewAlbum() { resetAlbumEditor(); showAlbumEditor(); }
function editAlbum(album) {
  state.editingAlbumId = album.id; state.albumDraftIds = [...new Set(album.items.map((item) => item.asset_id))];
  state.albumAssets.clear(); album.items.forEach((item) => state.albumAssets.set(item.asset.id, item.asset));
  byId("album-name").value = album.name; byId("album-interval").value = Math.round(album.interval_seconds / 60);
  byId("album-order").value = album.order_mode;
  byId("album-form-title").textContent = "Edit " + album.name; byId("album-form-eyebrow").textContent = "EDIT ALBUM"; byId("save-album").textContent = "Save album";
  setMessage("album-picker-message", ""); renderAlbumPlaylist(); showAlbumEditor(); window.scrollTo({ top: 0, behavior: "smooth" });
}
function renderAlbumList() {
  byId("album-list").innerHTML = state.albums.map((album) => '<article class="album-card"><div class="album-card-content"><div><p class="eyebrow">' + (album.is_running ? "RUNNING NOW" : "ALBUM") + "</p><h3>" + escapeHtml(album.name) + "</h3><p>" + album.items.length + " image" + (album.items.length === 1 ? "" : "s") + " · " + (album.order_mode === "shuffle" ? "Shuffle" : "In order") + " · every " + Math.round(album.interval_seconds / 60) + ' minutes</p></div><div class="album-thumbs">' + (album.items.slice(0, 6).map((item) => assetPreview(item.asset, "album-thumb")).join("") || "<span>Empty</span>") + '</div></div><div class="album-actions"><button data-run="' + album.id + '" type="button">' + (album.is_running ? "Restart" : "Run") + '</button><button data-stop="' + album.id + '" class="secondary" type="button" ' + (album.is_running ? "" : "disabled") + '>Stop</button><button data-edit="' + album.id + '" class="secondary" type="button">Edit</button><button data-delete-album="' + album.id + '" class="danger" type="button">Delete</button></div></article>').join("") || '<p class="empty">No albums yet. Create one when you are ready.</p>';
  byId("album-list").querySelectorAll("[data-run]").forEach((button) => button.addEventListener("click", () => albumAction(button.dataset.run, "run", "Album is running.")));
  byId("album-list").querySelectorAll("[data-stop]").forEach((button) => button.addEventListener("click", () => albumAction(button.dataset.stop, "stop", "Album stopped.")));
  byId("album-list").querySelectorAll("[data-edit]").forEach((button) => button.addEventListener("click", () => editAlbum(state.albums.find((album) => album.id === button.dataset.edit))));
  byId("album-list").querySelectorAll("[data-delete-album]").forEach((button) => button.addEventListener("click", () => deleteAlbum(button.dataset.deleteAlbum)));
}
async function albumAction(id, action, message) {
  try { await api("/api/v1/albums/" + id + "/" + action, { method: "POST" }); setMessage("album-message", message); await loadAlbums(); }
  catch (error) { setMessage("album-message", error.message, "error"); }
}
async function deleteAlbum(id) {
  if (!window.confirm("Delete this album?")) return;
  try { await api("/api/v1/albums/" + id, { method: "DELETE" }); setMessage("album-message", "Album deleted."); await loadAlbums(); }
  catch (error) { setMessage("album-message", error.message, "error"); }
}
async function loadAlbums() {
  const results = await Promise.all([api("/api/v1/albums"), api("/api/v1/displays/" + displayId)]);
  state.albums = results[0];
  renderAlbumList();
  renderDisplay(results[1]);
}
async function uploadAlbumImages(fileList) {
  const files = [...fileList];
  byId("album-image-file").value = "";
  if (!files.length) return;
  if (files.length === 1) {
    closeAlbumPicker();
    openImageEditor("album-upload", null, files[0], true);
    return;
  }
  const failures = [];
  let added = 0;
  for (const [index, file] of files.entries()) {
    setMessage("album-picker-message", "Uploading " + (index + 1) + " of " + files.length + "…");
    try {
      const asset = await uploadAssetFile(file);
      state.albumAssets.set(asset.id, asset);
      if (!state.pickerAssets.some((item) => item.id === asset.id)) state.pickerAssets.unshift(asset);
      // Identical files collapse onto one asset server-side, so only count new playlist entries.
      if (state.albumDraftIds.includes(asset.id)) continue;
      state.albumDraftIds.push(asset.id);
      added += 1;
    } catch (error) { failures.push(file.name + " (" + error.message + ")"); }
  }
  renderAlbumPlaylist(); renderAlbumPicker();
  const summary = added + " image" + (added === 1 ? "" : "s") + " added to this album. Each uses crop framing; edit any image from the gallery to change it.";
  setMessage("album-picker-message", failures.length ? summary + " Skipped " + failures.length + ": " + failures.join(", ") : summary, failures.length ? "error" : "");
}
async function stopPlayback() {
  const button = byId("stop-playback");
  button.disabled = true;
  try {
    await api("/api/v1/displays/" + displayId + "/stop-playback", { method: "POST" });
    await loadDashboard();
    setMessage("job-message", "Playback stopped.");
  } catch (error) { setMessage("job-message", error.message, "error"); }
  finally { button.disabled = !state.display?.active_album_name; }
}
async function saveAlbum(event) {
  event.preventDefault();
  const assetIds = [...new Set(state.albumDraftIds)];
  state.albumDraftIds = assetIds;
  if (!assetIds.length) { setMessage("album-picker-message", "Use Add images to choose at least one image.", "error"); return; }
  const settings = {
    name: byId("album-name").value, order_mode: byId("album-order").value,
    interval_seconds: Number(byId("album-interval").value) * 60, enabled: true,
    time_zone: state.display?.time_zone || "Europe/Copenhagen", schedule_start_at: null, schedule_end_at: null,
  };
  try {
    if (state.editingAlbumId) {
      await api("/api/v1/albums/" + state.editingAlbumId, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings) });
      await api("/api/v1/albums/" + state.editingAlbumId + "/items", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_ids: assetIds }) });
      setMessage("album-message", "Album updated.");
    } else {
      await api("/api/v1/albums", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...settings, asset_ids: assetIds }) });
      setMessage("album-message", "Album created.");
    }
    closeAlbumEditor(); await loadAlbums();
  } catch (error) { setMessage("album-picker-message", error.message, "error"); }
}
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
byId("image-file").addEventListener("change", (event) => selectSourceFile(event.target.files[0]));
byId("fit-mode").addEventListener("change", renderLocalPreview);
byId("content-rotation").addEventListener("change", renderLocalPreview);
byId("orientation").addEventListener("change", () => {
  const rotation = byId("rotation");
  if (byId("orientation").value === "portrait" && ["0", "180"].includes(rotation.value)) rotation.value = "90";
  if (byId("orientation").value === "landscape" && ["90", "270"].includes(rotation.value)) rotation.value = "0";
});
byId("upload-button").addEventListener("click", async () => {
  setUploadButtons(true);
  try { await uploadCurrentImage(); setMessage("job-message", "Image saved to the gallery."); }
  catch (error) { setMessage("job-message", error.message, "error"); }
  finally { setUploadButtons(false); }
});
byId("display-button").addEventListener("click", async () => {
  setUploadButtons(true);
  try { const asset = await uploadCurrentImage(); await displayAsset(asset.id, renderSettings()); }
  catch (error) { setMessage("job-message", error.message, "error"); }
  finally { setUploadButtons(false); }
});
byId("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    renderDisplay(await api("/api/v1/displays/" + displayId + "/settings", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ orientation: byId("orientation").value, rotation: Number(byId("rotation").value) }) }));
    setMessage("connection-message", "Panel settings saved.");
  } catch (error) { setMessage("connection-message", error.message, "error"); }
});
byId("stop-playback").addEventListener("click", stopPlayback);
byId("play-selected").addEventListener("click", openQuickPlay);
byId("delete-selected").addEventListener("click", () => deleteAssets(selectedGalleryIds()).catch((error) => setMessage("gallery-message", error.message, "error")));
byId("new-album").addEventListener("click", startNewAlbum);
byId("back-to-albums").addEventListener("click", closeAlbumEditor);
byId("cancel-album-edit").addEventListener("click", closeAlbumEditor);
byId("open-album-picker").addEventListener("click", () => openAlbumPicker());
byId("close-album-picker").addEventListener("click", closeAlbumPicker);
byId("album-search").addEventListener("input", (event) => {
  state.pickerQuery = event.target.value;
  clearTimeout(state.pickerTimer);
  state.pickerTimer = setTimeout(() => resetAlbumPicker(), 200);
});
byId("album-image-file").addEventListener("change", (event) => uploadAlbumImages(event.target.files));
byId("album-form").addEventListener("submit", saveAlbum);
byId("close-image-editor").addEventListener("click", closeImageEditor);
byId("save-image-editor").addEventListener("click", saveImageEditor);
byId("image-editor-fit").addEventListener("change", renderImageEditorPreview);
byId("image-editor-rotation").addEventListener("change", renderImageEditorPreview);
byId("close-quick-play").addEventListener("click", closeQuickPlay);
byId("start-quick-play").addEventListener("click", startQuickPlay);
const pickerObserver = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadAlbumPickerPage(); }, { root: byId("album-picker-dialog"), rootMargin: "180px" });
pickerObserver.observe(byId("album-picker-sentinel"));
loadDashboard().catch((error) => setMessage("job-message", error.message, "error"));
