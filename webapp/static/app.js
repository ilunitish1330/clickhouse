/* PUC Analytics front end: hash routes, no build step. */
(function () {
  const { render, fmt, esc, CURRENCY } = window.PUCCharts;
  const app = document.getElementById("app");
  const state = { me: null, filters: { period: "", region: 0, utility: 0 }, tableView: {}, lastPage: null };
  const icon = (id, cls = "i") => `<svg class="${cls}"><use href="#i-${id}"/></svg>`;

  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}, ...opts });
    if (r.status === 401 && !path.endsWith("/login")) { state.me = null; showLogin(); throw new Error("signed out"); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }

  // ------------------------------------------------------------------ login
  function showLogin(msg) {
    app.innerHTML = `
    <div class="login">
      <section class="login-brand">
        <div class="brandmark"><div class="logo">${icon("drop")}</div><div>PUC Analytics<small>Public Utilities Corporation</small></div></div>
        <div>
          <h1>Billing and consumption insight for every island.</h1>
          <p>Electricity, water and sewerage billing loaded into ClickHouse and turned into dashboards, shaped to your role.</p>
          <div class="login-utils"><span>${icon("bolt")} Electricity</span><span>${icon("drop")} Water</span><span>${icon("pipe")} Sewerage</span></div>
        </div>
        <small style="opacity:.6">Mahe · Praslin · La Digue</small>
      </section>
      <section class="login-form">
        <form id="login-form" autocomplete="on">
          <h2>Sign in</h2>
          <p class="sub">Use the account your administrator gave you.</p>
          ${msg ? `<div class="error">${icon("x")} ${esc(msg)}</div>` : ""}
          <label class="field"><span>Username</span><input name="username" autocomplete="username" required autofocus></label>
          <label class="field"><span>Password</span><input name="password" type="password" autocomplete="current-password" required></label>
          <button class="btn primary block" type="submit">Sign in</button>
        </form>
      </section>
    </div>`;
    document.getElementById("login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target), btn = e.target.querySelector("button");
      btn.disabled = true; btn.textContent = "Signing in…";
      try {
        await api("/api/login", { method: "POST", body: JSON.stringify({ username: f.get("username"), password: f.get("password") }) });
        await boot();
      } catch (err) { showLogin(err.message); }
    });
  }

  // ------------------------------------------------------------------ shell
  const initials = (n) => n.split(/\s+/).filter((w) => /^[A-Za-z]/.test(w)).slice(0, 2).map((w) => w[0].toUpperCase()).join("");

  function shell(active) {
    const me = state.me;
    const nav = me.pages.map((p) => `<a href="#/${p.id}" class="${active === p.id ? "active" : ""}">${icon(p.icon)}${esc(p.title)}</a>`).join("");
    const data = me.can_load ? `<div class="nav-group">Data</div><a href="#/load" class="${active === "load" ? "active" : ""}">${icon("upload")}Data Load</a>` : "";
    const admin = me.can_admin ? `<div class="nav-group">Admin</div><a href="#/users" class="${active === "users" ? "active" : ""}">${icon("users")}Users &amp; roles</a>` : "";
    app.innerHTML = `
    <div class="shell">
      <aside class="side" id="side">
        <div class="brandmark"><div class="logo">${icon("drop")}</div><div>PUC Analytics<small>Public Utilities Corporation</small></div></div>
        <nav class="nav nav-scroll">${me.pages.length ? `<div class="nav-group">Dashboards</div>${nav}` : ""}${data}${admin}</nav>
        <div class="me">
          <div class="me-card"><div class="avatar">${esc(initials(me.full_name))}</div>
            <div><b>${esc(me.full_name)}</b><small>${esc(me.role_title)}${me.island !== "All islands" ? " · " + esc(me.island) : ""}</small></div></div>
          <div class="me-actions">
            <button id="theme-btn" title="Light or dark theme" aria-label="Toggle theme">${icon("moon")}</button>
            <button id="pw-btn" title="Change password" aria-label="Change password">${icon("key")}</button>
            <button id="out-btn" title="Sign out" aria-label="Sign out">${icon("out")}</button>
          </div>
        </div>
      </aside>
      <main class="main" id="main"></main>
    </div>`;
    document.getElementById("theme-btn").onclick = toggleTheme;
    document.getElementById("pw-btn").onclick = () => passwordModal(false);
    document.getElementById("out-btn").onclick = async () => { await api("/api/logout", { method: "POST" }); state.me = null; showLogin(); };
    return document.getElementById("main");
  }

  function toggleTheme() {
    const dark = document.documentElement.dataset.theme === "dark" ||
      (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("puc-theme", document.documentElement.dataset.theme); } catch (e) {}
    route();
  }

  const mobileTop = (title) => `<div class="mobile-top"><button class="btn small" onclick="document.getElementById('side').classList.toggle('open')" aria-label="Menu">${icon("menu")}</button><b>${esc(title)}</b></div>`;

  // ------------------------------------------------------------------ dashboards
  function filterBar(page) {
    const f = state.me.filters, s = state.filters;
    const periods = [`<option value="">All periods</option>`].concat(f.periods.slice().reverse().map((p) => `<option value="${p.period}" ${s.period === p.period ? "selected" : ""}>${esc(p.period_label)}</option>`)).join("");
    const regions = f.region_locked ? "" : `<div class="sep"></div><label>Island<select id="f-region"><option value="0">All islands</option>${f.regions.map((r) => `<option value="${r.code}" ${+s.region === r.code ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>`;
    const utils = page.utility || f.utilities.length < 2 ? "" : `<div class="sep"></div><label>Utility<select id="f-utility"><option value="0">All utilities</option>${f.utilities.map((u) => `<option value="${u.code}" ${+s.utility === u.code ? "selected" : ""}>${esc(u.name)}</option>`).join("")}</select></label>`;
    return `<div class="filters"><label>Period<select id="f-period">${periods}</select></label>${regions}${utils}</div>`;
  }

  function scopeChips(page) {
    const me = state.me, chips = [];
    if (me.filters.region_locked) chips.push(`${icon("lock")} ${esc(me.island)} only`);
    if (me.filters.utilities.length < 3 && !page.utility) chips.push(`${icon("lock")} ${me.filters.utilities.map((u) => esc(u.name)).join(" and ")} only`);
    return chips.length ? `<div class="chips">${chips.map((c) => `<span class="chip">${c}</span>`).join("")}</div>` : "";
  }

  function tileHtml(t) {
    const f = fmt(t.value, t.fmt, t.unit);
    const empty = t.value === null;
    const valueHtml = empty ? "—" : t.fmt === "money" ? `<small style="margin:0 4px 0 0">${CURRENCY}</small>${esc(f.text)}` : `${esc(f.text)}${f.unit ? `<small>${esc(f.unit)}</small>` : ""}`;
    let foot = "";
    if (empty && (t.fmt === "qty" || t.fmt === "rate")) foot = `<div class="hint">Pick one utility to see this</div>`;
    else if (t.previous !== null && t.previous !== undefined && t.value !== null && t.previous !== 0) {
      const ch = (t.value - t.previous) / Math.abs(t.previous);
      foot = `<div class="delta">${icon(ch >= 0 ? "up" : "down")}<b>${Math.abs(ch * 100).toFixed(1)}%</b> vs ${esc(t.previous_label)}</div>`;
    }
    return `<div class="tile ${empty ? "empty" : ""}"><div class="label">${esc(t.label)}</div><div class="value">${valueHtml}</div>${foot}</div>`;
  }

  async function dashboard(pageId) {
    const page = state.me.pages.find((p) => p.id === pageId);
    if (!page) return notAllowed();
    const main = shell(pageId);
    const periodLabel = () => (state.me.filters.periods.find((p) => p.period === state.filters.period) || {}).period_label || "All periods";
    main.innerHTML = mobileTop(page.title) + `
      <div class="topbar"><div><h1>${esc(page.title)}</h1><p class="subtitle" id="subtitle">&nbsp;</p>${scopeChips(page)}</div>${filterBar(page)}</div>
      <div id="dash"><div class="tiles">${"<div class='skeleton'></div>".repeat(4)}</div></div>`;
    const onChange = () => {
      state.filters.period = main.querySelector("#f-period").value;
      state.filters.region = +(main.querySelector("#f-region")?.value || 0);
      state.filters.utility = +(main.querySelector("#f-utility")?.value || 0);
      load();
    };
    main.querySelectorAll(".filters select").forEach((s) => s.addEventListener("change", onChange));

    async function load() {
      const dash = main.querySelector("#dash");
      dash.classList.add("loading");
      const qs = new URLSearchParams({ period: state.filters.period, region: state.filters.region, utility: page.utility ? 0 : state.filters.utility });
      let d;
      try { d = await api(`/api/page/${pageId}?${qs}`); }
      catch (err) { dash.innerHTML = `<div class="note">${icon("x")} ${esc(err.message)}</div>`; dash.classList.remove("loading"); return; }
      if (d.empty) return noData(dash);
      main.querySelector("#subtitle").textContent = `${d.subtitle} · ${periodLabel()}`;
      dash.innerHTML = `<div class="tiles">${d.tiles.map(tileHtml).join("")}</div>
        <div class="grid">${d.charts.map((c, i) => `
          <section class="card ${c.wide || c.kind === "table" && c.metrics.length > 3 ? "wide" : ""}">
            <div class="card-head"><div><h3>${esc(c.title)}</h3><div class="meta">${c.all_periods ? "All periods" : esc(periodLabel())}${c.unit && c.metrics.some((m) => m.fmt === "qty") ? " · " + esc(c.unit) : ""}${c.metrics.some((m) => m.fmt === "money") ? " · " + CURRENCY : ""}</div></div>
            ${c.kind !== "table" && !c.note ? `<button class="btn small ghost" data-toggle="${i}" aria-label="Switch between chart and table">${icon(state.tableView[pageId + i] ? "chart" : "table")}${state.tableView[pageId + i] ? "Chart" : "Table"}</button>` : ""}</div>
            <div class="card-body" id="c${i}"></div></section>`).join("")}</div>`;
      d.charts.forEach((c, i) => {
        const el = dash.querySelector(`#c${i}`);
        if (c.note) el.innerHTML = `<div class="note">${icon("drop")} ${esc(c.note)}</div>`;
        else render(el, c, state.tableView[pageId + i]);
      });
      dash.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => {
        const i = +b.dataset.toggle; state.tableView[pageId + i] = !state.tableView[pageId + i]; load();
      }));
      state.lastPage = { pageId, d };
      dash.classList.remove("loading");
    }
    await load();
  }

  function noData(el) {
    el.innerHTML = `<div class="empty-state"><div class="big">${icon("upload")}</div><h2>No billing data loaded yet</h2>
      <p>${state.me.can_run ? `Load a Statistic Report export on the <a href="#/load">Data Load</a> page.` : "Ask a data operator to load a Statistic Report export."}</p></div>`;
  }

  function notAllowed() {
    const main = shell("");
    const first = state.me.pages[0];
    main.innerHTML = mobileTop("PUC Analytics") + `<div class="empty-state"><div class="big">${icon("lock")}</div><h2>Not part of your role</h2>
      <p>${first ? `Go to <a href="#/${first.id}">${esc(first.title)}</a>.` : state.me.can_load ? `Go to <a href="#/load">Data Load</a>.` : ""}</p></div>`;
  }

  // ------------------------------------------------------------------ data load
  let poll = null;
  async function loadPage() {
    if (!state.me.can_load) return notAllowed();
    const main = shell("load");
    const run = state.me.can_run;
    main.innerHTML = mobileTop("Data Load") + `
      <div class="topbar"><div><h1>Data Load</h1><p class="subtitle">${run ? "Upload a Statistic Report export and press Run: ClickHouse is loaded and every table, aggregate and dashboard is rebuilt." : "What has been loaded, and when."}</p></div></div>
      <div class="panels">
        ${run ? `<section class="card">
          <div class="card-head"><div><h3>Load a billing export</h3><div class="meta">.xlsx, .csv, .tsv, .txt, or a .zip of them</div></div><span id="job-status"></span></div>
          <label class="drop" id="drop"><input type="file" id="file" accept=".xlsx,.xlsm,.csv,.tsv,.txt,.zip" hidden>
            <div class="big">${icon("upload")}</div><b>Drop the file here or click to choose</b><small>A file with the same name replaces its earlier load; nothing is counted twice.</small></label>
          <div id="picked"></div>
          <div class="steps" id="steps">
            <div class="step" data-s="0"><b>Upload</b>File to the server</div>
            <div class="step" data-s="1"><b>Stage</b>Raw rows into ClickHouse</div>
            <div class="step" data-s="2"><b>Model</b>Fact and dimensions</div>
            <div class="step" data-s="3"><b>Aggregate</b>Tables for dashboards</div>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px">
            <button class="btn primary" id="run" disabled>${icon("pulse")}Run</button>
            <button class="btn" id="rebuild" title="Rebuild aggregates from what is already loaded">Rebuild aggregates only</button>
          </div>
          <div class="eta" id="eta" hidden><div class="eta-row"><span id="eta-text"></span><span id="eta-left"></span></div><div class="eta-track"><div class="eta-bar" id="eta-bar"></div></div></div>
          <div class="console" id="console" hidden></div>
        </section>` : ""}
        <section class="card ${run ? "" : "wide"}">
          <div class="card-head"><div><h3>Loaded billing periods</h3><div class="meta">What the dashboards are built from</div></div></div>
          <div id="batches"><div class="skeleton"></div></div>
          <div class="card-head" style="margin-top:18px"><div><h3>Load history</h3><div class="meta">Latest 30 loads</div></div></div>
          <div id="history"></div>
        </section>
      </div>`;
    let file = null;
    const pick = (f) => {
      file = f;
      if (!f && main.querySelector("#file")) main.querySelector("#file").value = "";  // so the same file can be chosen again
      main.querySelector("#picked").innerHTML = f ? `<div class="file-pill"><span>${icon("table")} <b>${esc(f.name)}</b> <span class="pill">${(f.size / 1e6).toFixed(1)} MB</span></span><button class="btn small ghost" id="unpick" aria-label="Remove file">${icon("x")}</button></div>` : "";
      main.querySelector("#run").disabled = !f;
      main.querySelector("#unpick")?.addEventListener("click", (e) => { e.preventDefault(); pick(null); });
    };
    if (run) {
      const drop = main.querySelector("#drop"), input = main.querySelector("#file");
      input.addEventListener("change", () => pick(input.files[0]));
      ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
      drop.addEventListener("drop", (e) => pick(e.dataTransfer.files[0]));
      main.querySelector("#run").addEventListener("click", async () => {
        const fd = new FormData(); fd.append("file", file);
        setSteps(0); showConsole(`Uploading ${file.name}…`);
        try { const { job } = await api("/api/load", { method: "POST", body: fd }); watch(job); }
        catch (err) { showConsole(err.message); status("failed"); }
      });
      main.querySelector("#rebuild").addEventListener("click", async () => {
        setSteps(3); showConsole("Rebuilding aggregates…");
        try { const { job } = await api("/api/rebuild", { method: "POST" }); watch(job); }
        catch (err) { showConsole(err.message); status("failed"); }
      });
    }
    function showConsole(t) { const c = main.querySelector("#console"); c.hidden = false; c.textContent = t; }
    function status(s) { const el = main.querySelector("#job-status"); if (el) el.innerHTML = `<span class="status ${s}">${{ running: "Running", done: "Finished", failed: "Failed" }[s]}</span>`; }
    function setSteps(n, done) {
      main.querySelectorAll(".step").forEach((el) => {
        const i = +el.dataset.s; el.classList.toggle("on", i === n && !done); el.classList.toggle("done", i < n || !!done);
      });
    }
    const clock = (s) => { s = Math.max(0, Math.round(s)); return s >= 60 ? `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s` : `${s} s`; };
    function showEta(j) {
      const box = main.querySelector("#eta"); if (!box) return;
      box.hidden = false;
      const el = j.elapsed || 0, est = j.estimate || 0;
      if (j.status === "running") {
        const left = est - el;
        // time-based bar, held short of full until the job really finishes
        main.querySelector("#eta-bar").style.width = Math.min(95, (el / Math.max(est, 1)) * 100) + "%";
        main.querySelector("#eta-text").textContent = `Elapsed ${clock(el)}`;
        main.querySelector("#eta-left").textContent = left > 2 ? `about ${clock(left)} left` : "finishing…";
      } else {
        main.querySelector("#eta-bar").style.width = "100%";
        main.querySelector("#eta-bar").classList.toggle("failed", j.status === "failed");
        main.querySelector("#eta-text").textContent = `${j.status === "done" ? "Finished" : "Stopped"} in ${clock(el)}`;
        main.querySelector("#eta-left").textContent = "";
      }
    }
    function watch(job) {
      status("running"); main.querySelector("#run").disabled = true; main.querySelector("#rebuild").disabled = true;
      clearInterval(poll);
      poll = setInterval(async () => {
        let j; try { j = await api(`/api/load/${job}`); } catch (e) { return; }
        const text = j.log.join("\n"), c = main.querySelector("#console");
        if (!c) return clearInterval(poll);
        c.textContent = text || "Starting…"; c.scrollTop = c.scrollHeight;
        showEta(j);
        setSteps(/aggregates rebuilt/.test(text) ? 4 : /rebuilding dimensions|rows read/.test(text) ? 3 : /rows staged/.test(text) ? 2 : 1, j.status === "done");
        if (j.status !== "running") {
          clearInterval(poll); status(j.status);
          main.querySelector("#rebuild").disabled = false; pick(null);
          if (j.status === "done") {
            state.me = await api("/api/me"); refreshTables();
            const ps = state.me.filters.periods; if (!state.filters.period && ps.length) state.filters.period = ps[ps.length - 1].period; c.textContent += "\n\nDone. The dashboards now show this data."; }
        }
      }, 1000);
    }
    async function refreshTables() {
      const d = await api("/api/loads");
      main.querySelector("#batches").innerHTML = d.batches.length ? `<div class="tbl-wrap"><table class="data"><thead><tr><th>Period</th><th class="n">Billing lines</th><th class="n">Invoices</th><th class="n">Amount (${CURRENCY})</th></tr></thead><tbody>${d.batches.map((b) => `<tr><td>${esc(fmtPeriod(b.period))}</td><td class="n">${(+b.lines).toLocaleString()}</td><td class="n">${(+b.invoices).toLocaleString()}</td><td class="n">${Math.round(b.amount).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<div class="note">Nothing loaded yet.</div>`;
      main.querySelector("#history").innerHTML = d.history.length ? `<div class="tbl-wrap scroll-y"><table class="data"><thead><tr><th>When</th><th>File</th><th class="n">Rows</th><th class="n">Amount</th></tr></thead><tbody>${d.history.map((h) => `<tr><td class="num">${esc(h.loaded_at)}</td><td>${esc(h.file_name)}</td><td class="n">${(+h.rows).toLocaleString()}</td><td class="n">${Math.round(h.amount).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<div class="note">No loads recorded.</div>`;
      if (d.running && run && !poll) watch(d.running.id);
    }
    await refreshTables();
  }
  const fmtPeriod = (p) => new Date(p + "T00:00:00").toLocaleDateString(undefined, { month: "short", year: "numeric" });

  // ------------------------------------------------------------------ users
  async function usersPage() {
    if (!state.me.can_admin) return notAllowed();
    const main = shell("users");
    main.innerHTML = mobileTop("Users & roles") + `<div class="topbar"><div><h1>Users &amp; roles</h1><p class="subtitle">Each user has a role (which pages and utilities) and optionally one island (which rows).</p></div><button class="btn primary" id="add">${icon("users")}Add user</button></div><div id="u"></div>`;
    const d = await api("/api/users");
    const roleTitle = Object.fromEntries(d.roles.map((r) => [r.id, r.title]));
    const island = Object.fromEntries(d.islands.map((i) => [i.code, i.name]));
    main.querySelector("#u").innerHTML = `
      <section class="card wide"><div class="tbl-wrap"><table class="data"><thead><tr><th>User</th><th>Name</th><th>Role</th><th>Island</th><th>Status</th><th></th></tr></thead><tbody>
      ${d.users.map((u) => `<tr><td><b>${esc(u.username)}</b></td><td>${esc(u.full_name)}</td><td>${esc(roleTitle[u.role] || u.role)}</td><td>${esc(island[u.region_code])}</td>
        <td>${+u.active ? (+u.must_change ? `<span class="pill">Must change password</span>` : `<span class="pill">Active</span>`) : `<span class="pill off">Disabled</span>`}</td>
        <td class="n"><button class="btn small" data-edit="${esc(u.username)}">Edit</button></td></tr>`).join("")}
      </tbody></table></div></section>
      <section class="card wide" style="margin-top:14px"><div class="card-head"><div><h3>Roles</h3><div class="meta">Defined in webapp/roles.py</div></div></div>
      <div class="tbl-wrap"><table class="data"><thead><tr><th>Role</th><th>What it sees</th></tr></thead><tbody>${d.roles.map((r) => `<tr><td><b>${esc(r.title)}</b></td><td>${esc(r.description)}</td></tr>`).join("")}</tbody></table></div></section>`;
    const edit = (u) => userModal(u, d, () => usersPage());
    main.querySelector("#add").onclick = () => edit(null);
    main.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => edit(d.users.find((u) => u.username === b.dataset.edit)));
  }

  function modal(html, onMount) {
    const root = document.getElementById("modal-root");
    root.innerHTML = `<div class="modal-back"><div class="modal" role="dialog" aria-modal="true">${html}</div></div>`;
    const close = () => { root.innerHTML = ""; };
    onMount(root.querySelector(".modal"), close);
    root.querySelector("input,select")?.focus();
  }

  function userModal(u, d, done) {
    modal(`<h2>${u ? "Edit " + esc(u.username) : "Add a user"}</h2><p class="sub">${u ? "Leave the password empty to keep it." : "They must change the password at first sign-in."}</p>
      <form id="uf"><div id="uerr"></div>
      <label class="field"><span>Username</span><input name="username" value="${esc(u?.username || "")}" ${u ? "readonly" : ""} required></label>
      <label class="field"><span>Full name</span><input name="full_name" value="${esc(u?.full_name || "")}" required></label>
      <label class="field"><span>Role</span><select name="role">${d.roles.map((r) => `<option value="${r.id}" ${u?.role === r.id ? "selected" : ""}>${esc(r.title)}</option>`).join("")}</select></label>
      <label class="field"><span>Island</span><select name="region_code">${d.islands.map((i) => `<option value="${i.code}" ${+u?.region_code === i.code ? "selected" : ""}>${esc(i.name)}</option>`).join("")}</select></label>
      <label class="field"><span>${u ? "New password" : "Password"}</span><input name="password" type="password" minlength="8" ${u ? "" : "required"} autocomplete="new-password"></label>
      <label style="display:flex;gap:8px;align-items:center;margin:-4px 0 12px"><input type="checkbox" name="active" ${!u || +u.active ? "checked" : ""}> Account active</label>
      <div class="actions"><button type="button" class="btn" id="cancel">Cancel</button><button class="btn primary" type="submit">Save</button></div></form>`,
    (m, close) => {
      m.querySelector("#cancel").onclick = close;
      m.querySelector("#uf").onsubmit = async (e) => {
        e.preventDefault(); const f = new FormData(e.target);
        try {
          await api("/api/users", { method: "POST", body: JSON.stringify({ username: f.get("username"), full_name: f.get("full_name"), role: f.get("role"), region_code: +f.get("region_code"), password: f.get("password") || null, active: !!f.get("active") }) });
          close(); done();
        } catch (err) { m.querySelector("#uerr").innerHTML = `<div class="error">${esc(err.message)}</div>`; }
      };
    });
  }

  function passwordModal(forced) {
    modal(`<h2>${forced ? "Choose a new password" : "Change password"}</h2><p class="sub">${forced ? "Your account still has its first password. Pick your own to continue." : "At least 8 characters."}</p>
      <form id="pf"><div id="perr"></div>
      <label class="field"><span>Current password</span><input name="current" type="password" required autocomplete="current-password"></label>
      <label class="field"><span>New password</span><input name="new" type="password" minlength="8" required autocomplete="new-password"></label>
      <label class="field"><span>Repeat new password</span><input name="again" type="password" minlength="8" required autocomplete="new-password"></label>
      <div class="actions">${forced ? "" : `<button type="button" class="btn" id="cancel">Cancel</button>`}<button class="btn primary" type="submit">Save password</button></div></form>`,
    (m, close) => {
      m.querySelector("#cancel")?.addEventListener("click", close);
      m.querySelector("#pf").onsubmit = async (e) => {
        e.preventDefault(); const f = new FormData(e.target);
        if (f.get("new") !== f.get("again")) { m.querySelector("#perr").innerHTML = `<div class="error">The new passwords differ.</div>`; return; }
        try { await api("/api/me/password", { method: "POST", body: JSON.stringify({ current: f.get("current"), new: f.get("new") }) }); close(); state.me.must_change = false; }
        catch (err) { m.querySelector("#perr").innerHTML = `<div class="error">${esc(err.message)}</div>`; }
      };
    });
  }

  // ------------------------------------------------------------------ routing
  function route() {
    if (!state.me) return;
    const id = location.hash.replace(/^#\//, "");
    if (!id) {
      const first = state.me.pages[0]?.id || (state.me.can_load ? "load" : "");
      location.replace("#/" + first); return;
    }
    if (id === "load") loadPage();
    else if (id === "users") usersPage();
    else dashboard(id);
    if (state.me.must_change && !document.querySelector(".modal")) passwordModal(true);
  }

  let resizeTimer;
  addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const lp = state.lastPage; if (!lp) return;
      lp.d.charts.forEach((c, i) => { const el = document.getElementById("c" + i); if (el && !c.note) render(el, c, state.tableView[lp.pageId + i]); });
    }, 150);
  });

  async function boot() {
    try { state.me = await api("/api/me"); }
    catch (e) { return; }
    const periods = state.me.filters.periods;
    if (!state.filters.period && periods.length) state.filters.period = periods[periods.length - 1].period;
    if (!state.routed) { addEventListener("hashchange", route); state.routed = true; }
    route();
  }
  boot();
})();
