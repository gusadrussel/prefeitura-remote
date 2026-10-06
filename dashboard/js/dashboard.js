(() => {
  const loginPanel = document.querySelector("#login-panel");
  const appView = document.querySelector("#app-view");
  const loginForm = document.querySelector("#login-form");
  const tokenInput = document.querySelector("#token-input");
  const loginError = document.querySelector("#login-error");
  const logoutButton = document.querySelector("#logout-button");
  const rows = document.querySelector("#computer-rows");
  const searchInput = document.querySelector("#search-input");
  const authorizationFilter = document.querySelector("#authorization-filter");
  const detailPanel = document.querySelector("#detail-panel");
  const globalError = document.querySelector("#global-error");
  const locale = "pt-BR";
  let computers = [];
  let selectedId = null;
  let searchTimer = null;
  let screenSocket = null;
  let screenComputerId = null;
  let screenState = "idle";
  let screenConsentRequestId = null;
  let screenConsentPollTimer = null;
  let screenFrameData = null;
  let screenLastFrameAt = null;
  let screenRenderSequence = 0;
  let controlState = "idle";
  let controlErrorMessage = "";
  let screenRemoteWidth = 0;
  let screenRemoteHeight = 0;
  const controlKeysDown = new Set();
  const controlButtonsDown = new Set();
  let lastControlPointer = { x: 0.5, y: 0.5 };
  let lastPointerMoveAt = 0;
  let pendingPointerMove = null;
  let pointerMoveTimer = null;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const formatBytes = (bytes) => {
    if (bytes == null || !Number.isFinite(Number(bytes))) return "Não informado";
    const value = Number(bytes);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = value;
    let index = 0;
    while (size >= 1000 && index < units.length - 1) { size /= 1000; index += 1; }
    return `${size.toLocaleString(locale, { maximumFractionDigits: 1 })} ${units[index]}`;
  };
  const formatTime = (value) => {
    if (!value) return "Nunca";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "Data inválida";
    return new Intl.DateTimeFormat(locale, { dateStyle: "short", timeStyle: "short" }).format(date);
  };
  const token = () => sessionStorage.getItem("dashboard_token") || "";
  const authHeaders = () => ({ Authorization: `Bearer ${token()}` });

  function showLogin(message = "") {
    if (screenComputerId !== null) closeScreenViewer();
    appView.classList.add("hidden");
    loginPanel.classList.remove("hidden");
    logoutButton.classList.add("hidden");
    loginError.textContent = message;
    tokenInput.focus();
  }

  function showApp() {
    loginPanel.classList.add("hidden");
    appView.classList.remove("hidden");
    logoutButton.classList.remove("hidden");
    loadComputers();
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { ...authHeaders(), ...(options.headers || {}) },
    });
    if (response.status === 401 || response.status === 503) {
      sessionStorage.removeItem("dashboard_token");
      showLogin(response.status === 401 ? "Token inválido ou expirado." : "Autenticação não configurada no servidor.");
      throw new Error("Acesso não autorizado.");
    }
    if (!response.ok) throw new Error(`Falha na API (${response.status}).`);
    return response.json();
  }

  async function loadComputers() {
    globalError.textContent = "";
    try {
      const search = new URLSearchParams();
      if (searchInput.value.trim()) search.set("search", searchInput.value.trim());
      if (authorizationFilter.value) search.set("authorization_status", authorizationFilter.value);
      computers = await api(`/api/v1/admin/computers?${search}`);
      renderComputers();
      document.querySelector("#updated-at").textContent = `Atualizado ${formatTime(new Date().toISOString())}`;
    } catch (error) {
      if (token()) globalError.textContent = error.message;
    }
  }

  function renderComputers() {
    const online = computers.filter((computer) => computer.online).length;
    const connectedChannels = computers.filter((computer) => computer.channel_connected).length;
    document.querySelector("#total-count").textContent = computers.length;
    document.querySelector("#online-count").textContent = online;
    document.querySelector("#offline-count").textContent = computers.length - online;
    document.querySelector("#channel-count").textContent = connectedChannels;
    document.querySelector("#result-count").textContent = `${computers.length} resultado${computers.length === 1 ? "" : "s"}`;
    if (selectedId !== null && !computers.some((computer) => computer.id === selectedId)) {
      if (screenComputerId !== null) closeScreenViewer();
      selectedId = null;
    }
    if (!computers.length) {
      rows.innerHTML = `<tr><td class="empty-state" colspan="7">${searchInput.value.trim() || authorizationFilter.value ? "Nenhum computador corresponde aos filtros." : "Nenhum computador cadastrado."}</td></tr>`;
      showPlaceholder("Nenhum equipamento encontrado", "Ajuste a busca ou aguarde o próximo inventário.");
      return;
    }
    rows.innerHTML = computers.map((computer) => `
      <tr data-id="${computer.id}" class="${selectedId === computer.id ? "selected" : ""}" tabindex="0" aria-label="Detalhes de ${escapeHtml(computer.hostname)}">
        <td><span class="hostname">${escapeHtml(computer.hostname)}</span><span class="cell-subtle">${escapeHtml(computer.agent_id.slice(0, 8))}</span></td>
        <td>${escapeHtml(computer.logged_user || "Não informado")}</td>
        <td>${escapeHtml(computer.os_name || "Não informado")}<span class="cell-subtle">${escapeHtml(computer.agent_version ? `Agente ${computer.agent_version}` : "")}</span></td>
        <td><span class="status ${escapeHtml(computer.authorization_status)}">${escapeHtml(authorizationLabel(computer.authorization_status))}</span></td>
        <td><span class="status ${computer.online ? "online" : "offline"}">${computer.online ? "Online" : "Offline"}</span></td>
        <td><span class="status ${computer.channel_connected ? "channel-active" : "channel-inactive"}">${computer.channel_connected ? "Conectado" : "Desconectado"}</span></td>
        <td>${escapeHtml(formatTime(computer.last_seen))}</td>
      </tr>`).join("");
    rows.querySelectorAll("tr[data-id]").forEach((row) => {
      const select = () => loadDetail(Number(row.dataset.id));
      row.addEventListener("click", select);
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
      });
    });
    if (selectedId !== null && computers.some((computer) => computer.id === selectedId)) loadDetail(selectedId);
  }

  function showPlaceholder(title, description) {
    detailPanel.innerHTML = `<div class="detail-placeholder"><span class="detail-symbol" aria-hidden="true">⌘</span><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p></div>`;
  }

  function authorizationLabel(status) {
    return ({ pending: "Pending", authorized: "Authorized", revoked: "Revoked" })[status] || "Pending";
  }

  async function changeAuthorization(id, action) {
    globalError.textContent = "";
    try {
      await api(`/api/v1/admin/computers/${id}/${action}`, { method: "POST" });
      await loadComputers();
    } catch (error) {
      if (token()) globalError.textContent = error.message;
    }
  }

  function detailRow(label, value) {
    return `<div class="detail-row"><span>${escapeHtml(label)}</span><span>${escapeHtml(value || "Não informado")}</span></div>`;
  }

  function section(title, content) {
    return `<section class="detail-section"><h3>${escapeHtml(title)}</h3>${content}</section>`;
  }

  async function loadDetail(id) {
    if (screenComputerId !== null && screenComputerId !== id) closeScreenViewer();
    selectedId = id;
    rows.querySelectorAll("tr[data-id]").forEach((row) => row.classList.toggle("selected", Number(row.dataset.id) === id));
    try {
      const result = await api(`/api/v1/admin/computers/${id}`);
      renderDetail(result.computer, result.inventory);
    } catch (error) {
      if (token()) globalError.textContent = error.message;
    }
  }

  function renderDetail(computer, inventory) {
    const system = inventory.system || {};
    const cpu = inventory.cpu || {};
    const memory = inventory.memory || {};
    const board = system.board || {};
    const bios = system.bios || {};
    const storage = inventory.storage || {};
    const authorizationAction = computer.authorization_status === "authorized"
      ? `<button class="authorization-button revoke-button" type="button" data-authorization-action="revoke">Revogar</button>`
      : `<button class="authorization-button" type="button" data-authorization-action="authorize">${computer.authorization_status === "revoked" ? "Reautorizar" : "Autorizar"}</button>`;
    const canStartScreen = computer.authorization_status === "authorized" && computer.channel_connected;
    const screenBusy = screenState === "connecting" || screenState === "starting" || screenState === "streaming" || screenState === "stopping";
    const canStartControl = canStartScreen && screenState === "streaming"
      && ["idle", "error"].includes(controlState);
    const screenFrame = screenFrameData
      ? `<img class="screen-frame" src="data:image/jpeg;base64,${screenFrameData}" alt="Tela de ${escapeHtml(computer.hostname)}">`
      : `<div class="screen-placeholder">Aguardando início da visualização</div>`;
    const ramModules = (memory.modules || []).map((module) => `
      <div class="hardware-item"><strong>${escapeHtml(formatBytes(module.capacity_bytes))} · ${escapeHtml(module.type)}</strong>
      <span>${escapeHtml(module.speed_mhz ? `${module.speed_mhz} MHz` : "Velocidade não informada")} · ${escapeHtml(module.manufacturer || "Fabricante não informado")}</span></div>`).join("");
    const disks = (storage.disks || []).map((disk) => `
      <div class="hardware-item"><strong>${escapeHtml(disk.model || disk.name)} · ${escapeHtml(disk.media_type)}</strong>
      <span>${escapeHtml(formatBytes(disk.size_bytes))}${disk.interface ? ` · ${escapeHtml(disk.interface)}` : ""}</span></div>`).join("");
    const volumes = (storage.volumes || []).map((volume) => `
      <div class="hardware-item"><strong>${escapeHtml(volume.mountpoint)} · ${escapeHtml(formatBytes(volume.total_bytes))}</strong>
      <span>${escapeHtml(formatBytes(volume.used_bytes))} usados · ${escapeHtml(formatBytes(volume.free_bytes))} livres</span></div>`).join("");
    const network = (inventory.network?.interfaces || []).map((item) => `
      <div class="hardware-item"><strong>${escapeHtml(item.name)}${item.is_up ? " · ativa" : " · inativa"}</strong>
      <span>${escapeHtml(item.mac || "MAC não informado")}</span>
      <div class="network-tags">${[...(item.ipv4 || []), ...(item.ipv6 || [])].map((address) => `<span>${escapeHtml(address)}</span>`).join("")}</div></div>`).join("");
    const gpus = (inventory.gpus || []).map((gpu) => `<div class="hardware-item"><strong>${escapeHtml(gpu.name)}</strong><span>${escapeHtml(gpu.driver_version || "")}</span></div>`).join("");

    detailPanel.innerHTML = `
      <header class="detail-header"><div class="detail-header-top"><div><span class="eyebrow">EQUIPAMENTO</span><h2>${escapeHtml(computer.hostname)}</h2><div class="detail-meta">${escapeHtml(computer.agent_id)}</div></div>
      <div class="detail-statuses"><span class="status ${escapeHtml(computer.authorization_status)}">${escapeHtml(authorizationLabel(computer.authorization_status))}</span><span class="status ${computer.online ? "online" : "offline"}">${computer.online ? "Online" : "Offline"}</span><span class="status ${computer.channel_connected ? "channel-active" : "channel-inactive"}">${computer.channel_connected ? "Canal conectado" : "Canal desconectado"}</span></div></div>
      <div class="detail-actions">${authorizationAction}</div></header>
      ${section("SISTEMA", detailRow("Usuário", computer.logged_user) + detailRow("Sistema", computer.os_name) + detailRow("Versão", computer.os_version) + detailRow("Arquitetura", system.architecture) + detailRow("Uptime", system.uptime_seconds ? `${Math.floor(system.uptime_seconds / 86400)} dias` : null) + detailRow("Último contato", formatTime(computer.last_seen)) + detailRow("Canal persistente", computer.channel_connected ? "Conectado" : "Desconectado") + detailRow("Conectado em", formatTime(computer.channel_connected_at)) + detailRow("Último heartbeat", formatTime(computer.channel_last_heartbeat)))}
      ${section("PROCESSADOR", detailRow("Modelo", cpu.model) + detailRow("Núcleos físicos", cpu.physical_cores) + detailRow("Núcleos lógicos", cpu.logical_cores))}
      ${section("MEMÓRIA", detailRow("Total", formatBytes(memory.total_bytes)) + (ramModules || `<div class="hardware-item"><span>Detalhes individuais indisponíveis.</span></div>`))}
      ${section("ARMAZENAMENTO", (disks || `<div class="hardware-item"><span>Nenhum disco identificado.</span></div>`) + (volumes || ""))}
      ${gpus ? section("VÍDEO", gpus) : ""}
      ${section("PLACA-MÃE E BIOS", detailRow("Fabricante", board.manufacturer) + detailRow("Modelo", board.model) + detailRow("Serial placa-mãe", board.serial) + detailRow("BIOS", bios.version) + detailRow("Serial BIOS", bios.serial))}
      ${section("VISUALIZAÇÃO DA TELA", `<div class="screen-meta"><span>Máquina: <strong>${escapeHtml(computer.hostname)}</strong></span><span>Canal: <strong>${computer.channel_connected ? "Conectado" : "Desconectado"}</strong></span><span>Transmissão: <strong data-screen-status>${escapeHtml(screenStateLabel())}</strong></span><span>Último frame: <strong data-screen-last-frame>${escapeHtml(formatTime(screenLastFrameAt))}</strong></span></div><div class="screen-viewer" data-screen-viewer><div class="screen-actions"><button class="screen-button" type="button" data-screen-action="start" ${!canStartScreen || screenBusy ? "disabled" : ""}>Solicitar acesso</button><button class="screen-button screen-stop-button" type="button" data-screen-action="stop" ${!screenBusy || screenState === "connecting" ? "disabled" : ""}>Parar</button><button class="screen-button" type="button" data-screen-action="control-start" ${!canStartControl ? "disabled" : ""}>Iniciar controle</button><button class="screen-button screen-stop-button" type="button" data-screen-action="control-stop" ${controlState === "idle" || controlState === "error" ? "disabled" : ""}>Encerrar controle</button><button class="screen-button screen-fullscreen-button" type="button" data-screen-action="fullscreen" ${screenState !== "streaming" ? "disabled" : ""}>Tela cheia</button><span class="control-status" data-control-status>${escapeHtml(controlStateLabel())}</span><span class="fullscreen-status" data-fullscreen-status>Janela</span></div><p class="control-error" data-control-error role="status">${escapeHtml(controlErrorMessage)}</p><div class="screen-frame-wrap" data-screen-frame tabindex="0" aria-label="Visualização remota de ${escapeHtml(computer.hostname)}">${screenFrame}</div></div><p class="screen-fullscreen-error" data-screen-fullscreen-error role="status"></p><p class="screen-hint">O usuário do computador precisa aceitar a solicitação antes de a tela ser compartilhada.</p>`)}
      ${section("REDE", detailRow("Gateways", (inventory.network?.gateways || []).join(", ")) + detailRow("DNS", (inventory.network?.dns_servers || []).join(", ")) + (network || `<div class="hardware-item"><span>Interfaces não disponíveis.</span></div>`))}`;
    detailPanel.querySelector("[data-authorization-action]")?.addEventListener("click", (event) => {
      changeAuthorization(computer.id, event.currentTarget.dataset.authorizationAction);
    });
    detailPanel.querySelector('[data-screen-action="start"]')?.addEventListener("click", () => {
      startScreenViewer(computer);
    });
    detailPanel.querySelector('[data-screen-action="stop"]')?.addEventListener("click", stopScreenViewer);
    detailPanel.querySelector('[data-screen-action="control-start"]')?.addEventListener("click", startRemoteControl);
    detailPanel.querySelector('[data-screen-action="control-stop"]')?.addEventListener("click", stopRemoteControl);
    detailPanel.querySelector('[data-screen-action="fullscreen"]')?.addEventListener("click", toggleScreenFullscreen);
    bindScreenInput();
    updateFullscreenButton();
  }

  function controlStateLabel() {
    return ({
      idle: "Controle desligado",
      starting: "Iniciando controle",
      active: "Controle ativo",
      stopping: "Encerrando controle",
      error: "Controle indisponível",
    })[controlState] || "Controle inativo";
  }

  function normalizedImagePoint(event, clampToImage = false) {
    const image = detailPanel.querySelector("[data-screen-frame] .screen-frame");
    if (!image || !image.naturalWidth || !image.naturalHeight) return null;
    const rect = image.getBoundingClientRect();
    const scale = Math.min(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    const left = rect.left + (rect.width - width) / 2;
    const top = rect.top + (rect.height - height) / 2;
    let x = (event.clientX - left) / width;
    let y = (event.clientY - top) / height;
    if (!clampToImage && (x < 0 || x > 1 || y < 0 || y > 1)) return null;
    x = Math.max(0, Math.min(1, x));
    y = Math.max(0, Math.min(1, y));
    return { x, y };
  }

  function sendControlInput(input) {
    if (controlState !== "active" || screenSocket?.readyState !== WebSocket.OPEN) return false;
    screenSocket.send(JSON.stringify({ type: "control.input", ...input }));
    return true;
  }

  function remoteKeyName(event) {
    if (/^Key[A-Z]$/.test(event.code)) return event.code.slice(3).toLowerCase();
    if (/^Digit[0-9]$/.test(event.code)) return event.code.slice(5);
    if (/^F([1-9]|1[0-2])$/.test(event.code)) return event.code.toLowerCase();
    return ({
      Enter: "enter", Escape: "esc", Tab: "tab", Space: "space", Backspace: "backspace",
      Delete: "delete", Insert: "insert", Home: "home", End: "end", PageUp: "pageup",
      PageDown: "pagedown", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left",
      ArrowRight: "right", ControlLeft: "ctrl", ControlRight: "ctrl", AltLeft: "alt",
      AltRight: "alt", ShiftLeft: "shift", ShiftRight: "shift", MetaLeft: "win", MetaRight: "win",
      Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\",
      Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/", Backquote: "`",
    })[event.code] || null;
  }

  function bindScreenInput() {
    const frameWrap = detailPanel.querySelector("[data-screen-frame]");
    const viewer = detailPanel.querySelector("[data-screen-viewer]");
    if (!frameWrap || !viewer) return;
    frameWrap.addEventListener("pointermove", (event) => {
      if (controlState !== "active") return;
      const point = normalizedImagePoint(event);
      if (!point) return;
      lastControlPointer = point;
      queuePointerMove(point);
    });
    frameWrap.addEventListener("pointerdown", (event) => {
      const button = event.button === 0 ? "left" : event.button === 2 ? "right" : null;
      const point = normalizedImagePoint(event);
      if (controlState !== "active" || !button || !point) return;
      event.preventDefault();
      frameWrap.focus({ preventScroll: true });
      try { frameWrap.setPointerCapture(event.pointerId); } catch {}
      lastControlPointer = point;
      flushPointerMove(point);
      if (sendControlInput({ action: "pointer.down", button, ...point })) {
        controlButtonsDown.add(button);
      }
    });
    frameWrap.addEventListener("pointerup", (event) => {
      const button = event.button === 0 ? "left" : event.button === 2 ? "right" : null;
      if (controlState !== "active" || !button || !controlButtonsDown.has(button)) return;
      const point = normalizedImagePoint(event, true) || lastControlPointer;
      sendControlInput({ action: "pointer.up", button, ...point });
      controlButtonsDown.delete(button);
    });
    frameWrap.addEventListener("pointercancel", releaseHeldInputs);
    frameWrap.addEventListener("contextmenu", (event) => {
      if (controlState === "active") event.preventDefault();
    });
    frameWrap.addEventListener("wheel", (event) => {
      if (controlState !== "active") return;
      const point = normalizedImagePoint(event);
      if (!point) return;
      event.preventDefault();
      sendControlInput({
        action: "pointer.scroll",
        delta_y: Math.max(-1200, Math.min(1200, event.deltaY)),
        ...point,
      });
    }, { passive: false });
    viewer.addEventListener("keydown", (event) => {
      if (controlState !== "active" || event.isComposing) return;
      if (event.target !== frameWrap && !event.target.classList?.contains("screen-frame")) return;
      const key = remoteKeyName(event);
      if (!key) return;
      event.preventDefault();
      if (event.repeat || controlKeysDown.has(key)) return;
      if (sendControlInput({ action: "key.down", key })) controlKeysDown.add(key);
    });
    viewer.addEventListener("keyup", (event) => {
      if (event.target !== frameWrap && !event.target.classList?.contains("screen-frame")) return;
      const key = remoteKeyName(event);
      if (controlState !== "active" || !key || !controlKeysDown.has(key)) return;
      event.preventDefault();
      sendControlInput({ action: "key.up", key });
      controlKeysDown.delete(key);
    });
  }

  function releaseHeldInputs() {
    flushPointerMove();
    for (const key of controlKeysDown) sendControlInput({ action: "key.up", key });
    for (const button of controlButtonsDown) {
      sendControlInput({ action: "pointer.up", button, ...lastControlPointer });
    }
    controlKeysDown.clear();
    controlButtonsDown.clear();
  }

  function clearHeldInputState() {
    if (pointerMoveTimer !== null) clearTimeout(pointerMoveTimer);
    pointerMoveTimer = null;
    pendingPointerMove = null;
    controlKeysDown.clear();
    controlButtonsDown.clear();
  }

  function queuePointerMove(point) {
    pendingPointerMove = point;
    const elapsed = performance.now() - lastPointerMoveAt;
    if (elapsed >= 50) {
      flushPointerMove();
      return;
    }
    if (pointerMoveTimer === null) {
      pointerMoveTimer = setTimeout(flushPointerMove, 50 - elapsed);
    }
  }

  function flushPointerMove(point = null) {
    if (pointerMoveTimer !== null) clearTimeout(pointerMoveTimer);
    pointerMoveTimer = null;
    const nextPoint = point || pendingPointerMove;
    pendingPointerMove = null;
    if (nextPoint && controlState === "active") {
      sendControlInput({ action: "pointer.move", ...nextPoint });
      lastPointerMoveAt = performance.now();
    }
  }

  function updateControlControls() {
    const status = detailPanel.querySelector("[data-control-status]");
    if (status) {
      status.textContent = controlStateLabel();
      status.classList.toggle("is-active", controlState === "active");
    }
    const error = detailPanel.querySelector("[data-control-error]");
    if (error) error.textContent = controlErrorMessage;
    const computer = computers.find((item) => item.id === selectedId);
    const startButton = detailPanel.querySelector('[data-screen-action="control-start"]');
    const stopButton = detailPanel.querySelector('[data-screen-action="control-stop"]');
    if (startButton) {
      startButton.disabled = !computer || computer.authorization_status !== "authorized"
        || !computer.channel_connected || screenState !== "streaming"
        || !["idle", "error"].includes(controlState);
    }
    if (stopButton) stopButton.disabled = !["starting", "active", "stopping"].includes(controlState);
  }

  function updateFullscreenButton() {
    const button = detailPanel.querySelector('[data-screen-action="fullscreen"]');
    const viewer = detailPanel.querySelector("[data-screen-viewer]");
    if (!button || !viewer) return;
    const isFullscreen = document.fullscreenElement === viewer;
    button.textContent = isFullscreen ? "Sair da tela cheia" : "Tela cheia";
    button.setAttribute("aria-pressed", String(isFullscreen));
    button.disabled = screenState !== "streaming" && !isFullscreen;
    const fullscreenStatus = detailPanel.querySelector("[data-fullscreen-status]");
    if (fullscreenStatus) fullscreenStatus.textContent = isFullscreen ? "Tela cheia ativa" : "Janela";
    if (isFullscreen && controlState === "active") {
      detailPanel.querySelector("[data-screen-frame]")?.focus({ preventScroll: true });
    }
  }

  async function toggleScreenFullscreen() {
    const viewer = detailPanel.querySelector("[data-screen-viewer]");
    const error = detailPanel.querySelector("[data-screen-fullscreen-error]");
    if (!viewer) return;
    if (error) error.textContent = "";
    console.info("[screen-fullscreen] click", {
      enabled: document.fullscreenEnabled,
      supported: typeof viewer.requestFullscreen === "function",
    });
    try {
      if (document.fullscreenElement === viewer) await document.exitFullscreen();
      else await viewer.requestFullscreen();
    } catch (reason) {
      console.error("[screen-fullscreen] rejected", {
        name: reason?.name || "UnknownError",
        message: reason?.message || "No rejection message",
        enabled: document.fullscreenEnabled,
        secureContext: window.isSecureContext,
      });
      if (error) error.textContent = `Tela cheia recusada: ${reason?.name || "erro do navegador"}`;
    }
  }

  async function startRemoteControl() {
    const computer = computers.find((item) => item.id === selectedId);
    if (
      !computer || computer.authorization_status !== "authorized" || !computer.channel_connected
      || screenState !== "streaming" || screenSocket?.readyState !== WebSocket.OPEN
      || !["idle", "error"].includes(controlState)
    ) return;
    if (!window.confirm(`Iniciar o controle de mouse e teclado em ${computer.hostname}?`)) return;
    controlState = "starting";
    controlErrorMessage = "";
    console.info("[remote-control] start requested");
    updateControlControls();
    screenSocket.send(JSON.stringify({ type: "control.start" }));
  }

  function stopRemoteControl() {
    if (!screenSocket || screenSocket.readyState !== WebSocket.OPEN
      || !["starting", "active"].includes(controlState)) return;
    releaseHeldInputs();
    controlState = "stopping";
    updateControlControls();
    screenSocket.send(JSON.stringify({ type: "control.stop" }));
  }

  function screenStateLabel() {
    return ({
      idle: "Parado",
      connecting: "Conectando",
      consent: "Aguardando aprovação do usuário",
      starting: "Iniciando",
      streaming: "Transmitindo",
      stopping: "Parando",
      stopped: "Parado",
      error: "Indisponível",
    })[screenState] || "Parado";
  }

  function updateScreenStatus(message) {
    const status = detailPanel.querySelector("[data-screen-status]");
    if (status) status.textContent = message || screenStateLabel();
    const lastFrame = detailPanel.querySelector("[data-screen-last-frame]");
    if (lastFrame) lastFrame.textContent = formatTime(screenLastFrameAt);
    const startButton = detailPanel.querySelector('[data-screen-action="start"]');
    const stopButton = detailPanel.querySelector('[data-screen-action="stop"]');
    if (startButton) {
      const computer = computers.find((item) => item.id === selectedId);
      startButton.disabled = !computer || computer.authorization_status !== "authorized"
        || !computer.channel_connected || ["connecting", "consent", "starting", "streaming", "stopping"].includes(screenState);
    }
    if (stopButton) {
      stopButton.disabled = !["consent", "starting", "streaming", "stopping"].includes(screenState);
    }
    updateControlControls();
    updateFullscreenButton();
  }

  async function startScreenViewer(computer) {
    if (computer.authorization_status !== "authorized" || !computer.channel_connected) return;
    if (screenSocket?.readyState === WebSocket.OPEN && screenComputerId === computer.id) {
      if (["idle", "stopped", "error"].includes(screenState)) {
        screenState = "starting";
        updateScreenStatus();
        screenSocket.send(JSON.stringify({ type: "screen.start" }));
      }
      return;
    }
    closeScreenViewer();
    screenComputerId = computer.id;
    screenState = "consent";
    screenConsentRequestId = null;
    screenFrameData = null;
    screenLastFrameAt = null;
    controlState = "idle";
    screenRemoteWidth = 0;
    screenRemoteHeight = 0;
    clearHeldInputState();
    const frameWrap = detailPanel.querySelector("[data-screen-frame]");
    if (frameWrap) frameWrap.innerHTML = '<div class="screen-placeholder">Aguardando aprovação do usuário no computador</div>';
    updateScreenStatus();
    try {
      const requestResult = await api(`/api/v1/admin/computers/${computer.id}/screen/access-request`, { method: "POST" });
      if (screenComputerId !== computer.id || screenState !== "consent") return;
      screenConsentRequestId = requestResult.request_id;
      const deadline = Date.now() + Number(requestResult.expires_in || 60) * 1000 + 1000;
      const poll = async () => {
        if (screenComputerId !== computer.id || screenState !== "consent" || !screenConsentRequestId) return;
        try {
          const result = await api(`/api/v1/admin/computers/${computer.id}/screen/access-request/${encodeURIComponent(screenConsentRequestId)}`);
          if (result.status === "approved" && result.ticket) {
            screenConsentRequestId = null;
            screenState = "connecting";
            updateScreenStatus("Acesso aprovado; conectando");
            const protocol = location.protocol === "https:" ? "wss:" : "ws:";
            const socket = new WebSocket(
              `${protocol}//${location.host}/ws/admin/computers/${computer.id}/screen?ticket=${encodeURIComponent(result.ticket)}`
            );
            screenSocket = socket;
            socket.addEventListener("open", () => {
              if (screenSocket !== socket) return;
              screenState = "starting";
              updateScreenStatus();
              socket.send(JSON.stringify({ type: "screen.start" }));
            });
            socket.addEventListener("message", (event) => {
              if (screenSocket !== socket) return;
              let message;
              try { message = JSON.parse(event.data); } catch { return; }
              if (message.type === "screen.start_ack") {
                screenState = message.accepted ? "streaming" : "error";
                screenRemoteWidth = Number.isInteger(message.screen_width) ? message.screen_width : 0;
                screenRemoteHeight = Number.isInteger(message.screen_height) ? message.screen_height : 0;
                updateScreenStatus(message.accepted ? "Transmitindo" : `Indisponível: ${message.reason || "captura recusada"}`);
              } else if (message.type === "screen.frame" && typeof message.data === "string") {
                screenLastFrameAt = new Date().toISOString();
                screenState = "streaming";
                const renderSequence = ++screenRenderSequence;
                const nextFrame = new Image();
                nextFrame.className = "screen-frame";
                nextFrame.alt = `Tela de ${computer.hostname}`;
                nextFrame.src = `data:image/jpeg;base64,${message.data}`;
                nextFrame.decode().then(() => {
                  if (screenSocket !== socket || renderSequence !== screenRenderSequence) return;
                  screenFrameData = message.data;
                  const frameWrap = detailPanel.querySelector("[data-screen-frame]");
                  if (!frameWrap) return;
                  const currentFrame = frameWrap.querySelector(".screen-frame");
                  if (currentFrame) currentFrame.replaceWith(nextFrame);
                  else frameWrap.replaceChildren(nextFrame);
                }).catch(() => {});
                updateScreenStatus("Transmitindo");
              } else if (message.type === "control.start_ack") {
                console.info("[remote-control] start ack", {
                  accepted: message.accepted === true,
                  reason: message.reason || "accepted",
                });
                if (message.accepted === true
                  && Number.isInteger(message.screen_width) && Number.isInteger(message.screen_height)
                  && message.screen_width === screenRemoteWidth && message.screen_height === screenRemoteHeight) {
                  controlState = "active";
                  controlErrorMessage = "";
                  detailPanel.querySelector("[data-screen-frame]")?.focus({ preventScroll: true });
                  updateScreenStatus("Controle ativo");
                } else {
                  controlState = "error";
                  controlErrorMessage = message.reason || "screen_resolution_mismatch";
                  if (message.accepted === true && screenSocket === socket) {
                    socket.send(JSON.stringify({ type: "control.stop" }));
                  }
                  updateScreenStatus("Controle não iniciado");
                }
              } else if (message.type === "control.stop_ack") {
                console.info("[remote-control] stop ack", { reason: message.reason || "operator_request" });
                controlState = "idle";
                controlErrorMessage = message.reason && message.reason !== "operator_request"
                  ? `Encerrado: ${message.reason}` : "";
                clearHeldInputState();
                updateScreenStatus(controlErrorMessage || "Controle encerrado");
              } else if (message.type === "screen.stop_ack") {
                screenState = "stopped";
                controlState = "idle";
                screenRemoteWidth = 0;
                screenRemoteHeight = 0;
                clearHeldInputState();
                updateScreenStatus("Parado");
              }
            });
            socket.addEventListener("close", (event) => {
              if (screenSocket !== socket) return;
              console.warn("[screen-viewer] websocket closed", { code: event.code, reason: event.reason || "no_reason" });
              screenSocket = null;
              screenComputerId = null;
              screenState = "stopped";
              controlState = "idle";
              clearHeldInputState();
              updateScreenStatus("Canal de visualização encerrado");
            });
            socket.addEventListener("error", () => {
              if (screenSocket !== socket) return;
              console.error("[screen-viewer] websocket error");
              screenState = "error";
              controlState = "idle";
              clearHeldInputState();
              updateScreenStatus("Falha no canal de visualização");
            });
            return;
          }
          if (["rejected", "expired", "cancelled"].includes(result.status)) {
            screenConsentRequestId = null;
            screenState = "error";
            updateScreenStatus(`Acesso não iniciado: ${result.reason || result.status}`);
            return;
          }
          if (Date.now() >= deadline) {
            screenConsentRequestId = null;
            screenState = "error";
            updateScreenStatus("A solicitação de acesso expirou");
            return;
          }
          screenConsentPollTimer = setTimeout(poll, 500);
        } catch (error) {
          if (screenState === "consent") {
            screenState = "error";
            updateScreenStatus(error.message);
          }
        }
      };
      await poll();
    } catch (error) {
      if (screenState === "consent") {
        screenState = "error";
        updateScreenStatus(error.message);
      }
    }
  }

  function stopScreenViewer() {
    if (screenState === "consent" && screenConsentRequestId && screenComputerId !== null) {
      const computerId = screenComputerId;
      const requestId = screenConsentRequestId;
      screenConsentRequestId = null;
      if (screenConsentPollTimer !== null) clearTimeout(screenConsentPollTimer);
      screenConsentPollTimer = null;
      api(`/api/v1/admin/computers/${computerId}/screen/access-request/${encodeURIComponent(requestId)}/cancel`, { method: "POST" }).catch(() => {});
      screenState = "stopped";
      updateScreenStatus("Solicitação cancelada");
      return;
    }
    if (!screenSocket || screenSocket.readyState !== WebSocket.OPEN) return;
    if (["starting", "streaming"].includes(screenState)) {
      if (["starting", "active"].includes(controlState)) {
        releaseHeldInputs();
        controlState = "stopping";
      }
      screenState = "stopping";
      updateScreenStatus();
      screenSocket.send(JSON.stringify({ type: "screen.stop" }));
    }
  }

  function closeScreenViewer() {
    if (screenConsentPollTimer !== null) clearTimeout(screenConsentPollTimer);
    screenConsentPollTimer = null;
    if (screenConsentRequestId && screenComputerId !== null) {
      api(`/api/v1/admin/computers/${screenComputerId}/screen/access-request/${encodeURIComponent(screenConsentRequestId)}/cancel`, { method: "POST" }).catch(() => {});
    }
    screenConsentRequestId = null;
    const socket = screenSocket;
    releaseHeldInputs();
    screenSocket = null;
    screenRenderSequence += 1;
    screenComputerId = null;
    screenState = "stopped";
    controlState = "idle";
    screenRemoteWidth = 0;
    screenRemoteHeight = 0;
    clearHeldInputState();
    screenFrameData = null;
    screenLastFrameAt = null;
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "viewer mudou de máquina");
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    sessionStorage.setItem("dashboard_token", tokenInput.value.trim());
    loginError.textContent = "";
    try {
      const response = await fetch("/api/v1/computers", { headers: authHeaders() });
      if (!response.ok) throw new Error(response.status === 401 ? "Token inválido." : "Não foi possível acessar o servidor.");
      showApp();
    } catch (error) {
      sessionStorage.removeItem("dashboard_token");
      loginError.textContent = error.message;
    }
  });

  logoutButton.addEventListener("click", () => {
    closeScreenViewer();
    sessionStorage.removeItem("dashboard_token");
    computers = [];
    selectedId = null;
    searchInput.value = "";
    showLogin();
  });
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadComputers, 180);
  });
  authorizationFilter.addEventListener("change", loadComputers);
  document.querySelector("#refresh-button").addEventListener("click", loadComputers);
  document.addEventListener("keydown", (event) => {
    if (!event.defaultPrevented && event.key === "/"
      && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      searchInput.focus();
    }
  });
  document.addEventListener("fullscreenchange", () => {
    console.info("[screen-fullscreen] changed", {
      active: document.fullscreenElement !== null,
    });
    updateFullscreenButton();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) releaseHeldInputs();
  });
  window.addEventListener("blur", releaseHeldInputs);

  if (token()) showApp(); else showLogin();
})();