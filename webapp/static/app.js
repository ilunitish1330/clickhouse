/* PUC Analytics front end: hash routes, no build step. */
(function () {
  const { render, fmt, esc, CURRENCY } = window.PUCCharts;
  const app = document.getElementById("app");
  const state = { me: null, filters: { period: "", pfrom: "", pto: "", region: 0, utility: 0, sector: "", tariff: "", compare: "" }, tableView: {}, lastPage: null };
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
    const data = me.can_load || me.can_review ? `<div class="nav-group">Data</div>${me.can_load ? `<a href="#/load" class="${active === "load" ? "active" : ""}">${icon("upload")}Data Load</a>` : ""}${me.can_review ? `<a href="#/review" class="${active === "review" ? "active" : ""}">${icon("edit")}Data Review</a>` : ""}` : "";
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
    // one month, every period combined, or a from-to range
    const cur = s.pfrom ? "range" : s.period;
    const periods = opt("", "All periods (combined)", cur) + f.periods.slice().reverse().map((p) => opt(p.period, p.period_label, cur)).join("")
      + (f.periods.length > 1 ? opt("range", "Range from – to…", cur) : "");
    const field = (id, label, inner, extra = "") => `<div class="sep"></div><label ${extra}>${label}<select id="${id}">${inner}</select></label>`;
    const range = s.pfrom ? field("f-pfrom", "From", f.periods.map((p) => opt(p.period, p.period_label, s.pfrom)).join(""))
      + field("f-pto", "To", f.periods.map((p) => opt(p.period, p.period_label, s.pto)).join("")) : "";
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
    return `<div class="filters" id="filters"><label>Period<select id="f-period">${periods}</select></label>${range}${compare}${regions}${utils}${sectors}${tariffs}</div>`;
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
    const known = (v) => f.periods.some((p) => p.period === v);
    if (known(q.get("pfrom")) && known(q.get("pto"))) { s.pfrom = q.get("pfrom"); s.pto = q.get("pto"); s.period = ""; }
  }
  // "Jun 2025 – Jan 2026", "Jan 2026" or "All periods"
  function periodLabel() {
    const s = state.filters;
    if (s.pfrom) { const [a, b] = [s.pfrom, s.pto].sort(); return a === b ? periodText(a) : `${periodText(a)} – ${periodText(b)}`; }
    return s.period ? periodText(s.period) : "All periods";
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
        case "f-period":  // a comparison belongs to the period it was picked for
          s.compare = "";
          if (v === "range") {
            const ps = state.me.filters.periods;
            s.pfrom = ps[0].period; s.pto = s.period || ps[ps.length - 1].period; s.period = "";
            if (s.pfrom === s.pto) s.pto = ps[ps.length - 1].period;
          } else { s.period = v; s.pfrom = s.pto = ""; }
          break;
        case "f-pfrom": s.pfrom = v; break;
        case "f-pto": s.pto = v; break;
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
      if (key === "period") { s.compare = ""; s.pfrom = s.pto = ""; }
      refresh();
    });
    drawFilters();

    async function load() {
      const dash = main.querySelector("#dash");
      dash.classList.add("loading");
      const s = state.filters;
      writeUrl(pageId);
      const qs = new URLSearchParams({ period: s.period, region: s.region, utility: page.utility ? 0 : s.utility, sector: s.sector,
                                       tariff: tariffApplies(page) ? s.tariff : "", compare: s.period ? s.compare : "",
                                       pfrom: s.pfrom, pto: s.pto });
      let d;
      try { d = await api(`/api/page/${pageId}?${qs}`); }
      catch (err) { dash.innerHTML = `<div class="note">${icon("x")} ${esc(err.message)}</div>`; dash.classList.remove("loading"); return; }
      if (d.empty) return noData(dash);
      main.querySelector("#subtitle").textContent = `${d.subtitle} · ${periodLabel()}`;
      // review status of the data on screen: Final once reviewed, Draft until then
      const chip = main.querySelector("#built"), rv = d.review || [];
      if (chip && rv.length) {
        const drafts = rv.filter((r) => r.status !== "final");
        chip.hidden = false;
        chip.className = `chip status-chip ${drafts.length ? "draft" : "final"}`;
        chip.innerHTML = drafts.length
          ? `${icon("pulse")} Draft · ${drafts.length === rv.length ? "awaiting review" : `${drafts.map((r) => r.periods.join(", ")).join(", ")} awaiting review`}`
          : `${icon("check")} Final · reviewed`;
        chip.title = rv.map((r) => `${r.periods.join(", ")}: ${r.status === "final" ? "Final" : "Draft"}, revision ${r.revision}${r.by ? `, last change by ${r.by} on ${r.at.slice(0, 16)}` : ""}`).join("\n")
          + (d.built ? `\n${d.built.queries} queries, ${d.built.rows_read.toLocaleString()} rows read, ${d.built.ms} ms` : "");
      }
      const slots = d.tiles.length + (d.tiles[0] && d.tiles[0].value !== null ? 1 : 0);  // the headline tile is two wide
      const cols = slots > 6 ? Math.ceil(slots / 2) : slots;  // too many for one row: two rows
      const fill = slots > 6 ? cols * 2 - slots : 0;  // the last tile stretches over any gap
      dash.innerHTML = `<div class="tiles${fill ? " fill" : ""}" style="--cols:${cols};--fill:${fill + 1}">${d.tiles.map((t, i) => tileHtml(t, i)).join("")}</div>
        <div class="grid">${d.charts.map((c, i) => `
          <section class="card ${c.wide || c.kind === "table" && c.metrics.length > 3 ? "wide" : ""}">
            <div class="card-head"><div><h3>${esc(c.title)}${c.filter_key ? `<span class="pk-dot" title="Click a bar to filter the page"></span>` : ""}</h3><div class="meta">${c.all_periods ? (s.pfrom ? esc(periodLabel()) : "All periods") : esc(periodLabel())}${c.unit && c.metrics.some((m) => m.fmt === "qty") ? " · " + esc(c.unit) : ""}${c.metrics.some((m) => m.fmt === "money") ? " · " + CURRENCY : ""}</div></div>
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
    Object.assign(bg, { phase: "upload", name: "Rebuild aggregates", upPct: 100, job: null, j: null, error: "" });
    bgChanged();
    try { const { job } = await api("/api/rebuild", { method: "POST" }); bgWatch(job, "Rebuild aggregates"); }
    catch (err) { bgFail(err.message); }
  }

  function bgFail(msg) { bg.phase = "failed"; bg.error = msg; bgChanged(); toast(`${icon("x")}<span>${esc(msg)}</span>`, "bad"); }

  function bgWatch(job, name) {
    Object.assign(bg, { phase: "running", job, name: name || bg.name });
    bgChanged();
    clearInterval(bgTimer);
    bgTimer = setInterval(async () => {
      let j; try { j = await api(`/api/load/${job}`); } catch (e) { return; }
      bg.j = j; bg.name = j.file;
      if (j.status !== "running") {
        clearInterval(bgTimer); bgTimer = null; bg.phase = j.status;
        if (j.status === "done") setTimeout(() => { if (bg.phase === "done" && bg.job === job) { bg.phase = null; renderBg(); } }, 20000);
        if (j.status === "done") {
          try { state.me = await api("/api/me"); } catch (e) {}
          const ps = state.me?.filters.periods || []; if (!state.filters.period && ps.length) state.filters.period = ps[ps.length - 1].period;
        }
        if (routeId() !== "load") toast(j.status === "done" ? `${icon("check")}<span><b>${esc(bg.name)}</b> is done. The aggregates and dashboards are rebuilt.</span>`
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
      <div class="topbar"><div><h1>Data Load</h1><p class="subtitle">${run ? "Upload a Statistic Report export and press Run: its rows are stored in ClickHouse, the aggregates and dashboards are built, and the data goes live as a Draft until a reviewer marks it reviewed." : "What has been loaded, and when."}</p></div></div>
      <div class="panels">
        ${run ? `<section class="card">
          <div class="card-head"><div><h3>Load a billing export</h3><div class="meta">.xlsx, .csv, .tsv, .txt, or a .zip of them</div></div><span id="job-status"></span></div>
          <label class="drop" id="drop"><input type="file" id="file" accept=".xlsx,.xlsm,.csv,.tsv,.txt,.zip" hidden>
            <div class="big">${icon("upload")}</div><b>Drop the file here or click to choose</b><small>A file with the same name replaces its earlier load; nothing is counted twice.</small></label>
          <div id="picked"></div>
          <div class="steps" id="steps">
            <div class="step" data-s="0"><b>Upload</b>File to the server</div>
            <div class="step" data-s="1"><b>Read</b>Rows out of the file</div>
            <div class="step" data-s="2"><b>Store</b>Rows in ClickHouse, checked</div>
            <div class="step" data-s="3"><b>Build</b>Aggregates and dashboards</div>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px">
            <button class="btn primary" id="run" disabled>${icon("pulse")}Run</button>
            <button class="btn" id="rebuild" title="Rebuild every period's aggregates and dashboards from the stored rows">Rebuild aggregates</button>
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
        showConsole(bg.phase === "upload" && !bg.size ? `Starting: ${bg.name}…` : `Uploading ${bg.name} (${(bg.size / 1e6).toFixed(1)} MB) · ${Math.round(bg.upPct)}%\nYou can open other pages meanwhile; the load carries on.`);
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
        setSteps(/aggregates and dashboards built/.test(text) ? 4 : /building the dashboard tables|rows read, .* stored/.test(text) ? 3 : /rows staged/.test(text) ? 2 : 1, j.status === "done");
      } else { showConsole("Starting…"); setSteps(1); }
      if (lastPhase === "running" && !busy) refreshTables();
      lastPhase = bg.phase;
    }
    async function refreshTables() {
      const d = await api("/api/loads");
      main.querySelector("#batches").innerHTML = d.batches.length ? `<div class="tbl-wrap"><table class="data"><thead><tr><th>Period</th><th>Status</th><th class="n">Billing lines</th><th class="n">Invoices</th><th class="n">Amount (${CURRENCY})</th></tr></thead><tbody>${d.batches.map((b) => `<tr><td>${esc(fmtPeriod(b.period))}</td><td>${statusPill(b.status, b.revision)}</td><td class="n">${(+b.lines).toLocaleString()}</td><td class="n">${(+b.invoices).toLocaleString()}</td><td class="n">${Math.round(b.amount).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<div class="note">Nothing loaded yet.</div>`;
      main.querySelector("#history").innerHTML = d.history.length ? `<div class="tbl-wrap scroll-y"><table class="data"><thead><tr><th>When</th><th>File</th><th class="n">Rows</th><th class="n">Amount</th></tr></thead><tbody>${d.history.map((h) => `<tr><td class="num">${esc(h.loaded_at)}</td><td>${esc(h.file_name)}</td><td class="n">${(+h.rows).toLocaleString()}</td><td class="n">${Math.round(h.amount).toLocaleString()}</td></tr>`).join("")}</tbody></table></div>` : `<div class="note">No loads recorded.</div>`;
      if (d.running && !bgTimer) bgWatch(d.running.id, d.running.file);  // e.g. started by another operator
    }
    bgListener = reflect; reflect();
    await refreshTables();
  }
  // Draft (not reviewed yet) or Final (reviewed), with its revision
  const statusPill = (status, revision) => status === "final"
    ? `<span class="spill final">${icon("check")}Final${+revision > 1 ? ` · rev ${revision}` : ""}</span>`
    : `<span class="spill draft">${icon("pulse")}Draft${+revision > 1 ? ` · rev ${revision}` : ""}</span>`;
  const fmtPeriod = (p) => new Date(p + "T00:00:00").toLocaleDateString(undefined, { month: "short", year: "numeric" });

  // ------------------------------------------------------------------ users
  // ------------------------------------------------------------------ data review
  // Uploads go live as a Draft. A reviewer edits rows here (the edits wait, not on the dashboards),
  // "Finalize modified data" writes them and rebuilds the aggregates and dashboards, and
  // "Mark as reviewed" makes the batch Final. Editing a Final batch starts the next revision.
  const rv = { batch: null, search: "", changed: false, page: 1, utility: 0, region: 0, all: false, tab: "rows", cols: null, job: null };
  async function reviewPage() {
    if (!state.me.can_review) return notAllowed();
    const main = shell("review");
    main.innerHTML = mobileTop("Data Review") + `
      <div class="topbar"><div><h1>Data Review</h1><p class="subtitle">Uploads go live as a <b>Draft</b>. Check the dashboards and the rows, edit rows here if needed, press <b>Finalize modified data</b> to rebuild the aggregates and dashboards, then <b>Mark as reviewed</b> to make the data Final.</p></div></div>
      <div class="rv-batches" id="rv-batches"><div class="skeleton"></div></div>
      <section class="card wide rv-panel" id="rv-panel" hidden></section>`;
    try { if (!rv.cols) rv.cols = await api("/api/review/columns"); } catch (e) { return; }
    const L = (c) => rv.cols.labels[c] || c;
    let batches = [];

    async function loadBatches() {
      const d = await api("/api/review/batches");
      batches = d.batches;
      if (!batches.length) { main.querySelector("#rv-batches").innerHTML = `<div class="note">Nothing uploaded yet.</div>`; return; }
      if (!batches.some((b) => b.batch_id === rv.batch)) rv.batch = batches[0].batch_id;
      main.querySelector("#rv-batches").innerHTML = batches.map((b) => `
        <button class="rv-card ${b.batch_id === rv.batch ? "on" : ""}" data-batch="${esc(b.batch_id)}">
          <span class="rv-period">${esc(b.period ? fmtPeriod(b.period) : "—")}</span>
          ${statusPill(b.status, b.revision)}
          <small>${(+b.lines).toLocaleString()} rows · ${CURRENCY} ${Math.round(b.amount).toLocaleString()}</small>
          ${+b.pending ? `<span class="rv-pending">${icon("edit")}${b.pending} pending edit${+b.pending > 1 ? "s" : ""}</span>` : `<small class="muted">${esc(b.file_name)}</small>`}
        </button>`).join("");
      main.querySelectorAll("[data-batch]").forEach((el) => el.onclick = () => {
        if (rv.batch === el.dataset.batch) return;
        Object.assign(rv, { batch: el.dataset.batch, page: 1, search: "", changed: false, tab: "rows" });
        loadBatches().then(panel);
      });
    }
    const cur = () => batches.find((b) => b.batch_id === rv.batch);

    function panel() {
      const b = cur(), el = main.querySelector("#rv-panel"); if (!b) return;
      el.hidden = false;
      const pending = +b.pending, final = b.status === "final", busy = bgBusy();
      const step = (n, label, state) => `<div class="rv-step ${state}"><span>${state === "done" ? icon("check") : n}</span>${label}</div>`;
      const flow = [
        step(1, "Uploaded, aggregates built", "done"),
        step(2, pending ? `${pending} edit${pending > 1 ? "s" : ""} waiting` : "Edit rows if needed", pending ? "now" : final ? "done" : "now"),
        step(3, "Finalize: rebuild dashboards", pending ? "next" : +b.revision > 1 || final ? "done" : "skip"),
        step(4, final && !pending ? "Reviewed: Final" : "Mark as reviewed", pending ? "wait" : final ? "done" : "next"),
      ].join(`<i class="rv-arrow"></i>`);
      const msg = busy && bg.job === rv.job ? `${icon("pulse")} Finalizing: writing the edits and rebuilding the aggregates and dashboards…`
        : pending ? `${icon("edit")} ${pending} edited row${pending > 1 ? "s are" : " is"} not on the dashboards yet. Finalize to rebuild the aggregates and dashboards with ${pending > 1 ? "them" : "it"}.`
        : final ? `${icon("check")} Reviewed by <b>${esc(b.changed_by)}</b> on ${esc(b.last_change.slice(0, 16))}. Editing a row starts revision ${+b.revision + 1}, which needs reviewing again.`
        : `${icon("spark")} Check the <a href="#/${state.me.pages[0]?.id || "executive"}?period=${esc(b.period)}">dashboards for ${esc(fmtPeriod(b.period))}</a> and the rows below. Edit if needed, then mark the data reviewed.`;
      el.innerHTML = `
        <div class="rv-head"><div><h2>${esc(fmtPeriod(b.period))} ${statusPill(b.status, b.revision)}</h2>
          <div class="meta">${esc(b.file_name)} · ${(+b.lines).toLocaleString()} rows on the dashboards · ${CURRENCY} ${Math.round(b.amount).toLocaleString()} · revision ${b.revision}${b.changed_by ? ` · last change by ${esc(b.changed_by)}, ${esc(b.last_change.slice(0, 16))}` : ""}</div></div>
          <a class="btn small" href="#/${state.me.pages[0]?.id || "executive"}?period=${esc(b.period)}">${icon("chart")}Open dashboards</a></div>
        <div class="rv-flow">${flow}</div>
        <div class="rv-bar ${pending ? "pending" : final ? "final" : "draft"}"><p>${msg}</p><div class="rv-btns">
          ${pending ? `<button class="btn" id="rv-discard" ${busy ? "disabled" : ""}>${icon("undo")}Discard edits</button>
                       <button class="btn primary" id="rv-finalize" ${busy ? "disabled" : ""}>${icon("pulse")}Finalize modified data</button>` : ""}
          ${!final ? `<button class="btn ${pending ? "" : "primary"}" id="rv-reviewed" ${pending || busy ? "disabled" : ""} title="${pending ? "Finalize or discard the pending edits first" : "Make this data Final"}">${icon("check")}Mark as reviewed</button>` : ""}
        </div></div>
        <div class="tabs"><button class="${rv.tab === "rows" ? "on" : ""}" data-tab="rows">${icon("table")}Rows</button><button class="${rv.tab === "history" ? "on" : ""}" data-tab="history">${icon("undo")}Change history</button></div>
        <div id="rv-body"></div>`;
      el.querySelectorAll("[data-tab]").forEach((t) => t.onclick = () => { rv.tab = t.dataset.tab; panel(); });
      el.querySelector("#rv-discard")?.addEventListener("click", async () => {
        if (!confirm(`Discard all ${pending} pending edit(s) of ${fmtPeriod(b.period)}?`)) return;
        await act(`/api/review/${encodeURIComponent(b.batch_id)}/discard`, {}); refresh();
      });
      el.querySelector("#rv-finalize")?.addEventListener("click", async () => {
        try {
          const { job } = await api(`/api/review/${encodeURIComponent(b.batch_id)}/finalize`, { method: "POST" });
          rv.job = job; bgWatch(job, `Finalize ${fmtPeriod(b.period)}`); panel();
        } catch (err) { toast(`${icon("x")}<span>${esc(err.message)}</span>`, "bad"); }
      });
      el.querySelector("#rv-reviewed")?.addEventListener("click", async () => {
        if (await act(`/api/review/${encodeURIComponent(b.batch_id)}/reviewed`, {})) {
          toast(`${icon("check")}<span><b>${esc(fmtPeriod(b.period))}</b> is reviewed and Final.</span>`, "good"); refresh();
        }
      });
      rv.tab === "rows" ? rowsView() : historyView();
    }

    async function act(url, body) {
      try { await api(url, { method: "POST", body: JSON.stringify(body) }); return true; }
      catch (err) { toast(`${icon("x")}<span>${esc(err.message)}</span>`, "bad"); return false; }
    }
    async function refresh() { await loadBatches(); panel(); }

    function rowsView() {
      const body = main.querySelector("#rv-body"), b = cur();
      const opt = (v, label, c) => `<option value="${v}" ${String(c) === String(v) ? "selected" : ""}>${esc(label)}</option>`;
      body.innerHTML = `
        <div class="rv-tools">
          <label class="rv-search">${icon("search")}<input id="rv-q" placeholder="Search customer, connection, invoice, meter or tariff" value="${esc(rv.search)}"></label>
          <select id="rv-util">${opt(0, "All utilities", rv.utility)}${opt(1, "Electricity", rv.utility)}${opt(3, "Water", rv.utility)}${opt(2, "Sewerage", rv.utility)}</select>
          <select id="rv-reg">${opt(0, "All islands", rv.region)}${opt(1, "Mahe", rv.region)}${opt(2, "Praslin", rv.region)}${opt(3, "La Digue", rv.region)}</select>
          <label class="rv-check"><input type="checkbox" id="rv-changed" ${rv.changed ? "checked" : ""}> Edited rows only</label>
          <label class="rv-check"><input type="checkbox" id="rv-all" ${rv.all ? "checked" : ""}> All columns</label>
          <button class="btn small primary" id="rv-add" ${bgBusy() ? "disabled" : ""}>${icon("plus")}Add row</button>
        </div>
        <div id="rv-grid"><div class="skeleton"></div></div>`;
      let t;
      body.querySelector("#rv-q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => { rv.search = e.target.value; rv.page = 1; grid(); }, 350); });
      body.querySelector("#rv-util").onchange = (e) => { rv.utility = +e.target.value; rv.page = 1; grid(); };
      body.querySelector("#rv-reg").onchange = (e) => { rv.region = +e.target.value; rv.page = 1; grid(); };
      body.querySelector("#rv-changed").onchange = (e) => { rv.changed = e.target.checked; rv.page = 1; grid(); };
      body.querySelector("#rv-all").onchange = (e) => { rv.all = e.target.checked; grid(); };
      body.querySelector("#rv-add").onclick = () => addRowModal(b);
      grid();
    }

    async function grid() {
      const box = main.querySelector("#rv-grid"), b = cur(); if (!box) return;
      const qs = new URLSearchParams({ search: rv.search, changed: rv.changed ? 1 : 0, page: rv.page, size: 50, utility: rv.utility, region: rv.region });
      let d; try { d = await api(`/api/review/${encodeURIComponent(b.batch_id)}/rows?${qs}`); }
      catch (err) { box.innerHTML = `<div class="note">${esc(err.message)}</div>`; return; }
      const cols = rv.all ? rv.cols.editable : rv.cols.default, busy = bgBusy();
      const widths = Object.fromEntries(cols.map((c) => [c, Math.min(40, Math.max(4, L(c).length, ...d.rows.map((r) => (r.values[c] || "").length)) + 1)]));
      const from = d.total ? (d.page - 1) * d.size + 1 : 0, to = Math.min(d.total, d.page * d.size);
      box.innerHTML = `
        <div class="tbl-wrap rv-grid"><table class="data rvtable"><thead><tr><th class="n">Line</th>${cols.map((c) => `<th class="${rv.cols.number.includes(c) ? "n" : ""}">${esc(L(c))}</th>`).join("")}<th></th></tr></thead><tbody>
        ${d.rows.map((r) => {
          const del = r.pending === "delete";
          return `<tr class="${r.pending ? "p-" + r.pending : ""}" data-line="${r.line_no}">
            <td class="n num">${r.pending === "insert" ? `<span class="tag new">New</span>` : r.pending === "delete" ? `<span class="tag del">Deleted</span>` : r.pending ? `<span class="tag chg">Edited</span>` : ""}${r.line_no}</td>
            ${cols.map((c) => {
              const was = r.changed[c];
              // each cell as wide as its column's longest value on this page
              return `<td class="${was !== undefined ? "chg" : ""}"><input class="cell ${rv.cols.number.includes(c) ? "n" : ""}" data-col="${c}" value="${esc(r.values[c])}" size="${widths[c]}" ${del || busy ? "disabled" : ""}
                aria-label="${esc(L(c))}, line ${r.line_no}" ${was !== undefined ? `title="Was: ${esc(was || "(empty)")}"` : ""}></td>`;
            }).join("")}
            <td class="n">${r.pending ? `<button class="btn small ghost" data-undo="${r.line_no}" title="Undo the pending change to this row" ${busy ? "disabled" : ""}>${icon("undo")}</button>`
                                      : `<button class="btn small ghost" data-del="${r.line_no}" title="Delete this row" ${busy ? "disabled" : ""}>${icon("trash")}</button>`}</td></tr>`;
        }).join("")}
        </tbody></table></div>
        <div class="rv-pager"><span>${d.total ? `Rows ${from.toLocaleString()}–${to.toLocaleString()} of ${d.total.toLocaleString()}` : "No rows match"}${rv.changed || rv.search || rv.utility || rv.region ? " (filtered)" : ""} · edited rows first</span>
          <span><button class="btn small" id="rv-prev" ${d.page <= 1 ? "disabled" : ""}>Previous</button>
          <button class="btn small" id="rv-next" ${to >= d.total ? "disabled" : ""}>Next</button></span></div>`;
      box.querySelector("#rv-prev").onclick = () => { rv.page--; grid(); };
      box.querySelector("#rv-next").onclick = () => { rv.page++; grid(); };
      box.querySelectorAll("input.cell").forEach((inp) => {
        inp.dataset.orig = inp.value;
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); if (e.key === "Escape") { inp.value = inp.dataset.orig; inp.blur(); } });
        inp.addEventListener("change", async () => {
          const line = +inp.closest("tr").dataset.line;
          inp.classList.add("saving");
          const ok = await act(`/api/review/${encodeURIComponent(b.batch_id)}/edit`, { line_no: line, changes: { [inp.dataset.col]: inp.value } });
          if (!ok) { inp.value = inp.dataset.orig; inp.classList.remove("saving"); return; }
          await loadBatches(); panel();
        });
      });
      box.querySelectorAll("[data-del]").forEach((btn) => btn.onclick = async () => {
        if (await act(`/api/review/${encodeURIComponent(b.batch_id)}/delete`, { line_no: +btn.dataset.del })) { await loadBatches(); panel(); }
      });
      box.querySelectorAll("[data-undo]").forEach((btn) => btn.onclick = async () => {
        if (await act(`/api/review/${encodeURIComponent(b.batch_id)}/undo`, { line_no: +btn.dataset.undo })) { await loadBatches(); panel(); }
      });
    }

    async function historyView() {
      const body = main.querySelector("#rv-body"), b = cur();
      const d = await api(`/api/review/${encodeURIComponent(b.batch_id)}/history`);
      const what = { update: "Edited", insert: "Added row", delete: "Deleted row", undo: "Undid change", discard: "Discarded edits", finalize: "Finalized", reviewed: "Marked reviewed" };
      body.innerHTML = d.history.length ? `<div class="tbl-wrap scroll-y"><table class="data"><thead><tr><th>When</th><th>Who</th><th>What</th><th class="n">Line</th><th>Field</th><th>Before</th><th>After</th><th class="n">Revision</th></tr></thead><tbody>
        ${d.history.map((h) => `<tr><td class="num">${esc(h.ts.slice(0, 19))}</td><td>${esc(h.user)}</td><td>${esc(what[h.action] || h.action)}</td><td class="n">${+h.line_no || ""}</td>
          <td>${esc(h.column ? L(h.column) : "")}</td><td class="muted">${esc(h.old_value)}</td><td>${esc(h.new_value)}</td><td class="n">${h.revision}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="note">No changes yet: this is the data as uploaded.</div>`;
    }

    function addRowModal(b) {
      const f = (c, extra = "") => `<label class="field"><span>${esc(L(c))}</span><input name="${c}" ${extra}></label>`;
      const sel = (c, opts) => `<label class="field"><span>${esc(L(c))}</span><select name="${c}">${opts.map(([v, t]) => `<option value="${v}">${esc(t)}</option>`).join("")}</select></label>`;
      modal(`<h2>Add a row to ${esc(fmtPeriod(b.period))}</h2><p class="sub">The labels for the utility, island and tariff code are filled in from rows that already use them. The row waits with the other edits until you finalize.</p>
        <form id="rf"><div id="rerr"></div><div class="rv-form">
        ${sel("UTILITYTYPE", [["1", "1 Electricity"], ["3", "3 Water"], ["2", "2 Sewerage"]])}
        ${sel("REGION", [["1", "1 Mahe"], ["2", "2 Praslin"], ["3", "3 La Digue"]])}
        ${f("CUSTID", "required")}${f("CONNECTIONID")}${f("INVOICEID")}${f("TARIFFGROUPCODE", 'placeholder="e.g. EMD1"')}
        ${sel("ADDITIONALITEMSTYPE", [["1", "1 Consumption"], ["2", "2 Adjustment"], ["3", "3 Fixed charge"], ["4", "4 Other"]])}
        ${f("ADDITIONALITEMSDESCRIPTION", 'placeholder="e.g. Consumption"')}
        ${f("QUANTITY", 'required inputmode="decimal" placeholder="0"')}${f("AMOUNT", 'required inputmode="decimal" placeholder="0.00"')}
        </div><div class="actions"><button type="button" class="btn" id="rcancel">Cancel</button><button class="btn primary">${icon("plus")}Add row</button></div></form>`,
      (m, close) => {
        m.querySelector("#rcancel").onclick = close;
        m.querySelector("#rf").onsubmit = async (e) => {
          e.preventDefault();
          const values = Object.fromEntries([...new FormData(e.target)].filter(([, v]) => v !== ""));
          try { await api(`/api/review/${encodeURIComponent(b.batch_id)}/add`, { method: "POST", body: JSON.stringify({ values }) }); }
          catch (err) { m.querySelector("#rerr").innerHTML = `<div class="error">${esc(err.message)}</div>`; return; }
          close(); rv.changed = true; rv.page = 1; await loadBatches(); panel();
        };
      });
    }

    // a finalize running in the background: redraw when it finishes
    bgListener = () => {
      if (!main.isConnected) { bgListener = null; return; }
      if (rv.job && bg.job === rv.job && !bgBusy()) { rv.job = null; refresh(); }
    };
    if (bgBusy() && bg.name.startsWith("Finalize")) rv.job = bg.job;
    await loadBatches(); panel();
  }

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
    else if (id === "review") reviewPage();
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
