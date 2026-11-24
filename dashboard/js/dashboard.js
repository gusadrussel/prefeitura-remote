(() => {
  const loginPanel = document.querySelector("#login-panel");
  const appView = document.querySelector("#app-view");
  const loginForm = document.querySelector("#login-form");
  const tokenInput = document.querySelector("#token-input");
  const usernameInput = document.querySelector("#username-input");
  const passwordInput = document.querySelector("#password-input");
  const themeButton = document.querySelector("#theme-button");
  const loginError = document.querySelector("#login-error");
  const logoutButton = document.querySelector("#logout-button");
  const rows = document.querySelector("#computer-rows");
  const searchInput = document.querySelector("#search-input");
  const authorizationFilter = document.querySelector("#authorization-filter");
  const detailPanel = document.querySelector("#detail-panel");
  const globalError = document.querySelector("#global-error");
  const installationCodes = document.querySelector("#installation-codes");
  const issueCode = document.querySelector("#issue-installation-code");
  const codeError = document.querySelector("#installation-code-error");
  const codeDialog = document.querySelector("#installation-code-dialog");
  const issuedCode = document.querySelector("#issued-installation-code");
  let installationSession = 0;
  const locale = "pt-BR";
  let computers = [];
  let selectedId = null;
  let currentView = "overview";
  let computerLoad = 0;
  let detailLoad = 0;
  let sessionsLoad = 0;
  let activeSessions = [];
  const connectionFilter = document.querySelector("#connection-filter");
  const codeAvailability = document.querySelector("#code-availability");
  let screenSocket = null;
  let screenComputerId = null;
  let screenState = "idle";
  let screenConsentRequestId = null;
  let screenConsentPollTimer = null;
  let controlConsentRequestId = null;
  let controlConsentPollTimer = null;
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
  let chatPollTimer = null;
  let chatLastMessageId = 0;

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
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    themeButton.textContent = theme === "dark" ? "☀" : "◐";
    themeButton.title = theme === "dark" ? "Usar modo claro" : "Usar modo escuro";
    themeButton.setAttribute("aria-label", themeButton.title);
    localStorage.setItem("vyntra-theme", theme);
  }
  applyTheme(localStorage.getItem("vyntra-theme") || "dark");
  themeButton.addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

  const views = {
    overview: "Acompanhe os computadores cadastrados.",
    computers: "Consulte os dispositivos e solicite suporte remoto.",
    pending: "Revise os computadores que aguardam aprovação.",
    codes: "Autorize o cadastro de novos dispositivos.",
    sessions: "Acompanhe a visualização e o controle remoto desta aba.",
    settings: "",
  };

  function selectView(view) {
    currentView = view;
    const listView = view === "computers" || view === "pending";
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      panel.classList.toggle("hidden", panel.id !== (listView ? "computers-view" : view + "-view"));
    });
    document.querySelectorAll("[data-view]").forEach((button) => {
      if (button.dataset.view === view) {
        button.setAttribute("aria-current", "page");
        document.querySelector("#view-title").textContent = button.textContent.trim();
      } else {
        button.removeAttribute("aria-current");
      }
    });
    document.querySelector("#view-description").textContent = views[view];
    authorizationFilter.closest("label").classList.toggle("hidden", view === "pending");
    releaseHeldInputs();
    if (listView) renderComputers();
    if (view === "sessions") {
      updateConsoleSession();
      loadRemoteSessions();
    }
    if (view === "settings") loadAdminTools();
  }

  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => selectView(button.dataset.view));
  });
  document.querySelector("#return-to-session").addEventListener("click", () => selectView("computers"));

  async function checkServer() {
    const state = document.querySelector("#server-state");
    const dot = document.querySelector("#server-dot");
    try {
      const response = await fetch("/api/v1/health", { cache: "no-store", signal: AbortSignal.timeout(5000) });
      if (!response.ok || (await response.json()).status !== "ok") throw new Error();
      state.textContent = "Servidor operacional";
      dot.dataset.state = "online";
    } catch {
      state.textContent = "Servidor indisponível";
      dot.dataset.state = "offline";
    }
  }

  function updateConsoleSession() {
    const computer = computers.find((item) => item.id === screenComputerId);
    const hasSession = screenComputerId !== null && !["idle", "stopped"].includes(screenState);
    document.querySelector("#console-session").textContent = hasSession
      ? `${computer?.display_name || computer?.hostname || "Computador remoto"} · ${screenStateLabel()} · ${controlStateLabel()}`
      : "Nenhuma sessão iniciada nesta aba.";
    document.querySelector("#return-to-session").classList.toggle("hidden", !hasSession);
  }

  function renderRemoteSessions() {
    const names = new Map(computers.map((computer) => [computer.id, computer.display_name || computer.hostname]));
    document.querySelector("#remote-session-list").innerHTML = activeSessions.map((session) => `
      <li><strong>${escapeHtml(names.get(session.computer_id) || `Computador #${session.computer_id}`)}</strong>
      <span>${session.mode === "control" ? "Controle remoto" : "Visualização"}</span></li>`).join("");
  }

  async function loadRemoteSessions() {
    const load = ++sessionsLoad;
    const status = document.querySelector("#remote-sessions-status");
    const count = document.querySelector("#session-count");
    const note = document.querySelector("#session-count-note");
    try {
      const response = await fetch("/api/v1/admin/remote-sessions", { headers: authHeaders(), cache: "no-store" });
      const sessions = response.ok ? await response.json() : [];
      if (load !== sessionsLoad || !token()) return;
      if (response.status === 401) {
        sessionStorage.removeItem("dashboard_token");
        showLogin("Token inválido ou expirado.");
        return;
      }
      if (!response.ok) {
        status.textContent = response.status === 403 ? "A lista do servidor exige acesso administrativo."
          : "O servidor está sem dados de sessões.";
        count.textContent = "—";
        note.textContent = response.status === 403 ? "Acesso administrativo" : "Indisponível";
      } else {
        status.textContent = sessions.length ? "" : "Nenhuma sessão remota ativa.";
        count.textContent = sessions.length;
        note.textContent = "";
      }
      activeSessions = sessions;
      renderRemoteSessions();
    } catch {
      if (load !== sessionsLoad || !token()) return;
      activeSessions = [];
      status.textContent = "Não foi possível consultar as sessões.";
      count.textContent = "—";
      note.textContent = "Indisponível";
      renderRemoteSessions();
    }
  }

  function showLogin(message = "") {
    installationSession += 1;
    computerLoad += 1;
    detailLoad += 1;
    sessionsLoad += 1;
    activeSessions = [];
    renderRemoteSessions();
    document.querySelector("#session-count-note").textContent = "";
    document.querySelector("#remote-sessions-status").textContent = "Consultando sessões…";
    computers = [];
    selectedId = null;
    rows.replaceChildren();
    document.querySelector("#admin-tools").classList.add("hidden");
    document.querySelector("#new-tech-button").classList.add("hidden");
    document.querySelector("#updated-at").textContent = "";
    document.querySelector("#global-error").textContent = "";
    document.querySelector("#recent-devices").innerHTML = "<li>Carregando inventário…</li>";
    document.querySelectorAll(".summary-item strong").forEach((metric) => { metric.textContent = "—"; });
    showPlaceholder("Selecione um computador", "Consulte informações e ações do dispositivo.");
    installationCodes.classList.add("hidden");
    issuedCode.value = "";
    codeError.textContent = "";
    codeDialog.close();
    if (chatPollTimer !== null) { clearInterval(chatPollTimer); chatPollTimer = null; }
    if (controlConsentPollTimer !== null) { clearInterval(controlConsentPollTimer); controlConsentPollTimer = null; }
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
    selectView("overview");
    loadComputers();
    loadInstallationCodes();
    loadRemoteSessions();
  }

  async function loadInstallationCodes() {
    const session = installationSession;
    installationCodes.classList.add("hidden");
    document.querySelector("#new-tech-button").classList.add("hidden");
    codeAvailability.classList.remove("hidden");
    codeAvailability.textContent = "Verificando permissão para emitir códigos…";
    try {
      const response = await fetch("/api/v1/admin/installation-codes", { headers: authHeaders(), cache: "no-store" });
      if (session !== installationSession) return;
      if (response.ok) {
        installationCodes.classList.remove("hidden");
        document.querySelector("#new-tech-button").classList.remove("hidden");
        codeAvailability.classList.add("hidden");
      } else {
        codeAvailability.textContent = response.status === 403 ? "A emissão de códigos exige acesso administrativo."
          : "A emissão de códigos está indisponível.";
      }
    } catch {
      if (session === installationSession) codeAvailability.textContent = "Não foi possível verificar a disponibilidade.";
    }
  }

  issueCode.addEventListener("click", async () => {
    const session = installationSession;
    issueCode.disabled = true;
    codeError.textContent = "";
    try {
      const result = await api("/api/v1/admin/installation-codes", { method: "POST", cache: "no-store" });
      if (session !== installationSession) return;
      issuedCode.value = result.code;
      codeDialog.showModal();
      issuedCode.select();
    } catch (error) {
      issuedCode.value = "";
      if (session === installationSession) codeError.textContent = error.message;
    } finally {
      issueCode.disabled = false;
    }
  });
  codeDialog.addEventListener("close", () => { issuedCode.value = ""; });
  window.addEventListener("pagehide", () => {
    installationSession += 1;
    issuedCode.value = "";
    codeDialog.close();
  });

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { ...authHeaders(), ...(options.headers || {}) },
    });
    if (response.status === 401) {
      sessionStorage.removeItem("dashboard_token");
      showLogin("Token inválido ou expirado.");
      throw new Error("Acesso não autorizado.");
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      const messages = {
        400: "Confira os dados informados.",
        403: "Esta ação não está disponível para seu perfil ou para este computador.",
        404: "O item solicitado não foi encontrado.",
        409: "O computador mudou de estado. Atualize a lista e tente novamente.",
        429: "Muitas tentativas. Aguarde alguns instantes e tente novamente.",
        500: "O servidor encontrou um problema.",
        502: "O servidor está temporariamente indisponível.",
        503: "O serviço está temporariamente indisponível.",
      };
      const supportDetails = [
        "Agente não conectado ao canal",
        "Não foi possível solicitar confirmação no computador",
        "Visualização consentida ativa obrigatória",
        "Auditoria indisponível",
      ];
      throw new Error(supportDetails.includes(detail.detail) ? detail.detail :
        messages[response.status] || "Não foi possível concluir a operação.");
    }
    return response.json();
  }

  async function loadComputers() {
    const load = ++computerLoad;
    globalError.textContent = "";
    try {
      const loaded = new Map();
      const pageSize = 500;
      for (let offset = 0; ; offset += pageSize) {
        const page = await api(`/api/v1/admin/computers?limit=${pageSize}&offset=${offset}`);
        if (load !== computerLoad) return;
        const before = loaded.size;
        page.forEach((computer) => loaded.set(computer.id, computer));
        if (page.length < pageSize) break;
        if (before === loaded.size) throw new Error("O servidor não oferece paginação para este inventário.");
      }
      computers = [...loaded.values()];
      renderComputers();
      renderRecentDevices();
      renderRemoteSessions();
      if (selectedId !== null && screenComputerId === null) loadDetail(selectedId);
      document.querySelector("#updated-at").textContent = `Atualizado ${formatTime(new Date().toISOString())}`;
    } catch (error) {
      if (token() && load === computerLoad) globalError.textContent = error.message;
    }
  }

  function renderRecentDevices() {
    const recent = computers.filter((computer) => computer.last_seen)
      .sort((a, b) => new Date(b.last_seen) - new Date(a.last_seen)).slice(0, 5);
    const list = document.querySelector("#recent-devices");
    list.innerHTML = recent.map((computer) => `
      <li><button type="button" data-id="${computer.id}">
        <span>${escapeHtml(computer.display_name || computer.hostname)}</span>
        <time datetime="${escapeHtml(computer.last_seen)}">${escapeHtml(formatTime(computer.last_seen))}</time>
      </button></li>`).join("") || "<li>Nenhum dispositivo enviou inventário ainda.</li>";
    list.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        selectView("computers");
        loadDetail(Number(button.dataset.id), { focus: true });
      });
    });
  }

  function renderComputers() {
    const online = computers.filter((computer) => computer.online).length;
    document.querySelector("#total-count").textContent = computers.length;
    document.querySelector("#online-count").textContent = online;
    document.querySelector("#offline-count").textContent = computers.length - online;
    document.querySelector("#pending-count").textContent = computers.filter((computer) => computer.authorization_status === "pending").length;
    const query = searchInput.value.trim().toLocaleLowerCase(locale);
    const authorization = currentView === "pending" ? "pending" : authorizationFilter.value;
    const connection = connectionFilter.value;
    const visible = computers.filter((computer) =>
      (!authorization || computer.authorization_status === authorization)
      && (!connection || computer.online === (connection === "online"))
      && [computer.hostname, computer.display_name, computer.agent_id].some((value) => value?.toLocaleLowerCase(locale).includes(query))
    );
    document.querySelector("#result-count").textContent = `${visible.length} resultado${visible.length === 1 ? "" : "s"}`;
    if (!visible.length) {
      const empty = currentView === "pending" && !query && !connection ? "Nenhuma solicitação pendente."
        : query || authorization || connection ? "Nenhum computador corresponde aos filtros." : "Nenhum computador cadastrado.";
      rows.innerHTML = `<tr><td class="empty-state" colspan="7">${empty}</td></tr>`;
      return;
    }
    rows.innerHTML = visible.map((computer) => `
      <tr data-id="${computer.id}" class="${selectedId === computer.id ? "selected" : ""}">
        <td><span class="hostname">${escapeHtml(computer.display_name || computer.hostname)}</span></td>
        <td><code>${escapeHtml(computer.agent_id)}</code></td>
        <td>${escapeHtml(computer.os_name || "Não informado")}</td>
        <td><span class="status ${escapeHtml(computer.authorization_status)}">${escapeHtml(authorizationLabel(computer.authorization_status))}</span></td>
        <td><span class="status ${computer.online ? "online" : "offline"}">${computer.online ? "Online" : "Offline"}</span></td>
        <td>${escapeHtml(formatTime(computer.last_seen))}</td>
        <td><button type="button" class="table-action" aria-label="Detalhes de ${escapeHtml(computer.hostname)}">Detalhes</button></td>
      </tr>`).join("");
    rows.querySelectorAll("tr[data-id]").forEach((row) =>
      row.addEventListener("click", () => loadDetail(Number(row.dataset.id), { focus: true })));
  }

  function showPlaceholder(title, description) {
    detailPanel.innerHTML = `<div class="detail-placeholder"><span class="detail-symbol" aria-hidden="true">⌘</span><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p></div>`;
  }

  function authorizationLabel(status) {
    return ({ pending: "Pendente de aprovação", authorized: "Autorizado", revoked: "Revogado" })[status] || "Não informado";
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

  async function loadDetail(id, { focus = false } = {}) {
    if (selectedId === id && screenComputerId === id) {
      if (focus) detailPanel.scrollIntoView({ block: "start" });
      return;
    }
    if (screenComputerId !== null && screenComputerId !== id) closeScreenViewer();
    const load = ++detailLoad;
    selectedId = id;
    detailPanel.innerHTML = '<div class="detail-placeholder"><span class="detail-symbol" aria-hidden="true">…</span><h2>Carregando equipamento</h2><p>Buscando os dados mais recentes.</p></div>';
    rows.querySelectorAll("tr[data-id]").forEach((row) => row.classList.toggle("selected", Number(row.dataset.id) === id));
    try {
      const result = await api(`/api/v1/admin/computers/${id}`);
      if (load === detailLoad) {
        renderDetail(result.computer, result.inventory || {});
        if (focus && !detailPanel.closest(".hidden")) {
          detailPanel.querySelector("h2")?.focus({ preventScroll: true });
          detailPanel.scrollIntoView({ block: "start" });
        }
      }
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
      : `<div class="screen-placeholder">Aguardando a visualização</div>`;
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
      <header class="detail-header"><div class="detail-header-top"><div><h2 tabindex="-1">${escapeHtml(computer.display_name || computer.hostname)}</h2><div class="detail-meta">${escapeHtml(computer.agent_id)}</div></div>
      <div class="detail-statuses"><span class="status ${escapeHtml(computer.authorization_status)}">${escapeHtml(authorizationLabel(computer.authorization_status))}</span><span class="status ${computer.online ? "online" : "offline"}">${computer.online ? "Online" : "Offline"}</span><span class="status ${computer.channel_connected ? "channel-active" : "channel-inactive"}">${computer.channel_connected ? "Canal conectado" : "Canal desconectado"}</span></div></div>
      <div class="detail-actions">${authorizationAction}<button type="button" data-rename-computer>Editar nome</button></div></header>
      ${section("SISTEMA", detailRow("Usuário", computer.logged_user) + detailRow("Sistema", computer.os_name) + detailRow("Versão", computer.os_version) + detailRow("Arquitetura", system.architecture) + detailRow("Uptime", system.uptime_seconds ? `${Math.floor(system.uptime_seconds / 86400)} dias` : null) + detailRow("Último contato", formatTime(computer.last_seen)) + detailRow("Canal persistente", computer.channel_connected ? "Conectado" : "Desconectado") + detailRow("Conectado em", formatTime(computer.channel_connected_at)) + detailRow("Último heartbeat", formatTime(computer.channel_last_heartbeat)))}
      ${section("PROCESSADOR", detailRow("Modelo", cpu.model) + detailRow("Núcleos físicos", cpu.physical_cores) + detailRow("Núcleos lógicos", cpu.logical_cores))}
      ${section("MEMÓRIA", detailRow("Total", formatBytes(memory.total_bytes)) + (ramModules || `<div class="hardware-item"><span>Detalhes individuais indisponíveis.</span></div>`))}
      ${section("ARMAZENAMENTO", (disks || `<div class="hardware-item"><span>Nenhum disco identificado.</span></div>`) + (volumes || ""))}
      ${gpus ? section("VÍDEO", gpus) : ""}
      ${section("PLACA-MÃE E BIOS", detailRow("Fabricante", board.manufacturer) + detailRow("Modelo", board.model) + detailRow("Serial placa-mãe", board.serial) + detailRow("BIOS", bios.version) + detailRow("Serial BIOS", bios.serial))}
      ${section("VISUALIZAÇÃO DA TELA", `<div class="screen-meta"><span>Máquina: <strong>${escapeHtml(computer.hostname)}</strong></span><span>Canal: <strong>${computer.channel_connected ? "Conectado" : "Desconectado"}</strong></span><span>Transmissão: <strong data-screen-status>${escapeHtml(screenStateLabel())}</strong></span><span>Último frame: <strong data-screen-last-frame>${escapeHtml(formatTime(screenLastFrameAt))}</strong></span></div><div class="screen-viewer" data-screen-viewer><div class="screen-actions"><button class="screen-button" type="button" data-screen-action="start" ${!canStartScreen || screenBusy ? "disabled" : ""}>Solicitar acesso</button><button class="screen-button screen-stop-button" type="button" data-screen-action="stop" ${!screenBusy || screenState === "connecting" ? "disabled" : ""}>Parar</button><button class="screen-button" type="button" data-screen-action="control-start" ${!canStartControl ? "disabled" : ""}>Iniciar controle</button><button class="screen-button screen-stop-button" type="button" data-screen-action="control-stop" ${controlState === "idle" || controlState === "error" ? "disabled" : ""}>Encerrar controle</button><button class="screen-button screen-fullscreen-button" type="button" data-screen-action="fullscreen" ${screenState !== "streaming" ? "disabled" : ""}>Tela cheia</button><span class="control-status" data-control-status>${escapeHtml(controlStateLabel())}</span><span class="fullscreen-status" data-fullscreen-status>Janela</span></div><p class="control-error" data-control-error role="status">${escapeHtml(controlErrorMessage)}</p><div class="screen-frame-wrap" data-screen-frame tabindex="0" aria-label="Visualização remota de ${escapeHtml(computer.hostname)}">${screenFrame}</div></div><p class="screen-fullscreen-error" data-screen-fullscreen-error role="status"></p><p class="screen-hint">O usuário do computador precisa aceitar a solicitação antes de a tela ser compartilhada.</p>`)}
      ${section("REDE", detailRow("Gateways", (inventory.network?.gateways || []).join(", ")) + detailRow("DNS", (inventory.network?.dns_servers || []).join(", ")) + (network || `<div class="hardware-item"><span>Interfaces não disponíveis.</span></div>`))}
      ${section("CHAT COM O CLIENTE", `<div class="chat-box"><div data-chat-messages>Carregando mensagens…</div><div class="chat-compose"><input data-chat-input maxlength="2000" placeholder="Mensagem para o cliente"><button type="button" data-chat-send>Enviar</button></div></div>`)}`;
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
    async function loadChat() {
      try {
        const messages = await api(`/api/v1/admin/computers/${computer.id}/messages`);
        const box = detailPanel.querySelector('[data-chat-messages]');
        if (!box || selectedId !== computer.id) return;
        const ordered = messages.slice(-40);
        const newestId = ordered.length ? Number(ordered[ordered.length - 1].id) : 0;
        const hadNewClientMessage = newestId > chatLastMessageId && ordered.some((m) => Number(m.id) > chatLastMessageId && m.sender === 'client');
        chatLastMessageId = Math.max(chatLastMessageId, newestId);
        box.innerHTML = ordered.map(m => `
          <div class="chat-message ${m.sender === 'technician' ? 'chat-message-out' : 'chat-message-in'}">
            <div class="chat-message-head"><strong>${m.sender === 'technician' ? 'Você · TI' : 'Cliente'}</strong><span>${escapeHtml(formatTime(m.created_at))}</span></div>
            <div class="chat-message-body">${escapeHtml(m.message)}</div>
          </div>`).join('') || '<div class="chat-empty">Nenhuma mensagem ainda.</div>';
        if (hadNewClientMessage) {
          box.classList.remove('chat-updated');
          void box.offsetWidth;
          box.classList.add('chat-updated');
        }
        box.scrollTop = box.scrollHeight;
      } catch (error) {
        if (selectedId === computer.id) globalError.textContent = error.message;
      }
    }
    detailPanel.querySelector('[data-chat-send]')?.addEventListener('click', async (event) => {
      const input = detailPanel.querySelector('[data-chat-input]');
      const message = input?.value.trim();
      if (!message) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const result = await api(`/api/v1/admin/computers/${computer.id}/messages`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message}),
        });
        input.value = '';
        if (selectedId === computer.id) globalError.textContent = result.delivered
          ? '' : 'Mensagem salva no histórico. O canal do cliente estava indisponível.';
        await loadChat();
      } catch(e) { globalError.textContent = e.message; }
      finally { button.disabled = false; }
    });
    chatLastMessageId = 0;
    if (chatPollTimer !== null) clearInterval(chatPollTimer);
    loadChat();
    chatPollTimer = setInterval(() => {
      if (selectedId === computer.id && document.visibilityState !== 'hidden') loadChat();
    }, 1200);
    detailPanel.querySelector("[data-rename-computer]")?.addEventListener("click", async () => {
      const name = prompt("Nome exibido no Vyntra:", computer.display_name || computer.hostname);
      if (!name) return;
      try { await api(`/api/v1/admin/computers/${computer.id}/name?name=${encodeURIComponent(name)}`, {method:"PATCH"}); await loadComputers(); } catch (error) { globalError.textContent = error.message; }
    });
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
    updateConsoleSession();
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
    try {
      if (!document.fullscreenEnabled) throw new Error("Seu navegador não permite tela cheia nesta conexão.");
      if (document.fullscreenElement === viewer) await document.exitFullscreen();
      else await viewer.requestFullscreen();
    } catch (reason) {
      if (error) error.textContent = reason?.message || "Não foi possível abrir a tela cheia.";
    }
  }

  async function requestControlConsent(computer) {
    if (controlConsentPollTimer !== null) clearInterval(controlConsentPollTimer);
    controlConsentRequestId = null;
    controlErrorMessage = "Aguardando a confirmação do usuário para gerenciamento";
    updateControlControls();
    try {
      const response = await api(`/api/v1/admin/computers/${computer.id}/screen/access-request?mode=control`, { method: "POST" });
      controlConsentRequestId = response.request_id;
      controlConsentPollTimer = setInterval(async () => {
        try {
          const result = await api(`/api/v1/admin/computers/${computer.id}/screen/access-request/${encodeURIComponent(controlConsentRequestId)}`);
          if (result.status === "approved") {
            clearInterval(controlConsentPollTimer); controlConsentPollTimer = null; controlConsentRequestId = null;
            if (screenSocket?.readyState === WebSocket.OPEN) {
              controlState = "starting";
              controlErrorMessage = "";
              updateControlControls();
              screenSocket.send(JSON.stringify({ type: "control.start" }));
            }
          } else if (["rejected", "expired", "cancelled"].includes(result.status)) {
            clearInterval(controlConsentPollTimer); controlConsentPollTimer = null; controlConsentRequestId = null;
            controlState = "error";
            controlErrorMessage = result.status === "rejected" ? "O usuário recusou o gerenciamento." : "A solicitação de gerenciamento expirou.";
            updateControlControls();
          }
        } catch (error) {
          clearInterval(controlConsentPollTimer); controlConsentPollTimer = null; controlConsentRequestId = null;
          controlState = "error"; controlErrorMessage = error.message; updateControlControls();
        }
      }, 800);
    } catch (error) {
      controlState = "error"; controlErrorMessage = error.message; updateControlControls();
    }
  }

  async function startRemoteControl() {
    const computer = computers.find((item) => item.id === selectedId);
    if (
      !computer || computer.authorization_status !== "authorized" || !computer.channel_connected
      || screenState !== "streaming" || screenSocket?.readyState !== WebSocket.OPEN
      || !["idle", "error"].includes(controlState)
    ) return;
    if (!window.confirm(`Solicitar gerenciamento de ${computer.hostname}? O usuário precisará confirmar no computador.`)) return;
    await requestControlConsent(computer);
  }

  function stopRemoteControl() {
    if (controlConsentPollTimer !== null) { clearInterval(controlConsentPollTimer); controlConsentPollTimer = null; }
    if (controlConsentRequestId && screenComputerId !== null) {
      api(`/api/v1/admin/computers/${screenComputerId}/screen/access-request/${encodeURIComponent(controlConsentRequestId)}/cancel`, { method: "POST" }).catch(() => {});
      controlConsentRequestId = null;
    }
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
    if (frameWrap) frameWrap.innerHTML = '<div class="screen-placeholder">Aguardando a confirmação no computador</div>';
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
                updateScreenStatus(message.accepted ? "Transmitindo" : `Não foi possível iniciar: ${message.reason || "solicitação recusada"}`);
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
                  updateScreenStatus("O controle não foi iniciado");
                }
              } else if (message.type === "control.stop_ack") {
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
              screenSocket = null;
              screenComputerId = null;
              screenState = "stopped";
              controlState = "idle";
              clearHeldInputState();
              updateScreenStatus("Canal de visualização encerrado");
            });
            socket.addEventListener("error", () => {
              if (screenSocket !== socket) return;
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
    if (controlConsentPollTimer !== null) { clearInterval(controlConsentPollTimer); controlConsentPollTimer = null; }
    if (controlConsentRequestId && screenComputerId !== null) {
      api(`/api/v1/admin/computers/${screenComputerId}/screen/access-request/${encodeURIComponent(controlConsentRequestId)}/cancel`, { method: "POST" }).catch(() => {});
      controlConsentRequestId = null;
    }
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
    updateConsoleSession();
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "viewer mudou de máquina");
  }

  async function loadAdminTools() {
    const techBox=document.querySelector('#technician-list'), ticketBox=document.querySelector('#ticket-list');
    const settingsStatus = document.querySelector("#settings-status");
    const session = installationSession;
    const adminTools = document.querySelector("#admin-tools");
    if(!techBox||!ticketBox)return;
    try {
      const [techs,tickets]=await Promise.all([api('/api/v1/admin/technicians'),api('/api/v1/admin/tickets')]);
      if (session !== installationSession) return;
      adminTools.classList.remove("hidden");
      settingsStatus.classList.add("hidden");
      techBox.innerHTML=techs.map(t=>`<div class="tool-item"><strong>${escapeHtml(t.display_name)}</strong> · ${escapeHtml(t.username)} ${t.authorized?'':'(revogado)'}</div>`).join('')||'<p>Nenhum técnico cadastrado.</p>';
      ticketBox.innerHTML=tickets.slice(0,12).map(t=>`<div class="tool-item"><strong>#${t.id} ${escapeHtml(t.title)}</strong><br>${escapeHtml(t.status)} · ${escapeHtml(t.priority)}</div>`).join('')||'<p>Nenhum chamado.</p>';
    } catch (error) {
      if (session !== installationSession) return;
      adminTools.classList.add("hidden");
      settingsStatus.textContent = error.message;
      settingsStatus.classList.remove("hidden");
    }
  }
  function requestTechnicianAccount() {
    return new Promise((resolve) => {
      const dialog = document.createElement('dialog');
      dialog.setAttribute('aria-label', 'Cadastrar técnico');
      const form = document.createElement('form');
      form.innerHTML = '<h2>Cadastrar técnico</h2><p><label>Usuário <input name="username" required minlength="3" maxlength="100" autocomplete="off"></label></p><p><label>Nome <input name="display_name" required maxlength="160" autocomplete="off"></label></p><p><label>Senha <input name="password" type="password" required minlength="10" maxlength="1024" autocomplete="new-password"></label></p><button type="submit">Criar conta</button> <button type="button" data-cancel>Cancelar</button>';
      let result = null;
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        result = Object.fromEntries(new FormData(form));
        form.reset();
        dialog.close();
      });
      form.querySelector('[data-cancel]').addEventListener('click', () => dialog.close());
      dialog.addEventListener('close', () => {
        form.reset();
        dialog.remove();
        resolve(result);
      }, { once: true });
      dialog.append(form);
      document.body.append(dialog);
      dialog.showModal();
    });
  }
  document.querySelector('#new-tech-button')?.addEventListener('click', async()=>{
    const account = await requestTechnicianAccount();
    if (!account) return;
    try{await api('/api/v1/admin/technicians',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(account)});loadAdminTools();alert('Conta criada.');}catch(e){alert(e.message)}
  });
  document.querySelector('#new-ticket-button')?.addEventListener('click', async()=>{
    const title=prompt('Título do chamado:'); if(!title)return; const description=prompt('Descrição:'); if(!description)return; const priority=prompt('Prioridade: low, normal, high ou urgent','normal')||'normal';
    try{await api(`/api/v1/admin/tickets?title=${encodeURIComponent(title)}&description=${encodeURIComponent(description)}&priority=${encodeURIComponent(priority)}`,{method:'POST'});loadAdminTools();alert('Chamado criado.');}catch(e){alert(e.message)}
  });

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginError.textContent = "";
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    const adminToken = tokenInput.value.trim();
    try {
      if (username && password) {
        const response = await fetch('/api/v1/auth/login', { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(response.status === 401 ? "Usuário ou senha inválidos." : "Não foi possível entrar agora.");
        sessionStorage.setItem("dashboard_token", data.token);
        passwordInput.value = "";
      } else if (adminToken) {
        const response = await fetch("/api/v1/admin/computers", { headers: { Authorization: `Bearer ${adminToken}` } });
        if (!response.ok) throw new Error(response.status === 401 ? "Credencial administrativa inválida." : "Não foi possível acessar o painel.");
        sessionStorage.setItem("dashboard_token", adminToken);
      } else {
        throw new Error("Informe seu usuário e senha ou a credencial administrativa.");
      }
      showApp();
    } catch (error) {
      sessionStorage.removeItem("dashboard_token");
      loginError.textContent = error.message;
    }
  });

  logoutButton.addEventListener("click", async () => {
    closeScreenViewer();
    // Capture the current credential before clearing browser state. Network
    // failure must not keep the local dashboard open.
    const logoutHeaders = authHeaders();
    sessionStorage.removeItem("dashboard_token");
    computers = [];
    selectedId = null;
    searchInput.value = "";
    showLogin();
    await fetch('/api/v1/auth/logout', { method: 'POST', headers: logoutHeaders }).catch(() => {});
  });
  searchInput.addEventListener("input", renderComputers);
  authorizationFilter.addEventListener("change", renderComputers);
  connectionFilter.addEventListener("change", renderComputers);
  document.querySelector("#refresh-button").addEventListener("click", async () => {
    const button = document.querySelector("#refresh-button");
    if (!button || button.disabled) return;
    button.disabled = true;
    button.classList.add("is-refreshing");
    try {
      await Promise.all([loadComputers(), checkServer(), loadRemoteSessions(),
        currentView === "settings" && loadAdminTools(),
        currentView === "codes" && loadInstallationCodes()]);
    } finally {
      button.disabled = false;
      button.classList.remove("is-refreshing");
    }
  });
  document.addEventListener("keydown", (event) => {
    if (["computers", "pending"].includes(currentView) && token() && !event.defaultPrevented && event.key === "/"
      && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      searchInput.focus();
    }
  });
  document.addEventListener("fullscreenchange", () => {
    updateFullscreenButton();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) releaseHeldInputs();
  });
  window.addEventListener("blur", releaseHeldInputs);

  checkServer();
  setInterval(() => {
    if (document.visibilityState === "hidden") return;
    checkServer();
    if (token() && ["overview", "sessions"].includes(currentView)) loadRemoteSessions();
  }, 15000);
  if (token()) showApp(); else showLogin();
})();
