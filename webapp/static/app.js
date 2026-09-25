/* PUC Analytics front end: hash routes, no build step. */
(function () {
  const { render, fmt, esc, CURRENCY } = window.PUCCharts;
  const app = document.getElementById("app");
  const state = { me: null, filters: { period: "", region: 0, utility: 0, sector: "", tariff: "", compare: "" }, tableView: {}, lastPage: null };
  const routeId = () => location.hash.replace(/^#\//, "").split("?")[0];
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
        <a href="#/load" class="bg-job" id="bg-job" hidden></a>
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
    renderBg();
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
  // ------------------------------------------------------------------ filters
  const pageUtils = (page) => page.utility ? [page.utility] : state.filters.utility ? [state.filters.utility] : state.me.filters.utilities.map((u) => u.code);
  const tariffInfo = (name) => state.me.filters.tariffs.find((t) => t.name === name);
  // a tariff group picked on another page only applies where its utility is shown
  const tariffApplies = (page) => { const t = tariffInfo(state.filters.tariff); return !!t && t.utilities.some((u) => pageUtils(page).includes(+u)); };
  const periodText = (p) => (state.me.filters.periods.find((x) => x.period === p) || {}).period_label || p;
  const UTIL_NAMES = { 1: "Electricity", 2: "Sewerage", 3: "Water" };

  function filterBar(page) {
    const f = state.me.filters, s = state.filters;
    const opt = (v, label, cur) => `<option value="${esc(String(v))}" ${String(cur) === String(v) ? "selected" : ""}>${esc(label)}</option>`;
    const periods = opt("", "All periods", s.period) + f.periods.slice().reverse().map((p) => opt(p.period, p.period_label, s.period)).join("");
    const field = (id, label, inner, extra = "") => `<div class="sep"></div><label ${extra}>${label}<select id="${id}">${inner}</select></label>`;
    const others = f.periods.filter((p) => p.period !== s.period);
    const compare = s.period && others.length ? field("f-compare", "Compare with",
      opt("", "Previous period", s.compare) + others.slice().reverse().map((p) => opt(p.period, p.period_label, s.compare)).join("") + opt("none", "No comparison", s.compare)) : "";
    const regions = f.region_locked ? "" : field("f-region", "Island", opt(0, "All islands", s.region) + f.regions.map((r) => opt(r.code, r.name, s.region)).join(""));
    const utils = page.utility || f.utilities.length < 2 ? "" : field("f-utility", "Utility", opt(0, "All utilities", s.utility) + f.utilities.map((u) => opt(u.code, u.name, s.utility)).join(""));
    const sectors = field("f-sector", "Sector", opt("", "All sectors", s.sector) + f.sectors.map((x) => opt(x, x, s.sector)).join(""));
    const shown = pageUtils(page);
    const groups = shown.map((u) => {
      const list = f.tariffs.filter((t) => t.utilities.map(Number).includes(u));
      return list.length ? `<optgroup label="${esc(UTIL_NAMES[u])}">${list.map((t) => opt(t.name, t.name, tariffApplies(page) ? s.tariff : "")).join("")}</optgroup>` : "";
    }).join("");
    // only the tariff groups of the utility on screen: the page's own, or the one picked in the Utility filter
    const narrowed = shown.length === 1 && f.utilities.length > 1;
    const tariffOpts = narrowed ? f.tariffs.filter((t) => t.utilities.map(Number).includes(shown[0])).map((t) => opt(t.name, t.name, tariffApplies(page) ? s.tariff : "")).join("") : groups;
    const tariffs = field("f-tariff", narrowed ? `Tariff group · ${UTIL_NAMES[shown[0]]}` : "Tariff group",
      opt("", narrowed ? `All ${UTIL_NAMES[shown[0]].toLowerCase()} tariffs` : "All tariff groups", "") + tariffOpts, `class="wide-sel"`);
    return `<div class="filters" id="filters"><label>Period<select id="f-period">${periods}</select></label>${compare}${regions}${utils}${sectors}${tariffs}</div>`;
  }

  // the filters in effect, as removable chips
  function activeChips(page) {
    const f = state.me.filters, s = state.filters, chips = [];
    const chip = (key, label, value, off) => chips.push(`<button class="fchip${off ? " off" : ""}" data-clear="${key}" title="${off ? "Not used on this page. " : ""}Remove this filter">
      <span>${esc(label)}</span><b>${esc(value)}</b>${off ? `<span>· not on this page</span>` : ""}${icon("x")}</button>`);
    if (s.region && !f.region_locked) chip("region", "Island", (f.regions.find((r) => r.code === +s.region) || {}).name || s.region);
    if (s.utility && !page.utility) chip("utility", "Utility", UTIL_NAMES[s.utility]);
    if (s.sector) chip("sector", "Sector", s.sector);
    if (s.tariff) chip("tariff", "Tariff group", s.tariff, !tariffApplies(page));
    if (s.period && s.compare) chip("compare", "Compared with", s.compare === "none" ? "nothing" : periodText(s.compare));
    if (!chips.length) return `<div class="active-filters empty"><p class="af-hint">${icon("spark")}Tip: click a bar in any chart marked <span class="pk-dot"></span> to filter the page by it.</p></div>`;
    return `<div class="active-filters"><span class="af-label">Filtered by</span>${chips.join("")}${chips.length > 1 ? `<button class="btn small ghost" data-clear="all">Clear all</button>` : ""}</div>`;
  }

  // keep the filters in the address, so a filtered view can be bookmarked, reloaded or shared
  function writeUrl(pageId) {
    const s = state.filters, q = new URLSearchParams();
    Object.entries(s).forEach(([k, v]) => { if (v && !(k === "compare" && !s.period)) q.set(k, v); });
    const h = `#/${pageId}${q.toString() ? "?" + q : ""}`;
    if (location.hash !== h) history.replaceState(null, "", h);
  }
  function readUrl() {
    const q = new URLSearchParams(location.hash.split("?")[1] || "");
    if (![...q.keys()].length) return;
    const f = state.me.filters, s = state.filters;
    if (q.has("period")) s.period = f.periods.some((p) => p.period === q.get("period")) ? q.get("period") : "";
    s.region = f.regions.some((r) => r.code === +q.get("region")) ? +q.get("region") : 0;
    s.utility = f.utilities.some((u) => u.code === +q.get("utility")) ? +q.get("utility") : 0;
    s.sector = f.sectors.includes(q.get("sector")) ? q.get("sector") : "";
    s.tariff = tariffInfo(q.get("tariff")) ? q.get("tariff") : "";
    s.compare = q.get("compare") === "none" || f.periods.some((p) => p.period === q.get("compare")) ? q.get("compare") : "";
  }

  function scopeChips(page) {
    const me = state.me, chips = [];
    if (me.filters.region_locked) chips.push(`${icon("lock")} ${esc(me.island)} only`);
    if (me.filters.utilities.length < 3 && !page.utility) chips.push(`${icon("lock")} ${me.filters.utilities.map((u) => esc(u.name)).join(" and ")} only`);
    return chips.length ? `<div class="chips">${chips.map((c) => `<span class="chip">${c}</span>`).join("")}</div>` : "";
  }

  const TILE_ICON = { money: "coin", qty: "pulse", rate: "tag", pct: "percent", count: "users" };

  function tileHtml(t, i) {
    const f = fmt(t.value, t.fmt, t.unit);
    const empty = t.value === null;
    const valueHtml = empty ? "—" : t.fmt === "money" ? `<small class="cur">${CURRENCY}</small>${esc(f.text)}` : `${esc(f.text)}${f.unit ? `<small>${esc(f.unit)}</small>` : ""}`;
    const hasPrev = t.previous !== null && t.previous !== undefined && t.value !== null && t.previous !== 0;
    let foot = "";
    if (empty && (t.fmt === "qty" || t.fmt === "rate")) foot = `<div class="hint">Pick one utility to see this</div>`;
    else if (hasPrev) {
      const ch = (t.value - t.previous) / Math.abs(t.previous);
      foot = `<div class="delta"><span class="delta-pill ${ch >= 0 ? "up" : "down"}">${icon(ch >= 0 ? "up" : "down")}${Math.abs(ch * 100).toFixed(1)}%</span> vs ${esc(t.previous_label)}</div>`;
    }
    const hero = i === 0 && !empty;
    let compare = "";
    if (hero && hasPrev) {  // the headline number against the previous period, as two bars
      const mx = Math.max(Math.abs(t.value), Math.abs(t.previous)) || 1;
      const row = (label, v, cls) => `<div class="cmp-row"><span>${esc(label)}</span><div class="cmp-track"><div class="cmp-bar ${cls}" style="width:${(Math.abs(v) / mx) * 100}%"></div></div><b>${esc(fmt(v, t.fmt, t.unit).text)}</b></div>`;
      compare = `<div class="cmp">${row(t.previous_label, t.previous, "prev")}${row(periodName(), t.value, "cur")}</div>`;
    }
    return `<div class="tile ${empty ? "empty" : ""} ${hero ? "hero" : ""}">
      <div class="tile-top"><div class="label">${esc(t.label)}</div><span class="tile-ic">${icon(TILE_ICON[t.fmt] || "grid")}</span></div>
      <div class="value">${valueHtml}</div>${compare}${foot}</div>`;
  }
  const periodName = () => (state.me.filters.periods.find((p) => p.period === state.filters.period) || {}).period_label || "Selected";

  /* one sentence per chart: what a reader should notice first */
  function insight(c) {
    if (c.note || !c.rows.length || c.kind === "grouped") return "";
    const m = c.metrics[0];
    if (!["money", "qty"].includes(m.fmt) || c.rows.length < 2) return "";
    const vals = c.rows.map((r) => r.values[0] || 0);
    const total = vals.reduce((a, b) => a + b, 0);
    if (total <= 0 || vals.some((v) => v < 0)) return "";
    if (c.dim_label === "Period") {
      const a = vals[vals.length - 2], b = vals[vals.length - 1];
      if (!a) return "";
      const ch = (b - a) / Math.abs(a);
      return `${esc(c.rows[c.rows.length - 1].label)} is ${ch >= 0 ? "up" : "down"} <b>${Math.abs(ch * 100).toFixed(1)}%</b> on ${esc(c.rows[c.rows.length - 2].label)}`;
    }
    const pi = c.picked != null && c.picked !== "" ? c.rows.findIndex((r) => r.filter && String(r.filter.value) === String(c.picked)) : -1;
    if (pi >= 0 && c.dim_label !== "Period") {  // a picked bar: say where it stands
      return `<b>${esc(c.rows[pi].label)}</b> is <b>${Math.round((vals[pi] / total) * 100)}%</b> of the total here, ranked ${pi === vals.indexOf(Math.max(...vals)) ? "first" : `#${[...vals].sort((a, b) => b - a).indexOf(vals[pi]) + 1} of ${vals.length}`}`;
    }
    let bi = 0; vals.forEach((v, i) => { if (v > vals[bi]) bi = i; });
    const share = vals[bi] / total;
    const limited = c.rows.length >= 12 ? " of the top " + c.rows.length : "";
    return `<b>${esc(c.rows[bi].label)}</b> accounts for <b>${Math.round(share * 100)}%</b>${limited}`;
  }

  async function dashboard(pageId) {
    const page = state.me.pages.find((p) => p.id === pageId);
    if (!page) return notAllowed();
    const main = shell(pageId);
    const periodLabel = () => (state.me.filters.periods.find((p) => p.period === state.filters.period) || {}).period_label || "All periods";
    const accent = { 1: "elec", 2: "sewer", 3: "water" }[page.utility] || "brand";
    const ps = state.me.filters.periods;
    const coverage = ps.length ? `${icon("check")} Data: ${esc(ps[0].period_label)}${ps.length > 1 ? " – " + esc(ps[ps.length - 1].period_label) : ""}` : "";
    main.innerHTML = mobileTop(page.title) + `
      <header class="hero-head accent-${accent}">
        <div class="hero-title"><span class="hero-ic">${icon(page.icon)}</span>
          <div><h1>${esc(page.title)}</h1><p class="subtitle" id="subtitle">&nbsp;</p>
          <div class="chips">${coverage ? `<span class="chip soft">${coverage}</span>` : ""}<span class="chip soft built" id="built" hidden></span>${scopeChips(page).replace(/^<div class="chips">|<\/div>$/g, "")}</div></div></div>
        <div class="filter-zone" id="filter-zone"></div>
      </header>
      <div id="dash" class="accent-${accent}"><div class="tiles">${"<div class='skeleton'></div>".repeat(4)}</div></div>`;
    const zone = main.querySelector("#filter-zone");
    function drawFilters() {
      zone.innerHTML = filterBar(page) + activeChips(page);
      zone.querySelectorAll(".filters select").forEach((sel) => sel.addEventListener("change", onChange));
      zone.querySelectorAll("[data-clear]").forEach((b) => b.addEventListener("click", () => {
        const k = b.dataset.clear, s = state.filters;
        if (k === "all") Object.assign(s, { region: state.me.filters.region_locked ? s.region : 0, utility: 0, sector: "", tariff: "", compare: "" });
        else s[k] = typeof s[k] === "number" ? 0 : "";
        refresh();
      }));
    }
    function onChange(e) {
      const s = state.filters, v = e.target.value;
      switch (e.target.id) {
        case "f-period": s.period = v; s.compare = ""; break;  // a comparison belongs to the period it was picked for
        case "f-compare": s.compare = v; break;
        case "f-region": s.region = +v; break;
        case "f-utility":  // a tariff group of another utility no longer fits: drop it
          s.utility = +v;
          if (s.tariff && s.utility && !(tariffInfo(s.tariff)?.utilities || []).map(Number).includes(s.utility)) s.tariff = "";
          break;
        case "f-sector": s.sector = v; break;
        case "f-tariff": s.tariff = v; break;
      }
      refresh();
    }
    function refresh() { drawFilters(); load(); }
    // a click on a chart bar arrives here as a filter; clicking the picked bar again clears it
    main.addEventListener("chart-filter", (e) => {
      const { key, value } = e.detail, s = state.filters;
      const val = key === "region" || key === "utility" ? +value : value;
      s[key] = String(s[key]) === String(val) ? (typeof s[key] === "number" ? 0 : "") : val;
      if (key === "period") s.compare = "";
      refresh();
    });
    drawFilters();

    async function load() {
      const dash = main.querySelector("#dash");
      dash.classList.add("loading");
      const s = state.filters;
      writeUrl(pageId);
      const qs = new URLSearchParams({ period: s.period, region: s.region, utility: page.utility ? 0 : s.utility, sector: s.sector,
                                       tariff: tariffApplies(page) ? s.tariff : "", compare: s.period ? s.compare : "" });
      let d;
      try { d = await api(`/api/page/${pageId}?${qs}`); }
      catch (err) { dash.innerHTML = `<div class="note">${icon("x")} ${esc(err.message)}</div>`; dash.classList.remove("loading"); return; }
      if (d.empty) return noData(dash);
      main.querySelector("#subtitle").textContent = `${d.subtitle} · ${periodLabel()}`;
      const b = d.built, chip = main.querySelector("#built");
      if (b && chip) {  // how this page was made: raw rows read and aggregated just now, or reused
        const secs = b.ms >= 1000 ? `${(b.ms / 1000).toFixed(1)} s` : `${b.ms} ms`;
        const fresh = b.queries - b.from_cache;
        chip.hidden = false;
        chip.innerHTML = fresh ? `${icon("pulse")} Aggregated from ${b.raw_rows_read.toLocaleString()} raw rows · ${secs}`
                               : `${icon("pulse")} Reused, aggregated earlier from raw rows · ${secs}`;
        chip.title = `${b.queries} queries, ${fresh} run on the raw rows just now, ${b.from_cache} reused from memory until the data changes`;
      }
      const pickedOf = { region: s.region || "", utility: s.utility || "", sector: s.sector, tariff: tariffApplies(page) ? s.tariff : "", period: s.period };
      d.charts.forEach((c) => { if (c.filter_key) c.picked = pickedOf[c.filter_key]; });
      const slots = d.tiles.length + (d.tiles[0] && d.tiles[0].value !== null ? 1 : 0);  // the headline tile is two wide
      const cols = slots > 6 ? Math.ceil(slots / 2) : slots;  // too many for one row: two rows
      const fill = slots > 6 ? cols * 2 - slots : 0;  // the last tile stretches over any gap
      dash.innerHTML = `<div class="tiles${fill ? " fill" : ""}" style="--cols:${cols};--fill:${fill + 1}">${d.tiles.map((t, i) => tileHtml(t, i)).join("")}</div>
        <div class="grid">${d.charts.map((c, i) => `
          <section class="card ${c.wide || c.kind === "table" && c.metrics.length > 3 ? "wide" : ""}">
            <div class="card-head"><div><h3>${esc(c.title)}${c.filter_key ? `<span class="pk-dot" title="Click a bar to filter the page"></span>` : ""}</h3><div class="meta">${c.all_periods ? "All periods" : esc(periodLabel())}${c.unit && c.metrics.some((m) => m.fmt === "qty") ? " · " + esc(c.unit) : ""}${c.metrics.some((m) => m.fmt === "money") ? " · " + CURRENCY : ""}</div></div>
            ${c.kind !== "table" && !c.note ? `<button class="btn small ghost" data-toggle="${i}" aria-label="Switch between chart and table">${icon(state.tableView[pageId + i] ? "chart" : "table")}${state.tableView[pageId + i] ? "Chart" : "Table"}</button>` : ""}</div>
            ${insight(c) ? `<div class="insight">${icon("spark")}<span>${insight(c)}</span></div>` : ""}
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

  // ------------------------------------------------------------------ background load
  // The upload and the load job are tracked here, not by the Data Load page, so moving to
  // another page (or reloading once the upload is done) never loses them.
  const bg = { phase: null, name: "", size: 0, upPct: 0, job: null, j: null, error: "" };
  let bgTimer = null, bgListener = null;
  const bgBusy = () => bg.phase === "upload" || bg.phase === "running";

  function bgChanged() { renderBg(); if (bgListener) bgListener(); }

  function renderBg() {
    const el = document.getElementById("bg-job"); if (!el) return;
    if (!bg.phase) { el.hidden = true; return; }
    el.hidden = false; el.className = `bg-job ${bg.phase}`;
    const est = bg.j?.estimate || 0, pct = bg.phase === "upload" ? bg.upPct
      : bg.phase === "running" ? Math.min(95, ((bg.j?.elapsed || 0) / Math.max(est, 1)) * 100) : 100;
    const what = bg.phase === "upload" ? "Uploading" : bg.phase === "running" ? "Loading data"
      : bg.phase === "done" ? "Load finished" : "Load failed";
    const right = bg.phase === "upload" ? `${Math.round(bg.upPct)}%` : bg.phase === "running" ? `${Math.round(bg.j?.elapsed || 0)} s` : "";
    el.title = `${bg.name} (open Data Load)`;
    el.innerHTML = `<div class="bg-row">${bgBusy() ? `<span class="spin"></span>` : icon(bg.phase === "done" ? "check" : "x")}<b>${what}</b><span class="bg-right">${right}</span></div>
      <div class="bg-track"><div class="bg-bar" style="width:${pct}%"></div></div>`;
  }

  function toast(msg, kind) {
    const t = document.createElement("div"); t.className = `toast ${kind || ""}`; t.setAttribute("role", "status"); t.innerHTML = msg;
    document.body.appendChild(t); setTimeout(() => t.classList.add("gone"), 5000); setTimeout(() => t.remove(), 5600);
  }

  function bgUpload(file) {
    Object.assign(bg, { phase: "upload", name: file.name, size: file.size, upPct: 0, job: null, j: null, error: "" });
    bgChanged();
    const fd = new FormData(); fd.append("file", file);
    const x = new XMLHttpRequest();
    x.open("POST", "/api/load");
    x.upload.onprogress = (e) => { if (e.lengthComputable) { bg.upPct = (e.loaded / e.total) * 100; bgChanged(); } };
    x.onload = () => {
      let d = {}; try { d = JSON.parse(x.responseText); } catch (e) {}
      if (x.status === 200 && d.job) bgWatch(d.job, file.name);
      else bgFail(d.detail || `Upload failed (${x.status})`);
    };
    x.onerror = () => bgFail("Upload failed: the connection to the server was lost");
    x.send(fd);
  }

  async function bgRebuild() {
    Object.assign(bg, { phase: "upload", name: "Power BI tables", upPct: 100, job: null, j: null, error: "" });
    bgChanged();
    try { const { job } = await api("/api/rebuild", { method: "POST" }); bgWatch(job, "Power BI tables"); }
    catch (err) { bgFail(err.message); }
  }

  function bgFail(msg) { bg.phase = "failed"; bg.error = msg; bgChanged(); toast(`${icon("x")}<span>${esc(msg)}</span>`, "bad"); }

  function bgWatch(job, name) {
    Object.assign(bg, { phase: "running", job, name: name || bg.name });
    bgChanged();
    clearInterval(bgTimer);
    bgTimer = setInterval(async () => {
      let j; try { j = await api(`/api/load/${job}`); } catch (e) { return; }
      bg.j = j; bg.name = j.file === "(Power BI tables)" ? "Power BI tables" : j.file;
      if (j.status !== "running") {
        clearInterval(bgTimer); bgTimer = null; bg.phase = j.status;
        if (j.status === "done") setTimeout(() => { if (bg.phase === "done" && bg.job === job) { bg.phase = null; renderBg(); } }, 20000);
        if (j.status === "done") {
          try { state.me = await api("/api/me"); } catch (e) {}
          const ps = state.me?.filters.periods || []; if (!state.filters.period && ps.length) state.filters.period = ps[ps.length - 1].period;
        }
        if (routeId() !== "load") toast(j.status === "done" ? `${icon("check")}<span><b>${esc(bg.name)}</b> is loaded. The dashboards now show it.</span>`
                                                                   : `${icon("x")}<span><b>${esc(bg.name)}</b> failed. See Data Load for the log.</span>`, j.status === "done" ? "good" : "bad");
      }
      bgChanged();
    }, 1000);
  }

  async function bgResume() {  // after a sign-in or page reload: pick up a load that is still running
    if (!state.me?.can_load || bgTimer) return;
    try { const d = await api("/api/loads"); if (d.running) bgWatch(d.running.id, d.running.file); } catch (e) {}
  }

  addEventListener("beforeunload", (e) => { if (bg.phase === "upload") { e.preventDefault(); e.returnValue = ""; } });

  // ------------------------------------------------------------------ data load
  async function loadPage() {
    if (!state.me.can_load) return notAllowed();
    const main = shell("load");
    const run = state.me.can_run;
    main.innerHTML = mobileTop("Data Load") + `
      <div class="topbar"><div><h1>Data Load</h1><p class="subtitle">${run ? "Upload a Statistic Report export and press Run: its rows are stored in ClickHouse as they are. Each dashboard reads and aggregates the raw rows it needs when it is opened." : "What has been loaded, and when."}</p></div></div>
      <div class="panels">
        ${run ? `<section class="card">
          <div class="card-head"><div><h3>Load a billing export</h3><div class="meta">.xlsx, .csv, .tsv, .txt, or a .zip of them</div></div><span id="job-status"></span></div>
          <label class="drop" id="drop"><input type="file" id="file" accept=".xlsx,.xlsm,.csv,.tsv,.txt,.zip" hidden>
            <div class="big">${icon("upload")}</div><b>Drop the file here or click to choose</b><small>A file with the same name replaces its earlier load; nothing is counted twice.</small></label>
          <div id="picked"></div>
          <div class="steps" id="steps">
            <div class="step" data-s="0"><b>Upload</b>File to the server</div>
            <div class="step" data-s="1"><b>Read</b>Rows out of the file</div>
            <div class="step" data-s="2"><b>Store</b>Raw rows in ClickHouse</div>
            <div class="step" data-s="3"><b>Check</b>Rows and amounts match</div>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px">
            <button class="btn primary" id="run" disabled>${icon("pulse")}Run</button>
            <button class="btn" id="rebuild" title="Only needed before a Power BI refresh: the dashboards here aggregate the raw rows themselves">Build Power BI tables</button>
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
      main.querySelector("#run").disabled = !f || bgBusy();
      main.querySelector("#unpick")?.addEventListener("click", (e) => { e.preventDefault(); pick(null); });
    };
    if (run) {
      const drop = main.querySelector("#drop"), input = main.querySelector("#file");
      input.addEventListener("change", () => pick(input.files[0]));
      ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
      drop.addEventListener("drop", (e) => pick(e.dataTransfer.files[0]));
      main.querySelector("#run").addEventListener("click", () => { const f = file; pick(null); bgUpload(f); });
      main.querySelector("#rebuild").addEventListener("click", () => bgRebuild());
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
    let lastPhase = null;
    function reflect() {  // draw the background load's state; runs on every change while this page is open
      if (!main.isConnected) { bgListener = null; return; }
      if (!run || !bg.phase) return;
      const busy = bgBusy();
      main.querySelector("#run").disabled = busy || !file; main.querySelector("#rebuild").disabled = busy;
      status(busy ? "running" : bg.phase);
      if (bg.phase === "upload") {
        setSteps(0);
        showConsole(bg.name === "Power BI tables" ? "Building the Power BI tables…" : `Uploading ${bg.name} (${(bg.size / 1e6).toFixed(1)} MB) · ${Math.round(bg.upPct)}%\nYou can open other pages meanwhile; the load carries on.`);
        const box = main.querySelector("#eta"); box.hidden = false;
        main.querySelector("#eta-bar").style.width = bg.upPct + "%"; main.querySelector("#eta-bar").classList.remove("failed");
        main.querySelector("#eta-text").textContent = "Uploading the file"; main.querySelector("#eta-left").textContent = `${Math.round(bg.upPct)}%`;
      } else if (bg.phase === "failed" && !bg.j) {
        showConsole(bg.error);
      } else if (bg.j) {
        const j = bg.j, text = j.log.join("\n"), c = main.querySelector("#console");
        c.hidden = false; c.textContent = text || "Starting…";
        if (j.status === "done") c.textContent += "\n\nDone. The dashboards now show this data.";
        c.scrollTop = c.scrollHeight;
        showEta(j);
        setSteps(/raw rows stored|Power BI tables built/.test(text) ? 4 : /rows read, .* stored/.test(text) ? 3 : /rows staged/.test(text) ? 2 : 1, j.status === "done");
      } else { showConsole("Starting…"); setSteps(1); }
      if (lastPhase === "running" && !busy) refreshTables();
      lastPhase = bg.phase;
    }
    async function refreshTables() {
      const d = await api("/api/loads");
      main.querySelector("#batches").innerHTML = d.batches.length ? `<div class="tbl-wrap"><table class="data"><thead><tr><th>Period</th><th class="n">Billing lines</th><th class="n">Invoices</th><th class="n">Amount (${CURRENCY})</th></tr></thead><tbody>${d.batches.map((b) => `<tr><td>${esc(fmtPeriod(b.period))}</td><td class="n">${(+b.lines).toLocaleString()}</td><td class="n">${(+b.invoices).toLocaleString()}</td><td class="n">${Math.round(b.amount).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<div class="note">Nothing loaded yet.</div>`;
      main.querySelector("#history").innerHTML = d.history.length ? `<div class="tbl-wrap scroll-y"><table class="data"><thead><tr><th>When</th><th>File</th><th class="n">Rows</th><th class="n">Amount</th></tr></thead><tbody>${d.history.map((h) => `<tr><td class="num">${esc(h.loaded_at)}</td><td>${esc(h.file_name)}</td><td class="n">${(+h.rows).toLocaleString()}</td><td class="n">${Math.round(h.amount).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<div class="note">No loads recorded.</div>`;
      if (d.running && !bgTimer) bgWatch(d.running.id, d.running.file);  // e.g. started by another operator
    }
    bgListener = reflect; reflect();
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
    const id = routeId();
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
    readUrl();
    if (!state.routed) { addEventListener("hashchange", route); state.routed = true; }
    route();
    bgResume();
  }
  boot();
})();
