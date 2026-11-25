"use strict";

const $ = (sel) => document.querySelector(sel);
let pollTimer = null;
let logsTimer = null;
let logsName = null;
let editingName = null;
let settingsLoaded = false;
let refreshing = false;
let lastStatus = null;

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d) return `${d}d ${h}j ${m}m`;
  if (h) return `${h}j ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " err" : "");
  el.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.hidden = true; }, 4000);
}

async function api(path, opts) {
  opts = opts || {};
  const method = opts.method || "GET";
  const headers = Object.assign({}, opts.headers);
  if (method !== "GET" && method !== "HEAD") headers["X-WGPool"] = "1";
  let body = opts.body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, { method, headers, body, credentials: "same-origin" });
  const text = await res.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch (_e) { data = { error: text }; }
  }
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("sesi berakhir, silakan login lagi");
  }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(refresh, 2000);
}

function showLogin() {
  stopPolling();
  $("#appView").hidden = true;
  $("#loginView").hidden = false;
  settingsLoaded = false;
  if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
  ["configDialog", "logsDialog"].forEach((id) => { const d = $("#" + id); if (d.open) d.close(); });
}

function showApp() {
  $("#loginView").hidden = true;
  $("#appView").hidden = false;
}

async function login(event) {
  event.preventDefault();
  $("#loginError").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      json: { username: $("#loginUser").value, password: $("#loginPass").value },
    });
    $("#loginPass").value = "";
    showApp();
    await refresh();
    startPolling();
  } catch (err) {
    $("#loginError").textContent = err.message;
  }
}

async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch (_e) { /* ignore */ }
  showLogin();
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await api("/api/status");
    lastStatus = data;
    showApp();
    renderHeader(data);
    renderTunnels(data.tunnels || [], data.settings && data.settings.probe);
    if (!settingsLoaded) {
      loadSettingsForm(data.settings);
      settingsLoaded = true;
    }
    if (!pollTimer) startPolling();
  } catch (err) {
    if (!$("#appView").hidden && err.message !== "sesi berakhir, silakan login lagi") {
      console.warn(err);
    }
  } finally {
    refreshing = false;
  }
}

function renderHeader(data) {
  $("#version").textContent = "v" + data.version;
  const up = (data.tunnels || []).filter((t) => t.state === "running" && t.enabled).length;
  const total = (data.tunnels || []).length;
  $("#proxyInfo").textContent =
    `${data.proxy.strategy} · ${up}/${total} aktif · :${data.proxy.port} · up ${fmtDuration(data.uptime)}`;
}

const STATE_LABEL = {
  running: "running", starting: "starting", backoff: "backoff",
  stopped: "stopped", disabled: "disabled", invalid: "invalid",
};

function renderTunnels(tunnels, probeSettings) {
  const box = $("#tunnelList");
  if (!tunnels.length) {
    box.innerHTML = `<p class="muted">Belum ada config. Klik "+ Tambah config" untuk upload file WireGuard.</p>`;
    return;
  }
  box.innerHTML = tunnels.map((t) => {
    const state = t.state || "stopped";
    const cls = t.enabled === false ? " off" : "";
    const healthy = t.healthy === false ? " · <span style=\"color:var(--danger)\">tidak sehat</span>"
      : (t.last_probe_ms != null ? ` · probe ${t.last_probe_ms} ms` : "");
    const egress = probeSettings && probeSettings.enabled
      ? `<div class="egress-ip">Egress IP: <strong>${esc(t.egress_ip || "menunggu probe…")}</strong></div>`
      : "";
    const error = t.last_error ? `<div class="tunnel-err">${esc(t.last_error)}</div>` : "";
    const toggle = t.enabled === false
      ? `<button data-act="enable" data-name="${esc(t.name)}">Enable</button>`
      : `<button data-act="disable" data-name="${esc(t.name)}">Disable</button>`;
    return `
      <div class="tunnel${cls}">
        <div class="tunnel-main">
          <div class="tunnel-title">
            ${esc(t.name)}
            <span class="tag state-${esc(state)}">${esc(STATE_LABEL[state] || state)}</span>
            ${t.enabled === false ? '<span class="tag state-disabled">off</span>' : ""}
          </div>
          <div class="tunnel-meta">
            ${esc(t.endpoint || "endpoint ?")} · socks 127.0.0.1:${esc(t.socks_port)}
            · uptime ${fmtDuration(t.uptime)} · restart ${esc(t.restarts)} · gagal ${esc(t.fails)}${healthy}
          </div>
          ${egress}
          ${error}
        </div>
        <div class="tunnel-actions">
          <button data-act="restart" data-name="${esc(t.name)}">Restart</button>
          ${toggle}
          <button data-act="egress" data-name="${esc(t.name)}">Egress</button>
          <button data-act="logs" data-name="${esc(t.name)}">Log</button>
          <button data-act="edit" data-name="${esc(t.name)}">Edit</button>
          <button data-act="delete" data-name="${esc(t.name)}" class="danger">Hapus</button>
        </div>
      </div>`;
  }).join("");
}

async function tunnelAction(name, act) {
  try {
    if (act === "delete") {
      if (!confirm(`Hapus config ${name}?`)) return;
      await api("/api/configs/" + encodeURIComponent(name), { method: "DELETE" });
      toast("Config dihapus");
    } else if (act === "egress") {
      const res = await api("/api/test-egress?tunnel=" + encodeURIComponent(name), { method: "POST" });
      toast(`Egress ${res.ip} via ${res.tunnel}`);
      return;
    } else if (act === "logs") {
      openLogs(name);
      return;
    } else if (act === "edit") {
      openEdit(name);
      return;
    } else {
      await api(`/api/tunnels/${encodeURIComponent(name)}/${act}`, { method: "POST" });
      toast(`Aksi ${act} dikirim`);
    }
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

async function testEgressGlobal() {
  try {
    const res = await api("/api/test-egress", { method: "POST" });
    toast(`Egress ${res.ip} via ${res.tunnel}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function restartAll() {
  if (!confirm("Restart semua tunnel aktif secara bergiliran?")) return;
  const button = $("#btnRestartAll");
  button.disabled = true;
  button.textContent = "Restarting…";
  try {
    const result = await api("/api/tunnels/restart-all", { method: "POST" });
    const failed = (result.restarted || []).filter((item) => item.error || item.state !== "running");
    if (failed.length) {
      toast(`Selesai, ${failed.length} tunnel perlu dicek`, true);
    } else {
      toast(`${(result.restarted || []).length} tunnel berhasil direstart`);
    }
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Restart All";
  }
}

function openAdd() {
  editingName = null;
  $("#configTitle").textContent = "Tambah config";
  $("#configName").disabled = false;
  $("#configName").value = "";
  $("#configText").value = "";
  $("#configFile").value = "";
  $("#configError").textContent = "";
  $("#configDialog").showModal();
}

async function openEdit(name) {
  try {
    const data = await api("/api/configs/" + encodeURIComponent(name));
    editingName = name;
    $("#configTitle").textContent = "Edit " + name;
    $("#configName").disabled = true;
    $("#configName").value = name;
    $("#configText").value = data.content || "";
    $("#configFile").value = "";
    $("#configError").textContent = "";
    $("#configDialog").showModal();
  } catch (err) {
    toast(err.message, true);
  }
}

async function saveConfig() {
  const name = $("#configName").value.trim();
  const text = $("#configText").value;
  $("#configError").textContent = "";
  if (!name) { $("#configError").textContent = "Nama file wajib diisi"; return; }
  if (!text.trim()) { $("#configError").textContent = "Isi config kosong"; return; }
  const btn = $("#configSave");
  btn.disabled = true;
  try {
    if (editingName) {
      await api("/api/configs/" + encodeURIComponent(editingName), { method: "PUT", body: text });
      toast("Config disimpan dan tunnel direstart");
    } else {
      await api("/api/configs?name=" + encodeURIComponent(name), { method: "POST", body: text });
      toast("Config ditambahkan");
    }
    $("#configDialog").close();
    await refresh();
  } catch (err) {
    $("#configError").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

async function loadLogs(name) {
  try {
    const data = await api(`/api/tunnels/${encodeURIComponent(name)}/logs?lines=300`);
    const box = $("#logsBody");
    box.textContent = (data.lines && data.lines.length) ? data.lines.join("\n") : "(log kosong)";
    box.scrollTop = box.scrollHeight;
  } catch (err) {
    $("#logsBody").textContent = err.message;
  }
}

function openLogs(name) {
  logsName = name;
  $("#logsTitle").textContent = "Log " + name;
  $("#logsDialog").showModal();
  loadLogs(name);
  if (logsTimer) clearInterval(logsTimer);
  logsTimer = setInterval(() => loadLogs(name), 2500);
}

function loadSettingsForm(settings) {
  if (!settings) return;
  const set = (name, value) => {
    const el = document.querySelector(`[name="${name}"]`);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value == null ? "" : value;
  };
  set("proxy.bind", settings.proxy.bind);
  set("proxy.port", settings.proxy.port);
  set("proxy.strategy", settings.proxy.strategy);
  set("proxy.max_fails", settings.proxy.max_fails);
  set("proxy.fail_timeout", settings.proxy.fail_timeout);
  set("proxy.connect_timeout", settings.proxy.connect_timeout);
  set("proxy.auth_enabled", settings.proxy.auth_enabled);
  set("proxy.auth_username", settings.proxy.auth_username);
  set("proxy.new_password", "");
  getField("proxy.new_password").placeholder = settings.proxy.auth_password_set
    ? "tersimpan; kosongkan untuk mempertahankan"
    : "min. 8 karakter";
  set("probe.enabled", settings.probe.enabled);
  set("probe.interval", settings.probe.interval);
  set("probe.target", settings.probe.target);
  set("probe.timeout", settings.probe.timeout);
  set("rotation.interval_minutes", settings.rotation ? settings.rotation.interval_minutes : 0);
  set("exit.ipv6_only", settings.exit ? settings.exit.ipv6_only : false);
  set("wireproxy.binary", settings.wireproxy.binary);
  set("wireproxy.base_socks_port", settings.wireproxy.base_socks_port);
  set("web.bind", settings.web.bind);
  set("web.port", settings.web.port);
  set("web.username", settings.web.username);
  set("web.tls_cert", settings.web.tls_cert || "");
  set("web.tls_key", settings.web.tls_key || "");
  set("new_password", "");
}

function getField(name) { return document.querySelector(`[name="${name}"]`); }

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    proxy: {
      bind: getField("proxy.bind").value.trim(),
      port: Number(getField("proxy.port").value),
      strategy: getField("proxy.strategy").value,
      max_fails: Number(getField("proxy.max_fails").value),
      fail_timeout: Number(getField("proxy.fail_timeout").value),
      connect_timeout: Number(getField("proxy.connect_timeout").value),
      auth_enabled: getField("proxy.auth_enabled").checked,
      auth_username: getField("proxy.auth_username").value.trim(),
    },
    probe: {
      enabled: getField("probe.enabled").checked,
      interval: Number(getField("probe.interval").value),
      target: getField("probe.target").value.trim(),
      timeout: Number(getField("probe.timeout").value),
    },
    rotation: {
      interval_minutes: Number(getField("rotation.interval_minutes").value || 0),
    },
    exit: {
      ipv6_only: getField("exit.ipv6_only").checked,
    },
    wireproxy: {
      binary: getField("wireproxy.binary").value.trim(),
      base_socks_port: Number(getField("wireproxy.base_socks_port").value),
    },
    web: {
      bind: getField("web.bind").value.trim(),
      port: Number(getField("web.port").value),
      username: getField("web.username").value.trim(),
      tls_cert: getField("web.tls_cert").value.trim(),
      tls_key: getField("web.tls_key").value.trim(),
    },
  };
  const password = getField("new_password").value;
  if (password) payload.new_password = password;
  const proxyPassword = getField("proxy.new_password").value;
  if (proxyPassword) payload.proxy.new_password = proxyPassword;

  const before = lastStatus ? lastStatus.web : null;
  const webChanged = before && (
    before.bind !== payload.web.bind ||
    before.port !== payload.web.port ||
    !!before.tls !== !!(payload.web.tls_cert && payload.web.tls_key)
  );
  try {
    await api("/api/settings", { method: "PUT", json: payload });
    toast(webChanged
      ? "Pengaturan disimpan. Web UI pindah ke " + payload.web.bind + ":" + payload.web.port
      : "Pengaturan disimpan");
    getField("new_password").value = "";
    getField("proxy.new_password").value = "";
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", login);
  $("#btnLogout").addEventListener("click", logout);
  $("#btnAdd").addEventListener("click", openAdd);
  $("#btnTest").addEventListener("click", testEgressGlobal);
  $("#btnRestartAll").addEventListener("click", restartAll);
  $("#settingsForm").addEventListener("submit", saveSettings);
  getField("exit.ipv6_only").addEventListener("change", async (event) => {
    const toggle = event.currentTarget;
    toggle.disabled = true;
    try {
      const settings = await api("/api/settings", {
        method: "PUT",
        json: { exit: { ipv6_only: toggle.checked } },
      });
      loadSettingsForm(settings);
      toast(`IPv6-only global ${toggle.checked ? "aktif" : "nonaktif"}; tunnel direstart bergantian`);
      await refresh();
    } catch (err) {
      toast(err.message, true);
      if (lastStatus && lastStatus.settings && lastStatus.settings.exit) {
        toggle.checked = lastStatus.settings.exit.ipv6_only;
      }
    } finally {
      toggle.disabled = false;
    }
  });

  $("#tunnelList").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    tunnelAction(btn.dataset.name, btn.dataset.act);
  });

  $("#configCancel").addEventListener("click", () => $("#configDialog").close());
  $("#configSave").addEventListener("click", saveConfig);
  $("#configFile").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $("#configText").value = String(reader.result || "");
      if (!$("#configName").value.trim()) $("#configName").value = file.name;
    };
    reader.readAsText(file);
  });
  $("#logsClose").addEventListener("click", () => $("#logsDialog").close());
  $("#logsRefresh").addEventListener("click", () => {
    if (logsName) loadLogs(logsName);
  });
  $("#logsDialog").addEventListener("close", () => {
    if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
    logsName = null;
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
    }
  });
}

bindEvents();
refresh().then(() => {
  if ($("#appView").hidden) showLogin();
  else startPolling();
});
