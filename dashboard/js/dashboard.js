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
  let screenFrameData = null;
  let screenLastFrameAt = null;

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
      ${section("VISUALIZAÇÃO DA TELA", `<div class="screen-meta"><span>Máquina: <strong>${escapeHtml(computer.hostname)}</strong></span><span>Canal: <strong>${computer.channel_connected ? "Conectado" : "Desconectado"}</strong></span><span>Transmissão: <strong data-screen-status>${escapeHtml(screenStateLabel())}</strong></span><span>Último frame: <strong data-screen-last-frame>${escapeHtml(formatTime(screenLastFrameAt))}</strong></span></div><div class="screen-actions"><button class="screen-button" type="button" data-screen-action="start" ${!canStartScreen || screenBusy ? "disabled" : ""}>Iniciar visualização</button><button class="screen-button screen-stop-button" type="button" data-screen-action="stop" ${!screenBusy || screenState === "connecting" ? "disabled" : ""}>Parar</button></div><div class="screen-frame-wrap" data-screen-frame>${screenFrame}</div><p class="screen-hint">A transmissão só começa após solicitação do técnico.</p>`)}
      ${section("REDE", detailRow("Gateways", (inventory.network?.gateways || []).join(", ")) + detailRow("DNS", (inventory.network?.dns_servers || []).join(", ")) + (network || `<div class="hardware-item"><span>Interfaces não disponíveis.</span></div>`))}`;
    detailPanel.querySelector("[data-authorization-action]")?.addEventListener("click", (event) => {
      changeAuthorization(computer.id, event.currentTarget.dataset.authorizationAction);
    });
    detailPanel.querySelector('[data-screen-action="start"]')?.addEventListener("click", () => {
      startScreenViewer(computer);
    });
    detailPanel.querySelector('[data-screen-action="stop"]')?.addEventListener("click", stopScreenViewer);
  }

  function screenStateLabel() {
    return ({
      idle: "Parado",
      connecting: "Conectando",
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
        || !computer.channel_connected || ["connecting", "starting", "streaming", "stopping"].includes(screenState);
    }
    if (stopButton) {
      stopButton.disabled = !["starting", "streaming", "stopping"].includes(screenState);
    }
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
    screenState = "connecting";
    screenFrameData = null;
    screenLastFrameAt = null;
    const frameWrap = detailPanel.querySelector("[data-screen-frame]");
    if (frameWrap) frameWrap.innerHTML = '<div class="screen-placeholder">Aguardando primeiro frame</div>';
    updateScreenStatus();
    try {
      const result = await api(`/api/v1/admin/computers/${computer.id}/screen/viewer-ticket`, { method: "POST" });
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
          updateScreenStatus(message.accepted ? "Transmitindo" : `Indisponível: ${message.reason || "captura recusada"}`);
        } else if (message.type === "screen.frame" && typeof message.data === "string") {
          screenFrameData = message.data;
          screenLastFrameAt = new Date().toISOString();
          screenState = "streaming";
          const frameWrap = detailPanel.querySelector("[data-screen-frame]");
          if (frameWrap) frameWrap.innerHTML = `<img class="screen-frame" src="data:image/jpeg;base64,${message.data}" alt="Tela de ${escapeHtml(computer.hostname)}">`;
          updateScreenStatus("Transmitindo");
        } else if (message.type === "screen.stop_ack") {
          screenState = "stopped";
          updateScreenStatus("Parado");
        }
      });
      socket.addEventListener("close", () => {
        if (screenSocket !== socket) return;
        screenSocket = null;
        screenComputerId = null;
        screenState = "stopped";
        updateScreenStatus("Canal de visualização encerrado");
      });
      socket.addEventListener("error", () => {
        if (screenSocket !== socket) return;
        screenState = "error";
        updateScreenStatus("Falha no canal de visualização");
      });
    } catch (error) {
      screenSocket = null;
      screenComputerId = null;
      screenState = "error";
      updateScreenStatus(error.message);
    }
  }

  function stopScreenViewer() {
    if (!screenSocket || screenSocket.readyState !== WebSocket.OPEN) return;
    if (["starting", "streaming"].includes(screenState)) {
      screenState = "stopping";
      updateScreenStatus();
      screenSocket.send(JSON.stringify({ type: "screen.stop" }));
    }
  }

  function closeScreenViewer() {
    const socket = screenSocket;
    screenSocket = null;
    screenComputerId = null;
    screenState = "stopped";
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
    if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      searchInput.focus();
    }
  });

  if (token()) showApp(); else showLogin();
})();