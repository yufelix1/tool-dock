const reviewState = {
    scanId: null,
    roots: [],
    games: [],
    emptyDirectories: [],
    ignoredDirectories: [],
    summary: {},
    view: "recordings",
    filter: "",
    timeRange: "",
    gameId: "",
    selectedRecordingKeys: new Set(),
    batchDeleting: false,
};

const TIME_RANGE_SECONDS = Object.freeze({
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
    "365d": 365 * 24 * 60 * 60,
});

const elements = {
    rootBadge: document.getElementById("root-badge"),
    refreshButton: document.getElementById("refresh-button"),
    openSettingsButton: document.getElementById("open-settings-button"),
    setupEmpty: document.getElementById("setup-empty"),
    setupButton: document.getElementById("setup-button"),
    status: document.getElementById("status"),
    content: document.getElementById("review-content"),
    recordingTabCount: document.getElementById("recording-tab-count"),
    favoriteTabCount: document.getElementById("favorite-tab-count"),
    emptyTabCount: document.getElementById("empty-tab-count"),
    gameFilter: document.getElementById("game-filter"),
    timeRangeFilter: document.getElementById("time-range-filter"),
    filterInput: document.getElementById("filter-input"),
    batchControls: document.getElementById("batch-controls"),
    selectAllRecordings: document.getElementById("select-all-recordings"),
    selectedRecordingCount: document.getElementById("selected-recording-count"),
    deleteSelectedRecordings: document.getElementById("delete-selected-recordings"),
    gameGroups: document.getElementById("game-groups"),
    recordingsEmpty: document.getElementById("recordings-empty"),
    favoritesView: document.getElementById("favorites-view"),
    favoriteGameGroups: document.getElementById("favorite-game-groups"),
    favoritesEmpty: document.getElementById("favorites-empty"),
    emptyView: document.getElementById("empty-view"),
    recordingsView: document.getElementById("recordings-view"),
    emptyDirectoryList: document.getElementById("empty-directory-list"),
    directoriesEmpty: document.getElementById("directories-empty"),
    emptyHeadingCount: document.getElementById("empty-heading-count"),
    cleanAllButton: document.getElementById("clean-all-button"),
    previewDialog: document.getElementById("preview-dialog"),
    previewVideo: document.getElementById("preview-video"),
    previewName: document.getElementById("preview-name"),
    previewLocation: document.getElementById("preview-location"),
    closePreviewButton: document.getElementById("close-preview-button"),
    settingsDialog: document.getElementById("settings-dialog"),
    settingsForm: document.getElementById("settings-form"),
    settingsRootPaths: document.getElementById("settings-root-paths"),
    settingsIgnoredDirectories: document.getElementById("settings-ignored-directories"),
    settingsError: document.getElementById("settings-error"),
    closeSettingsButton: document.getElementById("close-settings-button"),
    cancelSettingsButton: document.getElementById("cancel-settings-button"),
    saveSettingsButton: document.getElementById("save-settings-button"),
    toast: document.getElementById("toast"),
};

let toastTimer;
let directorySegmentLayoutFrame;

const directorySegmentObserver = typeof ResizeObserver === "function"
    ? new ResizeObserver(scheduleRecordingDirectorySegments)
    : null;

window.addEventListener("resize", scheduleRecordingDirectorySegments);

elements.settingsForm.addEventListener("submit", async event => {
    event.preventDefault();
    await scanRoots(parsePathList(elements.settingsRootPaths.value), {
        fromSettings: true,
        persist: true,
        ignoredDirectories: parsePathList(elements.settingsIgnoredDirectories.value),
    });
});

elements.openSettingsButton.addEventListener("click", openSettings);
elements.setupButton.addEventListener("click", openSettings);
elements.closeSettingsButton.addEventListener("click", closeSettings);
elements.cancelSettingsButton.addEventListener("click", closeSettings);
elements.refreshButton.addEventListener("click", () => scanRoots(reviewState.roots));

elements.gameFilter.addEventListener("change", event => {
    reviewState.gameId = event.target.value;
    clearRecordingSelection();
    updateViewCounts();
    renderRecordings();
    renderFavorites();
    renderEmptyDirectories();
    updateBatchControls();
});

elements.timeRangeFilter.addEventListener("change", event => {
    reviewState.timeRange = event.target.value;
    clearRecordingSelection();
    updateViewCounts();
    renderRecordings();
    renderFavorites();
    updateBatchControls();
});

elements.filterInput.addEventListener("input", event => {
    reviewState.filter = event.target.value.trim().toLowerCase();
    clearRecordingSelection();
    renderRecordings();
    renderFavorites();
    renderEmptyDirectories();
    updateBatchControls();
});

elements.selectAllRecordings.addEventListener("change", event => {
    visibleRecordings().forEach(recording => {
        const key = recordingKey(recording);
        if (event.target.checked) {
            reviewState.selectedRecordingKeys.add(key);
        } else {
            reviewState.selectedRecordingKeys.delete(key);
        }
    });
    syncRecordingSelectionControls();
    updateBatchControls();
});

elements.deleteSelectedRecordings.addEventListener("click", deleteSelectedRecordings);

document.querySelectorAll(".view-tab").forEach(tab => {
    tab.addEventListener("click", () => setView(tab.dataset.view));
});

elements.cleanAllButton.addEventListener("click", async () => {
    const count = filteredEmptyDirectories().length;
    if (!count || !confirm(`确定删除 ${count} 个空目录吗？此操作不可撤销。`)) return;
    await deleteEmptyDirectories();
});

elements.closePreviewButton.addEventListener("click", closePreview);
elements.previewDialog.addEventListener("close", stopPreview);
elements.previewDialog.addEventListener("click", event => {
    if (event.target === elements.previewDialog) closePreview();
});

initializeSettings();

async function requestJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || "请求失败");
    return data;
}

async function scanRoots(paths, options = {}) {
    const {
        fromSettings = false,
        persist = false,
        ignoredDirectories = reviewState.ignoredDirectories,
    } = options;
    if (!paths.length) {
        if (fromSettings) {
            showSettingsError("请至少输入一个游戏根目录");
            elements.settingsRootPaths.focus();
        } else {
            showStatus("请先配置游戏根目录");
        }
        return false;
    }

    setScanning(true);
    hideStatus();
    hideSettingsError();
    try {
        const data = await requestJson("/tools/game-recording-review/api/scan", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({paths, ignored_directories: ignoredDirectories}),
        });

        if (persist) {
            await saveSettings(data.roots, data.ignored_directories);
        }

        reviewState.scanId = data.scan_id;
        reviewState.roots = data.roots;
        reviewState.games = data.games;
        reviewState.emptyDirectories = data.empty_directories;
        reviewState.ignoredDirectories = data.ignored_directories;
        reviewState.summary = data.summary;
        clearRecordingSelection();
        elements.settingsRootPaths.value = data.roots.join("\n");
        elements.settingsIgnoredDirectories.value = data.ignored_directories.join("\n");
        updateRootBadge(data.roots);
        elements.setupEmpty.hidden = true;
        elements.content.hidden = false;
        updateGameFilter();
        renderAll();

        if (elements.settingsDialog.open) elements.settingsDialog.close();

        if (data.errors.length) {
            showStatus(`扫描完成，但有 ${data.errors.length} 个路径无法读取：${data.errors.join("；")}`, "warning");
        }
        return true;
    } catch (error) {
        if (fromSettings) {
            showSettingsError(error.message);
        } else {
            reviewState.scanId = null;
            elements.content.hidden = true;
            elements.setupEmpty.hidden = false;
            elements.rootBadge.textContent = "游戏根目录不可用";
            showStatus(error.message);
        }
        return false;
    } finally {
        setScanning(false);
    }
}

function setScanning(scanning) {
    elements.refreshButton.disabled = scanning || !reviewState.roots.length;
    elements.saveSettingsButton.disabled = scanning;
    elements.saveSettingsButton.textContent = scanning ? "正在扫描..." : "保存并扫描";
}

async function initializeSettings() {
    let settings;
    try {
        settings = await requestJson("/tools/game-recording-review/api/settings");
    } catch (error) {
        elements.setupEmpty.hidden = false;
        elements.content.hidden = true;
        elements.rootBadge.textContent = "设置加载失败";
        showStatus(error.message);
        return;
    }

    elements.settingsRootPaths.value = settings.roots.join("\n");
    elements.settingsIgnoredDirectories.value = settings.ignored_directories.join("\n");
    if (!settings.roots.length) {
        elements.setupEmpty.hidden = false;
        return;
    }

    updateRootBadge(settings.roots);
    await scanRoots(settings.roots, {
        ignoredDirectories: settings.ignored_directories,
    });
}

function openSettings() {
    if (reviewState.roots.length) {
        elements.settingsRootPaths.value = reviewState.roots.join("\n");
        elements.settingsIgnoredDirectories.value = reviewState.ignoredDirectories.join("\n");
    }
    hideSettingsError();
    elements.settingsDialog.showModal();
    elements.settingsRootPaths.focus();
    elements.settingsRootPaths.select();
}

function closeSettings() {
    elements.settingsDialog.close();
}

function showSettingsError(message) {
    elements.settingsError.hidden = false;
    elements.settingsError.textContent = message;
}

function hideSettingsError() {
    elements.settingsError.hidden = true;
    elements.settingsError.textContent = "";
}

function parsePathList(value) {
    return [...new Set(value.split(/\r?\n/).map(path => path.trim()).filter(Boolean))];
}

async function saveSettings(paths, ignoredDirectories) {
    return requestJson("/tools/game-recording-review/api/settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({roots: paths, ignored_directories: ignoredDirectories}),
    });
}

function updateRootBadge(roots) {
    elements.rootBadge.textContent = roots.length === 1
        ? roots[0]
        : `${roots.length} 个游戏根目录`;
    elements.rootBadge.title = roots.join("\n");
}

function updateGameFilter() {
    const previousGameId = reviewState.gameId;
    elements.gameFilter.replaceChildren(new Option("全部游戏", ""));
    reviewState.games.forEach(game => {
        elements.gameFilter.appendChild(new Option(game.game_id, game.game_id));
    });
    reviewState.gameId = reviewState.games.some(game => game.game_id === previousGameId)
        ? previousGameId
        : "";
    elements.gameFilter.value = reviewState.gameId;
}

function showStatus(message, type = "error") {
    elements.status.hidden = false;
    elements.status.className = `review-status ${type}`;
    elements.status.textContent = message;
}

function hideStatus() {
    elements.status.hidden = true;
    elements.status.textContent = "";
}

function renderAll() {
    updateViewCounts();
    renderRecordings();
    renderFavorites();
    renderEmptyDirectories();
    updateBatchControls();
}

function updateViewCounts() {
    const recordings = reviewState.games
        .flatMap(game => game.recordings)
        .filter(recordingMatchesScope);
    const emptyDirectoryCount = reviewState.emptyDirectories.filter(
        directory => !reviewState.gameId || directory.game_id === reviewState.gameId,
    ).length;

    elements.recordingTabCount.textContent = recordings.length;
    elements.favoriteTabCount.textContent = recordings.filter(recording => recording.favorite).length;
    elements.emptyTabCount.textContent = emptyDirectoryCount;
}

function recordingMatchesScope(recording) {
    if (reviewState.gameId && recording.game_id !== reviewState.gameId) return false;
    if (!reviewState.timeRange) return true;
    const rangeSeconds = TIME_RANGE_SECONDS[reviewState.timeRange];
    const createdAt = Number(recording.created_at ?? recording.mtime);
    return Number.isFinite(createdAt) && createdAt >= Date.now() / 1000 - rangeSeconds;
}

function recordingMatches(recording) {
    if (!recordingMatchesScope(recording)) return false;
    if (!reviewState.filter) return true;
    return [recording.game_id, recording.directory_id, recording.name, recording.root]
        .some(value => value.toLowerCase().includes(reviewState.filter));
}

function recordingKey(recording) {
    return JSON.stringify([recording.root, recording.path]);
}

function visibleRecordings() {
    if (reviewState.view === "empty") return [];
    return reviewState.games.flatMap(game => game.recordings).filter(recording => {
        if (reviewState.view === "favorites" && !recording.favorite) return false;
        return recordingMatches(recording);
    });
}

function clearRecordingSelection() {
    reviewState.selectedRecordingKeys.clear();
}

function syncRecordingSelectionControls() {
    document.querySelectorAll(".recording-select-checkbox").forEach(checkbox => {
        const selected = reviewState.selectedRecordingKeys.has(checkbox.recordingKey);
        checkbox.checked = selected;
        checkbox.disabled = reviewState.batchDeleting;
        checkbox.closest(".recording-card")?.classList.toggle("is-selected", selected);
    });
}

function updateBatchControls() {
    const active = reviewState.view !== "empty";
    elements.batchControls.hidden = !active;
    if (!active) return;

    const recordings = visibleRecordings();
    const selectedCount = recordings.filter(recording => (
        reviewState.selectedRecordingKeys.has(recordingKey(recording))
    )).length;
    elements.selectedRecordingCount.textContent = selectedCount;
    elements.selectedRecordingCount.hidden = selectedCount === 0;
    elements.selectedRecordingCount.setAttribute("aria-label", `已选 ${selectedCount} 项`);
    elements.selectAllRecordings.checked = recordings.length > 0
        && selectedCount === recordings.length;
    elements.selectAllRecordings.indeterminate = selectedCount > 0
        && selectedCount < recordings.length;
    elements.selectAllRecordings.disabled = reviewState.batchDeleting || !recordings.length;
    elements.deleteSelectedRecordings.disabled = reviewState.batchDeleting || !selectedCount;
    const deleteLabel = reviewState.batchDeleting ? "正在删除已选录屏" : "删除已选录屏";
    elements.deleteSelectedRecordings.setAttribute("aria-label", deleteLabel);
    elements.deleteSelectedRecordings.title = deleteLabel;
    elements.deleteSelectedRecordings.setAttribute("aria-busy", String(reviewState.batchDeleting));
}

function renderRecordings() {
    renderRecordingGroups(
        elements.gameGroups,
        elements.recordingsEmpty,
        recordingMatches,
    );
}

function renderFavorites() {
    const favoriteCount = reviewState.games.reduce(
        (count, game) => count + game.recordings.filter(recording => recording.favorite).length,
        0,
    );
    elements.favoritesEmpty.textContent = favoriteCount
        ? "没有匹配的收藏录屏"
        : "还没有收藏录屏";
    renderRecordingGroups(
        elements.favoriteGameGroups,
        elements.favoritesEmpty,
        recording => recording.favorite && recordingMatches(recording),
    );
}

function renderRecordingGroups(container, emptyElement, predicate) {
    container.querySelectorAll(".recording-grid").forEach(grid => {
        directorySegmentObserver?.unobserve(grid);
    });
    container.replaceChildren();
    let visibleCount = 0;

    reviewState.games.forEach(game => {
        const recordings = game.recordings.filter(predicate);
        if (!recordings.length) return;
        visibleCount += recordings.length;

        const section = document.createElement("section");
        section.className = "game-group";

        const header = document.createElement("div");
        header.className = "game-group-header";
        const title = document.createElement("h2");
        title.textContent = game.game_id;
        const count = document.createElement("span");
        count.textContent = `${recordings.length} 个录屏`;
        header.append(title, count);

        const grid = document.createElement("div");
        grid.className = "recording-grid";
        groupRecordingsByDirectory(recordings).forEach(group => {
            if (group.recordings.length === 1) {
                grid.appendChild(createRecordingCard(group.recordings[0]));
                return;
            }
            grid.appendChild(createRecordingDirectoryCluster(group));
        });
        section.append(header, grid);
        container.appendChild(section);
        directorySegmentObserver?.observe(grid);
    });

    emptyElement.hidden = visibleCount > 0;
    scheduleRecordingDirectorySegments();
}

function groupRecordingsByDirectory(recordings) {
    const groups = new Map();
    recordings.forEach(recording => {
        const key = JSON.stringify([recording.root, recording.directory_path]);
        if (!groups.has(key)) {
            groups.set(key, {
                root: recording.root,
                directoryId: recording.directory_id,
                directoryPath: recording.directory_path,
                recordings: [],
            });
        }
        groups.get(key).recordings.push(recording);
    });
    return [...groups.values()];
}

function createRecordingDirectoryCluster(group) {
    const cluster = document.createElement("div");
    cluster.className = "recording-directory-cluster";
    cluster.setAttribute(
        "aria-label",
        `目录 ${group.directoryId}，${group.recordings.length} 个录屏`,
    );
    cluster.setAttribute("role", "group");

    group.recordings.forEach(recording => {
        const cell = document.createElement("div");
        cell.className = "recording-directory-cell";

        const label = document.createElement("span");
        label.className = "recording-directory-cluster-label";
        label.textContent = `${group.directoryId} · ${group.recordings.length} 个录屏`;
        label.title = `${group.root}/${group.directoryPath}`;

        cell.append(label, createRecordingCard(recording, {grouped: true}));
        cluster.appendChild(cell);
    });
    return cluster;
}

function scheduleRecordingDirectorySegments() {
    if (directorySegmentLayoutFrame) cancelAnimationFrame(directorySegmentLayoutFrame);
    directorySegmentLayoutFrame = requestAnimationFrame(() => {
        directorySegmentLayoutFrame = null;
        syncRecordingDirectorySegments();
    });
}

function syncRecordingDirectorySegments() {
    document.querySelectorAll(".recording-directory-cluster").forEach(cluster => {
        const cells = [...cluster.children].filter(child => (
            child.classList.contains("recording-directory-cell")
        ));
        cells.forEach(cell => {
            cell.classList.remove("is-segment-start", "is-segment-end");
        });

        const rows = [];
        cells.forEach(cell => {
            const rect = cell.getBoundingClientRect();
            if (!rect.width) return;
            let row = rows.find(item => Math.abs(item.top - rect.top) < 2);
            if (!row) {
                row = {top: rect.top, cells: []};
                rows.push(row);
            }
            row.cells.push(cell);
        });

        rows.forEach(row => {
            row.cells[0]?.classList.add("is-segment-start");
            row.cells.at(-1)?.classList.add("is-segment-end");
        });
    });
}

function createRecordingCard(recording, options = {}) {
    const {grouped = false} = options;
    const article = document.createElement("article");
    article.className = "recording-card";
    const key = recordingKey(recording);
    const selected = reviewState.selectedRecordingKeys.has(key);
    article.classList.toggle("is-selected", selected);

    const selectionControl = document.createElement("label");
    selectionControl.className = "recording-select-control";
    const selectionInput = document.createElement("input");
    selectionInput.className = "recording-select-checkbox";
    selectionInput.type = "checkbox";
    selectionInput.checked = selected;
    selectionInput.disabled = reviewState.batchDeleting;
    selectionInput.recordingKey = key;
    selectionInput.setAttribute("aria-label", `选择 ${recording.name}`);
    selectionInput.addEventListener("change", event => {
        if (event.target.checked) {
            reviewState.selectedRecordingKeys.add(key);
        } else {
            reviewState.selectedRecordingKeys.delete(key);
        }
        article.classList.toggle("is-selected", event.target.checked);
        updateBatchControls();
    });
    const selectionGlyph = document.createElement("span");
    selectionGlyph.className = "recording-select-glyph";
    selectionGlyph.setAttribute("aria-hidden", "true");
    selectionControl.append(selectionInput, selectionGlyph);

    const coverButton = document.createElement("button");
    coverButton.className = "cover-button";
    coverButton.type = "button";
    coverButton.title = `预览 ${recording.name}`;
    coverButton.setAttribute("aria-label", `预览 ${recording.name}`);
    coverButton.addEventListener("click", () => openPreview(recording));

    if (recording.cover_path) {
        const image = document.createElement("img");
        image.src = mediaUrl(recording.root, recording.cover_path);
        image.alt = `${recording.name} 封面`;
        image.loading = "lazy";
        image.decoding = "async";
        coverButton.appendChild(image);
    } else {
        const placeholder = document.createElement("span");
        placeholder.className = "cover-placeholder";
        const label = document.createElement("span");
        label.textContent = "无封面";
        placeholder.appendChild(label);
        coverButton.appendChild(placeholder);
    }

    const playIndicator = document.createElement("span");
    playIndicator.className = "play-indicator";
    playIndicator.textContent = "▶";
    playIndicator.setAttribute("aria-hidden", "true");
    coverButton.appendChild(playIndicator);

    const info = document.createElement("div");
    info.className = "recording-info";
    const name = document.createElement("h3");
    name.textContent = recording.name;

    const meta = document.createElement("div");
    meta.className = "recording-meta";
    const date = document.createElement("span");
    date.textContent = formatDate(recording.created_at ?? recording.mtime);
    const size = document.createElement("span");
    size.textContent = formatBytes(recording.size);
    meta.append(date, size);

    const directory = document.createElement("div");
    directory.className = "recording-directory";
    directory.title = `${recording.root}/${recording.directory_path}`;
    directory.textContent = grouped
        ? sourceLabel(recording.root)
        : `${recording.directory_id} · ${sourceLabel(recording.root)}`;

    info.append(name, meta, directory);

    const favoriteButton = document.createElement("button");
    favoriteButton.className = "recording-favorite-button";
    favoriteButton.type = "button";
    const favoriteGlyph = document.createElement("span");
    favoriteGlyph.className = "favorite-glyph";
    favoriteGlyph.setAttribute("aria-hidden", "true");
    favoriteButton.appendChild(favoriteGlyph);
    updateFavoriteButton(favoriteButton, recording);
    favoriteButton.addEventListener("click", () => toggleFavorite(recording, favoriteButton));

    const actions = document.createElement("div");
    actions.className = "recording-card-actions";
    actions.append(selectionControl, favoriteButton);

    article.append(coverButton, info, actions);
    return article;
}

function updateFavoriteButton(button, recording) {
    const favorite = Boolean(recording.favorite);
    const action = favorite ? "取消收藏" : "收藏";
    button.classList.toggle("is-favorite", favorite);
    button.querySelector(".favorite-glyph").textContent = favorite ? "★" : "☆";
    button.setAttribute("aria-pressed", String(favorite));
    button.setAttribute("aria-label", `${action} ${recording.name}`);
    button.title = `${action} ${recording.name}`;
}

async function toggleFavorite(recording, button) {
    const favorite = !recording.favorite;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");

    try {
        const data = await requestJson("/tools/game-recording-review/api/recording/favorite", {
            method: "PATCH",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                scan_id: reviewState.scanId,
                root: recording.root,
                path: recording.path,
                favorite,
            }),
        });
        recording.favorite = data.favorite;
        recording.favorited_at = data.favorited_at;
        reviewState.selectedRecordingKeys.delete(recordingKey(recording));
        updateFavoriteButton(button, recording);
        renderAll();
        showToast(data.favorite ? "已收藏录屏" : "已取消收藏");
    } catch (error) {
        showStatus(error.message);
    } finally {
        button.disabled = false;
        button.removeAttribute("aria-busy");
    }
}

function renderEmptyDirectories() {
    elements.emptyDirectoryList.replaceChildren();
    const directories = filteredEmptyDirectories();

    directories.forEach(directory => {
        const row = document.createElement("div");
        row.className = "empty-directory-row";
        const gameId = document.createElement("strong");
        gameId.textContent = directory.game_id;
        const path = document.createElement("code");
        path.textContent = `${directory.directory_id} · ${sourceLabel(directory.root)}`;
        path.title = `${directory.root}/${directory.path}`;
        const deleteButton = commandButton("删除", "delete-button", async () => {
            if (confirm(`确定删除空目录 ${directory.path} 吗？`)) {
                await deleteEmptyDirectories(directory);
            }
        });
        row.append(gameId, path, deleteButton);
        elements.emptyDirectoryList.appendChild(row);
    });

    elements.emptyDirectoryList.hidden = directories.length === 0;
    elements.directoriesEmpty.hidden = directories.length > 0;
    elements.emptyHeadingCount.textContent = directories.length;
    elements.cleanAllButton.disabled = directories.length === 0;
}

function filteredEmptyDirectories() {
    return reviewState.emptyDirectories.filter(directory => {
        if (reviewState.gameId && directory.game_id !== reviewState.gameId) return false;
        if (!reviewState.filter) return true;
        return [directory.game_id, directory.directory_id, directory.path, directory.root]
            .some(value => value.toLowerCase().includes(reviewState.filter));
    });
}

function commandButton(label, className, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
}

function setView(view) {
    reviewState.view = view;
    clearRecordingSelection();
    elements.timeRangeFilter.disabled = view === "empty";
    elements.recordingsView.hidden = view !== "recordings";
    elements.favoritesView.hidden = view !== "favorites";
    elements.emptyView.hidden = view !== "empty";
    document.querySelectorAll(".view-tab").forEach(tab => {
        const active = tab.dataset.view === view;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
    });
    syncRecordingSelectionControls();
    updateBatchControls();
}

function mediaUrl(root, relativePath) {
    const path = relativePath.split(/[\\/]/).map(encodeURIComponent).join("/");
    const query = new URLSearchParams({root});
    return `/tools/game-recording-review/media/${encodeURIComponent(reviewState.scanId)}/${path}?${query}`;
}

function openPreview(recording) {
    elements.previewName.textContent = recording.name;
    elements.previewLocation.textContent = `${recording.game_id} / ${recording.directory_id} · ${recording.root}`;
    elements.previewVideo.src = mediaUrl(recording.root, recording.path);
    elements.previewDialog.showModal();
    elements.previewVideo.play().catch(() => {});
}

function closePreview() {
    elements.previewDialog.close();
}

function stopPreview() {
    elements.previewVideo.pause();
    elements.previewVideo.removeAttribute("src");
    elements.previewVideo.load();
}

async function deleteSelectedRecordings() {
    const recordings = visibleRecordings().filter(recording => (
        reviewState.selectedRecordingKeys.has(recordingKey(recording))
    ));
    if (!recordings.length) return;
    if (!confirm(`确定删除选中的 ${recordings.length} 个录屏及其封面吗？此操作不可撤销。`)) return;

    reviewState.batchDeleting = true;
    syncRecordingSelectionControls();
    updateBatchControls();
    try {
        const data = await requestJson("/tools/game-recording-review/api/recordings", {
            method: "DELETE",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                scan_id: reviewState.scanId,
                recordings: recordings.map(recording => ({
                    root: recording.root,
                    path: recording.path,
                    size: recording.size,
                    mtime_ns: recording.mtime_ns,
                })),
            }),
        });
        clearRecordingSelection();
        await scanRoots(reviewState.roots);
        if (data.errors.length) {
            const details = data.errors.slice(0, 3)
                .map(error => `${error.path || "未知录屏"}：${error.message}`)
                .join("；");
            const remaining = data.errors.length - 3;
            showStatus(
                `已删除 ${data.deleted_count} 个录屏，${data.errors.length} 个失败：${details}${remaining > 0 ? `；另有 ${remaining} 个错误` : ""}`,
                "warning",
            );
        } else {
            showToast(`已删除 ${data.deleted_count} 个录屏`);
        }
    } catch (error) {
        showStatus(error.message);
    } finally {
        reviewState.batchDeleting = false;
        syncRecordingSelectionControls();
        updateBatchControls();
    }
}

async function deleteEmptyDirectories(directory = null) {
    try {
        const directories = directory ? [directory] : filteredEmptyDirectories();
        const data = await requestJson("/tools/game-recording-review/api/empty-directories", {
            method: "DELETE",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                scan_id: reviewState.scanId,
                directories: directories.map(item => ({
                    root: item.root,
                    path: item.path,
                })),
            }),
        });
        if (data.errors.length) {
            showStatus(data.errors.join("；"), "warning");
        } else {
            showToast(data.message);
        }
        await scanRoots(reviewState.roots);
    } catch (error) {
        showStatus(error.message);
    }
}

function sourceLabel(root) {
    return root.split(/[\\/]/).filter(Boolean).at(-1) || root;
}

function showToast(message) {
    clearTimeout(toastTimer);
    elements.toast.textContent = message;
    elements.toast.hidden = false;
    toastTimer = setTimeout(() => {
        elements.toast.hidden = true;
    }, 2600);
}

function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes / 1024;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
        value /= 1024;
        unitIndex += 1;
    }
    return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

function formatDate(timestamp) {
    return new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
    }).format(new Date(timestamp * 1000));
}
