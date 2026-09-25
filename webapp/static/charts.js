/* Charts for PUC Analytics: plain SVG/HTML, no library.
   Marks follow the dataviz spec: thin bars with 4px rounded data ends, hairline
   grid, one axis, tooltips on every mark, a table view for every chart.
   Series colour follows the entity (a utility keeps its colour on every page). */
(function () {
  const CURRENCY = "SCR";
  const SERIES = { "Water": "--s1", "Electricity": "--s2", "Sewerage": "--s3" };
  const ORDER = ["--s1", "--s2", "--s3"];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function compact(v) {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 1 : 2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e8 ? 0 : a >= 1e7 ? 1 : 2) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "K";
    return v.toLocaleString(undefined, { maximumFractionDigits: a < 10 ? 2 : 0 });
  }
  const full = (v, d = 0) => v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });

  /* value -> {text, unit}; short = compact form for tiles and axes */
  function fmt(v, kind, unit, short = true) {
    if (v === null || v === undefined || Number.isNaN(v)) return { text: "—", unit: "" };
    switch (kind) {
      case "money": return { text: short ? compact(v) : full(v), unit: CURRENCY };
      case "qty": return { text: short ? compact(v) : full(v, Math.abs(v) < 100 ? 1 : 0), unit: unit || "" };
      case "rate": return { text: full(v, 2), unit: unit ? `${CURRENCY}/${unit}` : CURRENCY };
      case "num": return { text: short && Math.abs(v) >= 1e4 ? compact(v) : v.toLocaleString(undefined, { maximumFractionDigits: 2 }), unit: "" };
      case "pct": return { text: (v * 100).toLocaleString(undefined, { maximumFractionDigits: 1, minimumFractionDigits: 1 }) + "%", unit: "" };
      default: return { text: short && Math.abs(v) >= 1e6 ? compact(v) : full(v), unit: "" };
    }
  }
  const fmtText = (v, kind, unit, short) => { const f = fmt(v, kind, unit, short); return f.unit && f.text !== "—" ? (kind === "money" ? `${f.unit} ${f.text}` : `${f.text} ${f.unit}`) : f.text; };

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function seriesColor(name, i) { return `var(${SERIES[name] || ORDER[i % ORDER.length]})`; }

  // ---------- tooltip ----------
  const tip = () => document.getElementById("tooltip");
  function showTip(evt, html) {
    const t = tip(); t.innerHTML = html; t.hidden = false;
    const r = t.getBoundingClientRect();
    let x = evt.clientX + 14, y = evt.clientY + 14;
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
    t.style.left = x + "px"; t.style.top = y + "px";
  }
  const hideTip = () => { tip().hidden = true; };
  function bindTips(root) {
    root.querySelectorAll("[data-tip]").forEach((el) => {
      el.addEventListener("mousemove", (e) => showTip(e, el.dataset.tip));
      el.addEventListener("mouseleave", hideTip);
      el.addEventListener("focus", (e) => { const r = el.getBoundingClientRect(); showTip({ clientX: r.right, clientY: r.top }, el.dataset.tip); });
      el.addEventListener("blur", hideTip);
    });
  }

  function tipHtml(title, lines) {
    return `<b>${esc(title)}</b>` + lines.map(([k, v, color]) => v === "" ? `<div class="tip-hint">${esc(k)}</div>` :
      `<div class="row"><span>${color ? `<i style="background:${color}"></i>` : ""}${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  }

  // ---------- horizontal bars (ranked lists) ----------
  function hbar(el, c) {
    const m = c.metrics[0];
    const vals = c.rows.map((r) => r.values[0] ?? 0);
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
    const zero = (-lo / span) * 100;
    el.innerHTML = `<div class="hbars">` + c.rows.map((r, i) => {
      const v = r.values[0];
      const w = (Math.abs(v ?? 0) / span) * 100;
      const left = v < 0 ? zero - w : zero;
      const tipLines = [[m.label, fmtText(v, m.fmt, c.unit, false)]];
      if (r.shares && r.shares[0] != null) tipLines.push(["Share", fmtText(r.shares[0], "pct")]);
      return `<div class="row-hit${pickCls(c, r)}" tabindex="0" data-row="${i}" data-tip="${esc(tipHtml(r.label, withHint(c, tipLines)))}">
        <div class="lab" title="${esc(r.label)}">${esc(r.label)}</div>
        <div class="track">${lo < 0 ? `<div class="zero" style="left:${zero}%"></div>` : ""}
          <div class="bar${v < 0 ? " neg" : ""}" style="left:${left}%;width:0" data-w="${Math.max(w, v ? 0.6 : 0)}%"></div></div>
        <div class="val">${esc(fmtText(v, m.fmt, c.unit))}</div></div>`;
    }).join("") + `</div>`;
    requestAnimationFrame(() => el.querySelectorAll(".bar").forEach((b) => { b.style.width = b.dataset.w; }));
    bindTips(el);
  }

  // ---------- vertical columns, single series or grouped ----------
  function niceTicks(lo, hi, n = 4) {
    const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => span / s <= n) || 10 * mag;
    // first tick at or below the minimum, last at or above the maximum
    const t = [], a = Math.floor(lo / step + 1e-9), b = Math.ceil(hi / step - 1e-9);
    for (let k = a; k <= b; k++) t.push(+(k * step).toPrecision(12));
    return t.length > 1 ? t : [0, step];
  }

  function columns(el, c) {
    const grouped = c.kind === "grouped";
    const cats = [...new Set(c.rows.map((r) => r.label))];
    const series = grouped ? [...new Set(c.rows.map((r) => r.series))].sort((a, b) => (SERIES[a] ? 0 : 1) - (SERIES[b] ? 0 : 1) || a.localeCompare(b)) : [null];
    const val = (cat, s) => { const r = c.rows.find((r) => r.label === cat && (!grouped || r.series === s)); return r ? r.values[0] : null; };
    const all = cats.flatMap((k) => series.map((s) => val(k, s) ?? 0));
    const ticks = niceTicks(Math.min(0, ...all), Math.max(0, ...all));
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const W = Math.max(el.clientWidth, 280), H = 250, L = 52, R = 8, T = 18, B = 42;
    const pw = W - L - R, ph = H - T - B, bandW = pw / cats.length;
    const inner = Math.min(bandW * 0.7, grouped ? 26 * series.length + 2 * (series.length - 1) : 56);
    const barW = (inner - 2 * (series.length - 1)) / series.length;
    const y = (v) => T + ph - ((v - lo) / (hi - lo || 1)) * ph;
    const m = c.metrics[0];
    let svg = `<svg class="svgchart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(c.title)}">`;
    ticks.forEach((t) => {
      svg += `<line class="${t === 0 ? "baseline" : "gridline"}" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/>`;
      svg += `<text x="${L - 8}" y="${y(t) + 4}" text-anchor="end" class="num">${esc(fmt(t, m.fmt, c.unit).text)}</text>`;
    });
    const maxChars = Math.max(4, Math.floor(bandW / 6.6));
    cats.forEach((cat, i) => {
      const x0 = L + i * bandW + (bandW - inner) / 2;
      const short = cat.length > maxChars ? cat.slice(0, maxChars - 1) + "…" : cat;
      svg += `<text x="${L + i * bandW + bandW / 2}" y="${H - B + 18}" text-anchor="middle">${esc(short)}</text>`;
      series.forEach((s, j) => {
        const v = val(cat, s); if (v === null) return;
        const x = x0 + j * (barW + 2), y0 = y(0), y1 = y(v), h = Math.max(Math.abs(y0 - y1), v ? 1 : 0);
        const top = Math.min(y0, y1), r = Math.min(4, barW / 2, h);
        const color = grouped ? seriesColor(s, j) : "var(--accent, var(--s1))";
        const lines = grouped ? series.map((ss, k) => [ss, fmtText(val(cat, ss), m.fmt, c.unit, false), seriesColor(ss, k)])
                              : [[m.label, fmtText(v, m.fmt, c.unit, false)]];
        const row = c.rows.find((rr) => rr.label === cat);
        if (!grouped && row && row.shares && row.shares[0] != null) lines.push(["Share", fmtText(row.shares[0], "pct")]);
        // rounded only at the data end, anchored square to the baseline
        const d = v >= 0
          ? `M${x},${top + h} V${top + r} Q${x},${top} ${x + r},${top} H${x + barW - r} Q${x + barW},${top} ${x + barW},${top + r} V${top + h} Z`
          : `M${x},${top} V${top + h - r} Q${x},${top + h} ${x + r},${top + h} H${x + barW - r} Q${x + barW},${top + h} ${x + barW},${top + h - r} V${top} Z`;
        const ri = c.rows.findIndex((rr) => rr.label === cat);
        svg += `<g class="col${pickCls(c, c.rows[ri])}" tabindex="0" data-row="${ri}" data-tip="${esc(tipHtml(cat, withHint(c, lines)))}">
          <rect class="hit" x="${L + i * bandW}" y="${T}" width="${bandW}" height="${ph}"/>
          <path class="mark" d="${d}" style="fill:${color}"/></g>`;
      });
    });
    // selective direct label: the largest value only
    if (!grouped && cats.length) {
      let bi = 0; cats.forEach((k, i) => { if ((val(k) ?? -Infinity) > (val(cats[bi]) ?? -Infinity)) bi = i; });
      const v = val(cats[bi]);
      if (v > 0) svg += `<text class="vlabel" x="${L + bi * bandW + bandW / 2}" y="${y(v) - 6}" text-anchor="middle">${esc(fmtText(v, m.fmt, c.unit))}</text>`;
    }
    svg += `</svg>`;
    const legend = grouped ? `<div class="legend">${series.map((s, j) => `<span><i style="background:${seriesColor(s, j)}"></i>${esc(s)}</span>`).join("")}</div>` : "";
    el.innerHTML = legend + svg;
    bindTips(el);
  }

  // ---------- table (also the table view of every chart) ----------
  function table(el, c) {
    const head = [`<th>${esc(c.dim_label)}</th>`];
    if (c.series_label) head.push(`<th>${esc(c.series_label)}</th>`);
    c.metrics.forEach((m, i) => {
      head.push(`<th class="n">${esc(m.label)}${m.fmt === "qty" && c.unit ? ` (${esc(c.unit)})` : ""}</th>`);
      if (c.share.includes(m.key)) head.push(`<th class="n">Share</th>`);
    });
    // an in-cell bar on the first measure, so a table still reads at a glance
    const m0 = c.metrics[0], firstVals = c.rows.map((r) => r.values[0] || 0);
    const barMax = ["money", "qty", "count"].includes(m0.fmt) && firstVals.every((v) => v >= 0) ? Math.max(...firstVals) : 0;
    const body = c.rows.map((r) => {
      const cells = [`<td>${esc(r.label)}</td>`];
      if (c.series_label) cells.push(`<td>${esc(r.series)}</td>`);
      c.metrics.forEach((m, i) => {
        const f = fmt(r.values[i], m.fmt, c.unit, false);
        cells.push(i === 0 && barMax > 0
          ? `<td class="n barcell"><div class="cellwrap"><span class="celltrack"><span class="cellbar" style="width:${((r.values[0] || 0) / barMax) * 100}%"></span></span><span>${esc(f.text)}</span></div></td>`
          : `<td class="n">${esc(f.text)}</td>`);
        if (c.share.includes(m.key)) cells.push(`<td class="n share">${esc(fmtText(r.shares?.[i], "pct"))}</td>`);
      });
      return `<tr class="${pickCls(c, r).trim()}" data-row="${c.rows.indexOf(r)}" ${c.filter_key && r.filter ? `tabindex="0" title="Click to filter the page by ${esc(r.label)}"` : ""}>${cells.join("")}</tr>`;
    }).join("");
    const moneyNote = c.metrics.some((m) => m.fmt === "money") ? ` <span class="meta">Amounts in ${CURRENCY}</span>` : "";
    el.innerHTML = `<div class="tbl-wrap ${c.rows.length > 12 ? "scroll-y" : ""}"><table class="data"><thead><tr>${head.join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  // ---------- click a bar / column / row to filter the page by it ----------
  // c.filter_key names the page filter this chart's dimension drives; c.picked is its current value.
  function pickCls(c, r) {
    if (!c.filter_key || !r || !r.filter) return "";
    if (c.picked == null || c.picked === "") return " pickable";
    return String(r.filter.value) === String(c.picked) ? " pickable picked" : " pickable unpicked";
  }
  function withHint(c, lines) {
    return c.filter_key ? lines.concat([[c.picked != null && c.picked !== "" ? "Click to pick / clear" : "Click to filter the page", ""]]) : lines;
  }
  function bindPick(el, c) {
    if (!c.filter_key) return;
    const fire = (node) => {
      const r = c.rows[+node.dataset.row]; if (!r || !r.filter) return;
      hideTip();
      el.dispatchEvent(new CustomEvent("chart-filter", { bubbles: true, detail: r.filter }));
    };
    el.querySelectorAll("[data-row]").forEach((node) => {
      if (!c.rows[+node.dataset.row]?.filter) return;
      node.addEventListener("click", () => fire(node));
      node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire(node); } });
    });
  }

  function render(el, c, asTable) {
    if (!c.rows.length) { el.innerHTML = `<div class="note">No data for this selection.</div>`; return; }
    if (asTable || c.kind === "table") table(el, c);
    else if (c.kind === "bar") hbar(el, c);
    else columns(el, c);
    bindPick(el, c);
  }

  window.PUCCharts = { render, fmt, fmtText, esc, compact, CURRENCY };
})();
