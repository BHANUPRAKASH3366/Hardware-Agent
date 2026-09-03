/* Hardware Agent — front-end controller.
 *
 * Results stream in over SSE so a fast distributor renders immediately instead
 * of waiting on the slowest one. Filtering, sorting and export all run against
 * the data already in the browser, so those never cost another API call. */

(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const el = {
    form: $("#search-form"),
    q: $("#q"),
    qty: $("#qty"),
    currency: $("#currency"),
    searchBtn: $("#search-btn"),
    examples: $("#examples"),
    sources: $("#sources"),
    stats: $("#stats"),
    toolbar: $("#toolbar"),
    filter: $("#filter"),
    onlyStock: $("#only-stock"),
    onlyFulfillable: $("#only-fulfillable"),
    sort: $("#sort"),
    refreshBtn: $("#refresh-btn"),
    exportBtn: $("#export-btn"),
    results: $("#results-region"),
    toasts: $("#toasts"),
    drawer: $("#drawer"),
    drawerBody: $("#drawer-body"),
    sourceSummary: $("#source-summary"),
    sourceSummaryText: $("#source-summary-text"),
    sourceDot: $("#source-dot"),
    themeToggle: $("#theme-toggle"),
    browseToggle: $("#browse-toggle"),
    categoryPanel: $("#category-panel"),
    categoryGrid: $("#category-grid"),
    categoryCount: $("#category-count"),
    categoryFilter: $("#category-filter"),
    crumb: $("#crumb"),
    photoToggle: $("#photo-toggle"),
    photoPanel: $("#photo-panel"),
    dropzone: $("#dropzone"),
    photoInput: $("#photo-input"),
    photoPreview: $("#photo-preview"),
    dropzonePrompt: $("#dropzone-prompt"),
    photoNote: $("#photo-note"),
    identifyBtn: $("#identify-btn"),
    photoClear: $("#photo-clear"),
    photoTiming: $("#photo-timing"),
    photoOut: $("#photo-out"),
    visionPill: $("#vision-pill"),
    visionDot: $("#vision-dot"),
    visionPillText: $("#vision-pill-text"),
    bomToggle: $("#bom-toggle"),
    bomPanel: $("#bom-panel"),
    bomDrop: $("#bom-drop"),
    bomInput: $("#bom-input"),
    bomFile: $("#bom-file"),
    bomRun: $("#bom-run"),
    bomClear: $("#bom-clear"),
    bomOut: $("#bom-out"),
  };

  const state = {
    meta: null,
    query: "",
    quantity: 100,
    currency: "USD",
    sort: "price_asc",
    view: "flat",
    filter: "",
    onlyStock: false,
    onlyFulfillable: false,
    results: [],
    providers: [],
    summary: null,
    category: null,
    categories: null,
    categoryFilter: "",
    categoryOpen: new Set(),
    elapsedMs: null,
    fetchedAt: null,
    cached: false,
    // Every fresh page load must hit the distributors, never a stored result.
    firstLoad: true,
    loading: false,
    stream: null,
    searchToken: 0,
    // Photo identification, run by the local Ollama model.
    vision: null,
    photo: null,          // { dataUrl, name, kb }
    lastCandidates: [],
    identifying: false,
    identifyTimer: null,
    // Bill-of-materials pricing.
    bomItems: [],
    bomMeta: null,
    bomJob: null,
    bomStream: null,
    bomRunning: false,
    bomCurrency: "USD",
    bomUnits: 1,
  };

  // Infinity - Infinity is NaN, which makes Array.sort behave unpredictably, so
  // every comparison goes through cmp() rather than plain subtraction.
  const cmp = (x, y) => (x < y ? -1 : x > y ? 1 : 0);

  const SORTERS = {
    price_asc:    (a, b) => cmp(price(a), price(b)) || cmp(stock(b), stock(a)),
    price_desc:   (a, b) => cmp(price(b, -1), price(a, -1)) || cmp(stock(b), stock(a)),
    stock_desc:   (a, b) => cmp(stock(b), stock(a)) || cmp(price(a), price(b)),
    stock_asc:    (a, b) => cmp(stock(a), stock(b)) || cmp(price(a), price(b)),
    name_asc:     (a, b) => String(a.mpn).localeCompare(String(b.mpn)) || cmp(price(a), price(b)),
    supplier_asc: (a, b) => String(a.sourceLabel).localeCompare(String(b.sourceLabel)) || cmp(price(a), price(b)),
  };

  function price(row, missing = 1) {
    const v = row.unitPriceDisplay ?? row.unitPrice;
    // Rows without a published price always sink to the bottom, either way.
    return v == null ? missing * Infinity : v;
  }

  // `price` is for ranking, where falling back to the native figure only has
  // to order rows sensibly. Anything shown to the user has to be in the
  // currency the label claims, so display reads the converted value alone and
  // shows nothing when there is not one.
  function shownPrice(row) {
    return row.unitPriceDisplay ?? null;
  }
  function stock(row) {
    return row.stock == null ? -1 : row.stock;
  }

  /* ------------------------------------------------------------ formatting */

  const numFmt = new Intl.NumberFormat(undefined);

  function money(value, currency) {
    if (value == null) return "—";
    const digits = Math.abs(value) < 1 ? 4 : 2;
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency", currency,
        minimumFractionDigits: digits, maximumFractionDigits: digits,
      }).format(value);
    } catch {
      return `${currency} ${value.toFixed(digits)}`;
    }
  }

  /* 1,762,117 in a category chip is noise; 1.8M is the same fact, readable. */
  function compact(value) {
    if (value == null) return "";
    try {
      return new Intl.NumberFormat(undefined, {
        notation: "compact", maximumFractionDigits: 1,
      }).format(value);
    } catch {
      return numFmt.format(value);
    }
  }

  function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /* ------------------------------------------------------ component images */

  /* Live distributors return a real product photo, and that is what gets shown.
   * Where there is no photo — sample rows, or a distributor that omits one — a
   * silhouette of the actual package is drawn instead. A package outline is how
   * you recognise a part on a reel anyway, and it beats a grey "no image" box. */

  // Checked before the package rules: an LED in a PLCC or chip body is an LED
  // first, and reading it as a logic package would be actively misleading.
  const CATEGORY_OVERRIDES = [
    [/\bleds?\b|optoelectronic/, "led"],
  ];

  const PACKAGE_RULES = [
    [/bga|wlcsp|\bcsp\b/, "bga"],
    [/\btqfp|lqfp|\bqfp|mqfp\b/, "qfp"],
    [/qfn|wson|lfcsp|\bdfn|\blga|plcc/, "qfn"],
    [/\bpdip|\bdip\b|dip-\d/, "dip"],
    [/soic|\bsop\b|ssop|tssop|vssop|msop|htssop|powerpad|\bso[\s-]\d/, "soic"],
    [/to-?220|to-?263|to-?247|to-?3\b|multiwatt|dpak/, "to220"],
    [/\bsot|sc-?70|to-?92|sod-?\d/, "sot"],
    [/module/, "module"],
    [/header|receptacle|connector|terminal|\busb\b|socket/, "connector"],
    [/\bcan\b|electrolytic/, "can"],
    [/0201|0402|0603|0805|1206|1210|2010|2512|1812/, "chip"],
    [/3225|2520|5032|7050|crystal|hc-?49/, "crystal"],
    [/\bled\b|[35]\s?mm/, "led"],
    [/\btht?\b|through.?hole/, "tht"],
  ];

  const CATEGORY_RULES = [
    [/led|optoelectronic/, "led"],
    [/crystal|oscillator|timing/, "crystal"],
    [/resistor|capacitor|inductor|passive/, "chip"],
    [/connector|pcb hardware|switch/, "connector"],
    [/diode|transistor|discrete/, "sot"],
    [/module|wireless|rf/, "module"],
    [/memory|fpga|programmable logic/, "qfp"],
  ];

  function packageKind(row) {
    const pkg = String(row.package || "").toLowerCase();
    const context = `${row.category || ""} ${row.description || ""}`.toLowerCase();
    for (const [re, kind] of CATEGORY_OVERRIDES) if (re.test(context)) return kind;
    for (const [re, kind] of PACKAGE_RULES) if (re.test(pkg)) return kind;
    for (const [re, kind] of CATEGORY_RULES) if (re.test(context)) return kind;
    return "ic";
  }

  // Every drawing is a 48x48 viewBox using two theme-aware classes: pk-b (body)
  // and pk-p (pins / metal).
  const PACKAGE_ART = {
    dip: `<rect class="pk-b" x="15" y="9" width="18" height="30" rx="2"/>
      <path class="pk-n" d="M24 9a3.2 3.2 0 0 0 0 6.4z"/>
      ${[13, 20, 27, 34].map((y) =>
        `<rect class="pk-p" x="8" y="${y}" width="7" height="3" rx="1"/>
         <rect class="pk-p" x="33" y="${y}" width="7" height="3" rx="1"/>`).join("")}`,
    soic: `<rect class="pk-b" x="15" y="13" width="18" height="22" rx="1.6"/>
      <circle class="pk-n" cx="19" cy="17" r="1.5"/>
      ${[16, 22, 28].map((y) =>
        `<rect class="pk-p" x="9" y="${y}" width="6" height="2.6" rx="1"/>
         <rect class="pk-p" x="33" y="${y}" width="6" height="2.6" rx="1"/>`).join("")}`,
    qfn: `<rect class="pk-b" x="11" y="11" width="26" height="26" rx="3"/>
      <circle class="pk-n" cx="16" cy="16" r="1.7"/>
      ${[15, 21, 27].map((v) =>
        `<rect class="pk-p" x="${v}" y="11" width="4" height="2.6" rx=".8"/>
         <rect class="pk-p" x="${v}" y="34.4" width="4" height="2.6" rx=".8"/>
         <rect class="pk-p" x="11" y="${v}" width="2.6" height="4" rx=".8"/>
         <rect class="pk-p" x="34.4" y="${v}" width="2.6" height="4" rx=".8"/>`).join("")}`,
    qfp: `<rect class="pk-b" x="14" y="14" width="20" height="20" rx="2"/>
      <circle class="pk-n" cx="18" cy="18" r="1.6"/>
      ${[16, 21, 26, 30].map((v) =>
        `<rect class="pk-p" x="${v}" y="8" width="2.4" height="6" rx=".8"/>
         <rect class="pk-p" x="${v}" y="34" width="2.4" height="6" rx=".8"/>
         <rect class="pk-p" x="8" y="${v}" width="6" height="2.4" rx=".8"/>
         <rect class="pk-p" x="34" y="${v}" width="6" height="2.4" rx=".8"/>`).join("")}`,
    bga: `<rect class="pk-b" x="10" y="10" width="28" height="28" rx="2.5"/>
      ${[16, 21.5, 27, 32].flatMap((x) => [16, 21.5, 27, 32].map((y) =>
        `<circle class="pk-p" cx="${x}" cy="${y}" r="2"/>`)).join("")}`,
    chip: `<rect class="pk-b" x="11" y="18" width="26" height="12" rx="1.5"/>
      <rect class="pk-p" x="11" y="18" width="6" height="12" rx="1.5"/>
      <rect class="pk-p" x="31" y="18" width="6" height="12" rx="1.5"/>`,
    sot: `<rect class="pk-b" x="14" y="15" width="20" height="15" rx="1.8"/>
      <rect class="pk-p" x="16" y="30" width="3" height="8" rx="1"/>
      <rect class="pk-p" x="29" y="30" width="3" height="8" rx="1"/>
      <rect class="pk-p" x="22.5" y="7" width="3" height="8" rx="1"/>`,
    to220: `<rect class="pk-p" x="14" y="7" width="20" height="11" rx="1.5"/>
      <circle class="pk-h" cx="24" cy="12" r="2.6"/>
      <rect class="pk-b" x="14" y="17" width="20" height="15" rx="1.5"/>
      ${[17, 22.5, 28].map((x) =>
        `<rect class="pk-p" x="${x}" y="32" width="3" height="9" rx="1"/>`).join("")}`,
    module: `<rect class="pk-b" x="7" y="12" width="34" height="24" rx="2"/>
      <rect class="pk-p" x="11" y="16" width="18" height="14" rx="1.5"/>
      <path class="pk-p" d="M32 17h6M32 20h6M32 23h6" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round"/>
      ${[10, 15, 20, 25, 30, 35].map((x) =>
        `<rect class="pk-p" x="${x}" y="36" width="3" height="3" rx=".6"/>`).join("")}`,
    connector: `<rect class="pk-b" x="9" y="16" width="30" height="14" rx="1.8"/>
      ${[13, 20, 27, 34].map((x) =>
        `<rect class="pk-h" x="${x}" y="18" width="4" height="4" rx=".6"/>
         <rect class="pk-p" x="${x + 0.8}" y="30" width="2.6" height="9" rx="1"/>`).join("")}`,
    led: `<path class="pk-b" d="M15 24a9 9 0 0 1 18 0v6H15z"/>
      <rect class="pk-b" x="14" y="29" width="20" height="4" rx="1"/>
      <rect class="pk-p" x="19" y="33" width="2.8" height="8" rx="1"/>
      <rect class="pk-p" x="26" y="33" width="2.8" height="8" rx="1"/>`,
    crystal: `<rect class="pk-b" x="10" y="16" width="28" height="16" rx="7.5"/>
      <rect class="pk-p" x="14" y="32" width="3" height="7" rx="1"/>
      <rect class="pk-p" x="31" y="32" width="3" height="7" rx="1"/>`,
    can: `<circle class="pk-b" cx="24" cy="22" r="12"/>
      <path class="pk-h" d="M16 13.5a12 12 0 0 0 0 17z"/>
      <rect class="pk-p" x="20" y="34" width="2.8" height="7" rx="1"/>
      <rect class="pk-p" x="25.5" y="34" width="2.8" height="7" rx="1"/>`,
    tht: `<rect class="pk-b" x="13" y="17" width="22" height="14" rx="2"/>
      <rect class="pk-p" x="17" y="31" width="3" height="8" rx="1"/>
      <rect class="pk-p" x="28" y="31" width="3" height="8" rx="1"/>`,
    ic: `<rect class="pk-b" x="13" y="13" width="22" height="22" rx="2.5"/>
      <circle class="pk-n" cx="18" cy="18" r="1.7"/>
      ${[17, 23, 29].map((y) =>
        `<rect class="pk-p" x="8" y="${y}" width="5" height="2.6" rx="1"/>
         <rect class="pk-p" x="35" y="${y}" width="5" height="2.6" rx="1"/>`).join("")}`,
  };

  /* Photo of last resort for a part number. Mouser's image CDN refuses
   * hotlinked requests and serves an HTML block page instead of the JPEG, so
   * those rows would show a silhouette even though the same manufacturer part
   * is pictured on another distributor's row in the very same table. This maps
   * a normalised MPN to a photo from whichever supplier does serve one. */
  let photoByMpn = Object.create(null);

  function mpnKey(mpn) {
    return String(mpn ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }

  function indexPhotos(rows) {
    photoByMpn = Object.create(null);
    for (const row of rows || []) {
      const key = mpnKey(row.mpn);
      // Mouser's own URLs are the ones that fail, so they never become a
      // fallback for anyone else.
      if (!key || !row.image || row.source === "mouser") continue;
      if (!photoByMpn[key]) photoByMpn[key] = row.image;
    }
  }

  function thumbHtml(row) {
    const kind = packageKind(row);
    const label = row.package
      ? `${row.mpn} — ${row.package} package`
      : `${row.mpn} — ${kind.toUpperCase()} outline`;
    const art = `<svg class="thumb__art" viewBox="0 0 48 48" aria-hidden="true">${
      PACKAGE_ART[kind] || PACKAGE_ART.ic}</svg>`;
    // The photo layers over the outline. If the CDN 404s or blocks the request,
    // try another supplier's photo of the same MPN once, then fall back to the
    // silhouette rather than leaving a broken icon.
    const fallback = photoByMpn[mpnKey(row.mpn)];
    const alt = fallback && fallback !== row.image ? fallback : "";
    const src = row.image || fallback;
    const photo = src
      ? `<img class="thumb__img" src="${esc(src)}" alt="${esc(row.mpn)}"
              loading="lazy" decoding="async" referrerpolicy="no-referrer"
              ${alt ? `data-fallback="${esc(alt)}"` : ""}
              onerror="if(this.dataset.fallback&&this.src!==this.dataset.fallback){this.src=this.dataset.fallback;}else{this.remove();}">`
      : "";
    return `<span class="thumb" data-pkg="${esc(kind)}" title="${esc(label)}">${art}${photo}</span>`;
  }

  /* ---------------------------------------------------------------- toasts */

  function toast(kind, title, body, ttl = 7000) {
    const node = document.createElement("div");
    node.className = `toast toast--${kind}`;
    node.innerHTML = `<div><strong>${esc(title)}</strong>${body ? esc(body) : ""}</div>`;
    el.toasts.appendChild(node);
    setTimeout(() => node.remove(), ttl);
  }

  /* ------------------------------------------------------------------ init */

  async function init() {
    initTheme();
    wireEvents();
    try {
      const res = await fetch("/api/meta");
      if (!res.ok) throw new Error(`meta responded ${res.status}`);
      state.meta = await res.json();
    } catch (err) {
      toast("error", "Could not reach the agent backend.", ` ${err.message}`);
      return;
    }
    populateMeta();
    renderSourceSummary();
    loadCategories();
    readUrl();
    if (state.query) {
      el.q.value = state.query;
      runSearch();
    } else {
      el.q.focus();
    }
  }

  function populateMeta() {
    const { currencies, defaultCurrency, sorts } = state.meta;
    el.currency.innerHTML = currencies
      .map((c) => `<option value="${c}"${c === defaultCurrency ? " selected" : ""}>${c}</option>`)
      .join("");
    state.currency = defaultCurrency;
    el.sort.innerHTML = sorts.map((s) => `<option value="${s.key}">${esc(s.label)}</option>`).join("");
    el.sort.value = state.sort;
    renderDrawer();
    state.vision = state.meta.vision || null;
    renderVisionPill();
  }

  function renderSourceSummary() {
    const live = state.meta.providers.filter((p) => p.enabled && p.kind !== "sample");
    const sample = state.meta.providers.some((p) => p.enabled && p.kind === "sample");
    if (live.length) {
      el.sourceDot.className = "dot dot--live";
      el.sourceSummaryText.textContent =
        `${live.length} live source${live.length === 1 ? "" : "s"}`;
    } else {
      el.sourceDot.className = "dot dot--sample";
      el.sourceSummaryText.textContent = sample ? "Sample data only" : "No sources enabled";
    }
  }

  // Today's API spend for one supplier, worded so a distributor's own figure is
  // never confused with our local count of it.
  function usageLine(key) {
    const u = (state.meta.usage && state.meta.usage.providers || [])
      .find((x) => x.key === key);
    if (!u || !u.calls) return "";
    if (u.limit == null) {
      return `<p class="quota">${numFmt.format(u.calls)} call${u.calls === 1 ? "" : "s"}
              today · this supplier does not publish a limit</p>`;
    }
    const pct = Math.min(100, Math.round((1 - u.remaining / u.limit) * 100));
    const low = u.remaining <= u.limit * 0.15;
    return `
      <p class="quota${low ? " quota--low" : ""}">
        ${numFmt.format(u.remaining)} of ${numFmt.format(u.limit)} calls left today
        ${u.reported ? "" : " (counted here, not confirmed by the supplier)"}
      </p>
      <div class="quota__bar"><span style="width:${pct}%"></span></div>`;
  }

  function renderDrawer() {
    el.drawerBody.innerHTML = state.meta.providers.map((p) => `
      <article class="provider-card">
        <div class="provider-card__head">
          <span class="dot ${p.enabled ? (p.kind === "sample" ? "dot--sample" : "dot--live") : ""}"></span>
          <span class="provider-card__name">${esc(p.label)}</span>
          <span class="provider-card__kind">${esc(p.enabled ? p.kind : "off")}</span>
        </div>
        <p>${esc(p.reason)}</p>
        ${usageLine(p.key)}
        ${p.docs ? `<p><a href="${esc(p.docs)}" target="_blank" rel="noopener noreferrer">Get a key →</a></p>` : ""}
      </article>`).join("");
  }

  /* ------------------------------------------------------ category browser */

  async function loadCategories() {
    try {
      const res = await fetch("/api/categories");
      if (!res.ok) throw new Error(`categories responded ${res.status}`);
      const data = await res.json();
      state.categories = data.categories || [];
      const lines = data.totalSupplierParts
        ? ` covering ${compact(data.totalSupplierParts)} distributor line items`
        : "";
      el.categoryCount.textContent =
        `${numFmt.format(data.totalCategories)} categories${lines}. ` +
        "Pick one to search it across every supplier.";
      renderCategories();
    } catch (err) {
      // The browser is an extra: a failure here must not block searching.
      el.browseToggle.hidden = true;
      console.warn("category tree unavailable:", err.message);
    }
  }

  /* The tree spans the full distributor catalogue — hundreds of categories —
   * so the browser shows a readable slice of each group and lets the filter do
   * the finding. Filtering matches at any depth and keeps a branch whose
   * parent matched, so typing "capacitor" gives you the whole capacitor group
   * rather than the one line that happens to contain the word. */

  const CATEGORY_PREVIEW = 8;

  // Reference-part counts and distributor line-item counts are different
  // things: one is a handful of representative parts held locally, the other
  // is how many products the supplier lists. Never add them together.
  function countLabel(node) {
    if (node.totalParts) return `${node.totalParts}`;
    if (node.supplierParts) return compact(node.supplierParts);
    return "";
  }

  function matchesFilter(node, needle) {
    if (!needle) return true;
    if (node.name.toLowerCase().includes(needle)) return true;
    return (node.children || []).some((c) => matchesFilter(c, needle));
  }

  function catItemHtml(node, sub = false) {
    const count = countLabel(node);
    return `
      <button type="button" class="cat-item${sub ? " cat-item--sub" : ""}"
              data-category="${esc(node.name)}">
        ${esc(node.name)}${count ? `<span class="n">${count}</span>` : ""}
      </button>`;
  }

  function renderCategories() {
    const needle = state.categoryFilter;
    const groups = state.categories.filter((top) => matchesFilter(top, needle));

    if (!groups.length) {
      el.categoryGrid.innerHTML = `
        <p class="categories__empty">No category matches “${esc(needle)}”.
           Search for it directly instead — the suppliers are queried on the raw
           term when nothing in the tree matches.</p>`;
      return;
    }

    el.categoryGrid.innerHTML = groups.map((top) => {
      const topMatched = !needle || top.name.toLowerCase().includes(needle);
      // A group whose own name matched shows everything under it; otherwise
      // only the branches that matched are worth listing.
      let children = (top.children || []).filter(
        (c) => topMatched || matchesFilter(c, needle));

      const expanded = needle || state.categoryOpen.has(top.id);
      const hidden = expanded ? 0 : Math.max(0, children.length - CATEGORY_PREVIEW);
      if (hidden) children = children.slice(0, CATEGORY_PREVIEW);

      const items = children.length
        ? children.map((child) => {
            const showLeaves = needle
              ? (child.children || []).filter((l) => matchesFilter(l, needle))
              : (state.categoryOpen.has(top.id) ? (child.children || []) : []);
            return catItemHtml(child) +
                   showLeaves.map((leaf) => catItemHtml(leaf, true)).join("");
          }).join("")
        : `<button type="button" class="cat-item" data-category="${esc(top.name)}">
             All ${esc(top.name.toLowerCase())}
           </button>`;

      const more = hidden
        ? `<button type="button" class="cat-more" data-expand="${esc(top.id)}">
             ${hidden} more…
           </button>`
        : "";

      const count = countLabel(top);
      return `
        <div class="cat-group">
          <button type="button" class="cat-group__title" data-category="${esc(top.name)}">
            ${esc(top.name)}${count ? `<span class="n">${count}</span>` : ""}
          </button>
          <div class="cat-list">${items}${more}</div>
        </div>`;
    }).join("");
  }

  function renderCrumb() {
    const cat = state.category;
    if (!cat) { el.crumb.hidden = true; return; }
    const path = cat.breadcrumb.map((name, i) =>
      `${i ? '<span class="crumb__sep">/</span>' : ""}<span>${esc(name)}</span>`).join(" ");
    el.crumb.hidden = false;
    el.crumb.innerHTML = `
      <span class="crumb__label">Category</span>
      <span class="crumb__path">${path}</span>
      ${cat.rewritten
        ? `<span class="crumb__note">suppliers queried for &ldquo;${esc(cat.searchTerm)}&rdquo;</span>`
        : ""}`;
  }

  /* -------------------------------------------------- photo identification */

  /* A photo goes to a vision model running locally under Ollama, which reads
   * the package markings and names the part. The picture is downscaled in the
   * browser first: a 12 MP phone shot carries no more legible detail of a chip
   * top than a 1280 px crop does, and it would cost the model minutes of
   * inference on a small GPU to look at it. */

  // 900 px, not 1280: a 4 GB card running a 3B vision model has only a few
  // hundred MB free after the weights, and the image encoder's scratch buffer
  // grows with the pixel count -- a larger photo crashes the Ollama runner
  // outright ("connection forcibly closed"). qwen2.5-VL pads anything smaller
  // than ~896 px back up, so this is about as low as is useful.
  const PHOTO_MAX_EDGE = 900;
  const PHOTO_QUALITY = 0.85;

  function renderVisionPill() {
    const v = state.vision;
    if (!v) return;
    const kind = v.enabled ? "dot--live" : v.reachable ? "dot--sample" : "";
    el.visionDot.className = `dot ${kind}`;
    el.visionPillText.textContent = v.enabled
      ? v.model
      : v.reachable ? "No vision model" : "Ollama offline";
    el.visionPill.title = v.reason || "";
    syncIdentifyBtn();
    if (!v.enabled && !state.identifying) renderVisionHelp();
  }

  function syncIdentifyBtn() {
    el.identifyBtn.disabled =
      !state.photo || state.identifying || !(state.vision && state.vision.enabled);
  }

  function renderVisionHelp() {
    const v = state.vision;
    if (!v) return;
    const pull = (v.suggest && v.suggest[0]) || "qwen2.5vl:3b";
    const fix = v.reachable
      ? `<p>Pull a model that can read images, then re-check:</p>
         <pre class="photo__cmd">ollama pull ${esc(pull)}</pre>
         ${v.suggest && v.suggest.length > 1
           ? `<p class="hint">Smaller alternatives: ${v.suggest.slice(1).map(esc).join(", ")}.
              A 4&nbsp;GB card is happiest with a 3B model.</p>` : ""}`
      : `<p>Start Ollama and make sure it is listening on
         <code>${esc(v.host || "http://127.0.0.1:11434")}</code>, then re-check.</p>`;
    el.photoOut.innerHTML = `
      <div class="photo__notice">
        <strong>Local identification is not ready</strong>
        <p>${esc(v.reason || "")}</p>
        ${fix}
        <button type="button" class="btn btn--ghost" data-recheck>Re-check Ollama</button>
      </div>`;
  }

  async function refreshVision() {
    try {
      const res = await fetch("/api/vision?refresh=1");
      if (!res.ok) throw new Error(`vision responded ${res.status}`);
      state.vision = await res.json();
      renderVisionPill();
      if (state.vision.enabled) {
        el.photoOut.innerHTML = "";
        toast("info", `Using ${state.vision.model}.`, " Drop a photo to identify a part.");
      }
    } catch (err) {
      toast("error", "Could not check the local model.", ` ${err.message}`);
    }
  }

  /* Scale the longest edge down to PHOTO_MAX_EDGE and re-encode as JPEG. */
  async function downscale(file) {
    const readAsDataUrl = () => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("could not read the file"));
      reader.readAsDataURL(file);
    });

    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
    } catch {
      // No decoder for this format in this browser — hand the original over
      // and let the server decide whether it is usable.
      return readAsDataUrl();
    }
    const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    const w = Math.max(1, Math.round(bitmap.width * scale));
    const h = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bitmap, 0, 0, w, h);
    bitmap.close?.();
    try {
      return canvas.toDataURL("image/jpeg", PHOTO_QUALITY);
    } catch {
      return readAsDataUrl();     // tainted canvas is not possible here, but be safe
    }
  }

  async function acceptFile(file) {
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      toast("warn", "That is not an image.", " Use a JPEG, PNG or WebP photo.");
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      toast("warn", "That photo is very large.", " Anything under 25 MB, please.");
      return;
    }
    let dataUrl;
    try {
      dataUrl = await downscale(file);
    } catch (err) {
      toast("error", "Could not read that photo.", ` ${err.message}`);
      return;
    }
    state.photo = {
      dataUrl,
      name: file.name || "photo",
      kb: Math.round((dataUrl.length * 3) / 4 / 1024),
    };
    el.photoPreview.src = dataUrl;
    el.photoPreview.hidden = false;
    el.dropzonePrompt.hidden = true;
    el.dropzone.classList.add("dropzone--has-image");
    el.photoClear.hidden = false;
    el.photoOut.innerHTML = "";
    el.photoTiming.hidden = true;
    syncIdentifyBtn();
    if (!(state.vision && state.vision.enabled)) renderVisionHelp();
  }

  function clearPhoto() {
    state.photo = null;
    el.photoInput.value = "";
    el.photoPreview.removeAttribute("src");
    el.photoPreview.hidden = true;
    el.dropzonePrompt.hidden = false;
    el.dropzone.classList.remove("dropzone--has-image");
    el.photoClear.hidden = true;
    el.photoOut.innerHTML = "";
    el.photoTiming.hidden = true;
    syncIdentifyBtn();
  }

  async function runIdentify() {
    if (!state.photo || state.identifying) return;
    if (!(state.vision && state.vision.enabled)) {
      renderVisionHelp();
      return;
    }
    state.identifying = true;
    syncIdentifyBtn();
    el.identifyBtn.classList.add("is-loading");
    el.photoOut.innerHTML = `
      <div class="photo__notice photo__notice--busy">
        <strong>Reading the photo with ${esc(state.vision.model)}…</strong>
        <p>Running entirely on this machine. The first run after a restart also has
           to load the model into memory, so it is the slow one.</p>
      </div>`;

    // Inference has no progress to report, so an honest elapsed clock stands in
    // for a progress bar.
    const startedAt = Date.now();
    el.photoTiming.hidden = false;
    const tick = () => {
      el.photoTiming.textContent =
        `Working… ${((Date.now() - startedAt) / 1000).toFixed(0)}s elapsed`;
    };
    tick();
    state.identifyTimer = setInterval(tick, 1000);

    try {
      const res = await fetch("/api/identify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image: state.photo.dataUrl,
          note: el.photoNote.value.trim(),
        }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        if (data && data.vision) {
          state.vision = data.vision;
          renderVisionPill();
        }
        const message = data?.error?.message || `Identification failed (${res.status}).`;
        el.photoOut.innerHTML = `
          <div class="photo__notice photo__notice--bad">
            <strong>Could not identify that photo</strong>
            <p>${esc(message)}</p>
            <button type="button" class="btn btn--ghost" data-recheck>Re-check Ollama</button>
          </div>`;
        toast("error", "Identification failed.", ` ${message}`);
        return;
      }
      renderIdentification(data);
    } catch (err) {
      el.photoOut.innerHTML = `
        <div class="photo__notice photo__notice--bad">
          <strong>Could not reach the agent backend</strong>
          <p>${esc(err.message)}</p>
        </div>`;
      toast("error", "Could not reach the agent backend.", ` ${err.message}`);
    } finally {
      clearInterval(state.identifyTimer);
      state.identifyTimer = null;
      state.identifying = false;
      el.identifyBtn.classList.remove("is-loading");
      syncIdentifyBtn();
    }
  }

  function confidenceBand(value) {
    if (value == null) return "unknown";
    if (value >= 0.75) return "high";
    if (value >= 0.45) return "medium";
    return "low";
  }

  function renderIdentification(data) {
    el.photoTiming.hidden = false;
    el.photoTiming.textContent =
      `${data.model} answered in ${(data.ms / 1000).toFixed(1)}s`;

    const list = data.candidates || [];
    state.lastCandidates = list;
    if (!list.length) {
      el.photoOut.innerHTML = `
        <div class="photo__notice photo__notice--bad">
          <strong>No component recognised in that photo</strong>
          <p>${esc(data.summary || "The model did not find an electronic part it could name.")}</p>
          <p class="hint">Fill the frame with the part, keep the printed markings in
             focus and avoid glare on the package top.</p>
        </div>`;
      return;
    }

    // What the OCR pass actually read is shown verbatim. It is the one part of
    // this the user can check against the package in their hand in a second,
    // and it is what everything downstream was reasoned from.
    const printed = (data.printed || []).length
      ? `<div class="ident__printed">
           <span>Read off the package</span>
           ${data.printed.map((line) => `<code>${esc(line)}</code>`).join("")}
         </div>`
      : "";

    el.photoOut.innerHTML = `
      <div class="ident">
        <div class="ident__head">
          <h3>${list.length === 1 ? "One candidate" : `${list.length} candidates`}
              from ${esc(data.model)}</h3>
          <p>${esc(data.summary || "Pick the one that matches the part in your hand.")}</p>
          ${printed}
        </div>
        <div class="ident__grid">
          ${list.map((c, i) => candidateHtml(c, i)).join("")}
        </div>
        <p class="ident__caveat">Read by a local model from a photograph — confirm the
           part number against the markings and the datasheet before you order.</p>
      </div>`;

    // The top candidate is priced straight away so the answer to "what is this
    // and what does it cost" arrives in one step -- but only when there is
    // something worth searching. Firing a supplier fan-out at a guess the model
    // could not stand behind buys a table of unrelated parts, which reads as
    // the agent being confidently wrong.
    const top = list[0];
    if (top.searchTerm && (top.verified === true || top.mpn)) {
      useCandidate(0, true);
    } else if (top.searchTerm) {
      toast("info", "Identified loosely from the photo.",
            " Nothing was confirmed at a supplier — pick a candidate to search it.");
    }
  }

  /* What the photo itself backs up, said only where there is something worth
   * saying. A marking match is real corroboration and is worth calling out; a
   * part number asserted over a package with nothing legible on it is worth a
   * warning. Everything in between gets no badge — a flag on every card is a
   * flag nobody reads. */
  function candidateFlagHtml(c) {
    if (!c.mpn) {
      return `<p class="cand__loose">No readable part number — searched as a
              description, so expect a family of parts rather than one.</p>`;
    }

    // Whether a distributor actually stocks the part is the strongest evidence
    // available, so it outranks anything the model said about itself.
    if (c.verified === true) {
      const where = (c.verifiedBy || []).join(", ");
      const corrected = c.correctedFrom
        ? `<p class="cand__loose">The model read this as
           <code>${esc(c.correctedFrom)}</code>; the string printed on the package is
           what checked out at the suppliers, so that is what was searched.</p>`
        : "";
      return `${corrected}<p class="cand__read">Confirmed in stock${where ? ` at ${esc(where)}` : ""}.</p>`;
    }
    if (c.verified === false) {
      return `<p class="cand__bad">No supplier lists this part number. On a small
              local model that usually means it was misread — check it against the
              package before trusting it.</p>`;
    }

    if (c.markingMatch) {
      return `<p class="cand__read">Backed by the markings read off the package.</p>`;
    }
    if (c.thinMarkings) {
      return `<p class="cand__loose">Nothing legible was read off this package, so the
              part number comes from its shape rather than from print on it. Treat it
              as a starting point.</p>`;
    }
    return "";
  }

  function candidateHtml(c, index) {
    const band = confidenceBand(c.confidence);
    const pct = c.confidence == null ? "—" : `${Math.round(c.confidence * 100)}%`;
    const facts = [
      ["Package", c.package],
      ["Markings", c.markings],
      ["Manufacturer", c.manufacturer],
      ["Type", c.type],
      ["Category", c.category ? c.category.breadcrumb.join(" › ") : ""],
    ].filter(([, v]) => v);

    return `
      <article class="cand${index === 0 ? " cand--top" : ""}" data-index="${index}">
        <div class="cand__head">
          <div class="cand__title">
            <span class="cand__name">${esc(c.mpn || c.name)}</span>
            ${c.mpn && c.name && c.name !== c.mpn
              ? `<span class="cand__sub">${esc(c.name)}</span>` : ""}
          </div>
          <span class="cand__conf" data-band="${band}" title="Model confidence">${pct}</span>
        </div>
        ${candidateFlagHtml(c)}
        ${facts.length ? `<dl class="cand__facts">${facts.map(([k, v]) => `
          <div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>` : ""}
        ${c.notes ? `<p class="cand__notes">${esc(c.notes)}</p>` : ""}
        <div class="cand__foot">
          <code class="cand__term">${esc(c.searchTerm || "nothing searchable")}</code>
          <button type="button" class="btn btn--ghost" data-candidate="${index}"
                  ${c.searchTerm ? "" : "disabled"}>
            Price this part
          </button>
        </div>
      </article>`;
  }

  function useCandidate(index, auto = false) {
    const candidate = (state.lastCandidates || [])[index];
    if (!candidate) return;
    $$(".cand", el.photoOut).forEach((n) =>
      n.classList.toggle("cand--active", n.dataset.index === String(index)));
    el.q.value = candidate.searchTerm;
    runSearch();
    if (auto) {
      toast("info", `Searching for ${candidate.mpn || candidate.searchTerm}.`,
            " Pick another candidate below if that is not the part.");
    }
  }

  function wirePhoto() {
    el.photoToggle.addEventListener("click", () => {
      const open = el.photoPanel.hidden;
      el.photoPanel.hidden = !open;
      el.photoToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        refreshVision();
        el.photoPanel.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });

    el.dropzone.addEventListener("click", () => el.photoInput.click());
    el.dropzone.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        el.photoInput.click();
      }
    });
    el.photoInput.addEventListener("change", () => acceptFile(el.photoInput.files[0]));

    ["dragenter", "dragover"].forEach((name) =>
      el.dropzone.addEventListener(name, (ev) => {
        ev.preventDefault();
        el.dropzone.classList.add("dropzone--over");
      }));
    ["dragleave", "dragend", "drop"].forEach((name) =>
      el.dropzone.addEventListener(name, () => el.dropzone.classList.remove("dropzone--over")));
    el.dropzone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      acceptFile(ev.dataTransfer?.files?.[0]);
    });

    // Paste straight from a screenshot tool, but only while the panel is open
    // so Ctrl+V in the search box is never hijacked.
    document.addEventListener("paste", (ev) => {
      if (el.photoPanel.hidden) return;
      const item = Array.from(ev.clipboardData?.items || [])
        .find((i) => i.type.startsWith("image/"));
      if (!item) return;
      ev.preventDefault();
      acceptFile(item.getAsFile());
    });

    el.identifyBtn.addEventListener("click", runIdentify);
    el.photoClear.addEventListener("click", clearPhoto);

    el.photoOut.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-recheck]")) {
        refreshVision();
        return;
      }
      const btn = ev.target.closest("[data-candidate]");
      if (btn) useCandidate(Number(btn.dataset.candidate));
    });
  }

  /* ------------------------------------------------- bill of materials */

  /* A BOM is the real job this tool exists for: fifty part numbers that each
   * need pricing at every supplier. The file is parsed on the server, the
   * extracted lines are shown for confirmation before anything is ordered
   * against them, and the pricing run streams back line by line because a
   * hundred parts is minutes of upstream calls. */

  function bomFileMeta(meta, count, truncated, maxLines) {
    const how = {
      "headers": "from the column headings",
      "local model": "the local model worked out the columns",
      "column contents": "worked out from the column contents — no usable headings",
      "line scan": "read line by line out of the PDF",
    }[meta.how] || meta.how;
    // Every part-number row in the file is accounted for here. A merge is the
    // right thing to do with a part listed twice, but doing it silently made
    // the reader look like it had dropped rows: 101 in the file, 99 on screen,
    // nothing saying where the other two went.
    const rows = meta.partRows || count;
    const bits = [`<strong>${rows}</strong> part number${rows === 1 ? "" : "s"} read from the file`,
                  esc(how)];
    if (meta.duplicateRows) {
      const dupes = (meta.duplicates || [])
        .map((d) => `${d.mpn} (${d.rows} rows)`).join(", ");
      bits.push(`<strong>${count}</strong> unique — ${meta.duplicateRows}
                 duplicate row${meta.duplicateRows === 1 ? "" : "s"} merged, quantities added:
                 <span class="mono">${esc(dupes)}</span>`);
    }
    if (!meta.quantityFound) bits.push("no quantity column found — every line defaults to 1");
    if (meta.skippedRows) bits.push(`${meta.skippedRows} row${meta.skippedRows === 1 ? "" : "s"} skipped`);
    if (truncated) bits.push(`only the first ${maxLines} are priced`);
    return bits.join(" · ");
  }

  /* A sheet built for a batch carries the same part twice over — "QTY" per
   * board and "QTY Of 5 Units" for the build — and the requirement is never
   * the one the file was written for. So the file is reduced to a per-unit
   * figure once, and the build size is a control: type 20 and every line is
   * its per-unit quantity times 20. */
  function unitsPickerHtml() {
    return `
      <label class="unitpick">
        <span>Units to build</span>
        <input type="number" id="bom-units" class="unitpick__input"
               min="1" step="1" value="${state.bomUnits}">
      </label>`;
  }

  /* How the quantity was arrived at, in the user's words rather than the
   * code's. A figure nobody can account for is a figure nobody will order
   * against, and this is the one number the whole run turns on. */
  function qtyAnalysisHtml(meta) {
    const q = (meta && meta.quantities) || null;
    if (!q || !q.note) return "";
    const how = {
      "ratios": "checked against every row of the file",
      "headers": "read from the column heading",
      "local model": "worked out by the local model",
      "as written": "taken as written",
      "single column": "one quantity column",
      "line scan": "read out of the PDF",
    }[q.how] || q.how;
    const columns = (q.columns || []).filter((c) => c.why).map((c) =>
      `<li><b>${esc(c.name)}</b> — ${c.role === "batch"
        ? `${c.units || "?"} units’ worth` : "one unit’s worth"}
        <span class="dim">(${esc(c.why)})</span></li>`).join("");
    return `
      <div class="qtynote${q.confident ? "" : " qtynote--unsure"}">
        <p><b>Quantities:</b> ${esc(q.note)} <span class="dim">${esc(how)}</span></p>
        ${columns ? `<ul>${columns}</ul>` : ""}
        ${q.confident ? "" : `<p class="dim">Worth a check before ordering — the file did
           not say plainly, so this is the agent’s reading of it.</p>`}
      </div>`;
  }

  function skippedHtml(meta) {
    const skipped = (meta && meta.skipped) || [];
    if (!skipped.length) return "";
    return `
      <div class="bomskip">
        <div class="bomskip__head">
          <strong>${skipped.length} row${skipped.length === 1 ? "" : "s"} not read as a part number</strong>
          <button type="button" class="btn btn--ghost" id="bom-addskipped">Add them anyway</button>
        </div>
        <p>Usually a footer or a note. If any of these are real part numbers, add them —
           nothing here has been thrown away.</p>
        <ul>${skipped.map((row) => `
          <li><code>${esc(row.mpn)}</code>${row.description
            ? ` <span class="dim">${esc(row.description.slice(0, 60))}</span>` : ""}</li>`).join("")}
        </ul>
      </div>`;
  }

  /* One unit's worth. A file with no quantity column at all still has to
   * price something, and one per unit is the only defensible default. */
  function perUnitOf(item) {
    const value = item.perUnit != null ? item.perUnit : item.quantity;
    return Math.max(1, parseInt(value, 10) || 1);
  }

  /* Re-derive the order quantities in place. A full re-render would take the
   * focus out of the box the user is still typing in. */
  function refreshQuantities() {
    state.bomItems.forEach((item) => { item.quantity = perUnitOf(item) * state.bomUnits; });
    $$("[data-total]").forEach((cell) => {
      const item = state.bomItems[Number(cell.dataset.total)];
      if (item) cell.textContent = numFmt.format(item.quantity);
    });
    const head = $("#bom-totalhead");
    if (head) head.textContent = `Qty for ${state.bomUnits} unit${state.bomUnits === 1 ? "" : "s"}`;
  }

  function renderBomItems() {
    const items = state.bomItems;
    if (!items.length) { el.bomOut.innerHTML = skippedHtml(state.bomMeta); return; }
    el.bomOut.innerHTML = `
      <div class="bomtable">
        <div class="bomtable__head">
          <h3>Lines read from the file</h3>
          <p>Check the per-unit quantities, set how many units you are building,
             then price them. Remove anything you do not want to source.</p>
          ${unitsPickerHtml()}
        </div>
        ${qtyAnalysisHtml(state.bomMeta)}
        ${skippedHtml(state.bomMeta)}
        <div class="tablewrap">
          <table class="grid-table">
            <thead><tr>
              <th>#</th><th>Manufacturer part number</th><th>Designators</th>
              <th>Description</th><th class="num">Per unit</th>
              <th class="num" id="bom-totalhead">Qty for ${state.bomUnits} unit${
                state.bomUnits === 1 ? "" : "s"}</th><th></th>
            </tr></thead>
            <tbody>
              ${items.map((item, i) => `
                <tr data-row="${i}">
                  <td class="dim">${item.line}</td>
                  <td class="mono">${esc(item.mpn)}${item.mergedLines > 1
                    ? `<span class="tag-merged" title="This part appeared on ${item.mergedLines} lines; the quantities were added together">${item.mergedLines} lines merged</span>`
                    : ""}</td>
                  <td class="dim">${esc(item.reference || "—")}</td>
                  <td class="dim">${esc(item.description || "—")}</td>
                  <td class="num">
                    <input type="number" min="1" step="1" class="qty-cell"
                           value="${perUnitOf(item)}"
                           data-qty="${i}" aria-label="Per-unit quantity for ${esc(item.mpn)}">
                  </td>
                  <td class="num mono" data-total="${i}">${
                    numFmt.format(perUnitOf(item) * state.bomUnits)}</td>
                  <td><button type="button" class="rowdrop" data-drop="${i}"
                              aria-label="Remove ${esc(item.mpn)}">&times;</button></td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
    el.bomRun.disabled = !items.length || state.bomRunning;
    el.bomRun.querySelector(".btn__label").textContent =
      `Price ${items.length} part${items.length === 1 ? "" : "s"}`;
  }

  async function acceptBomFile(file) {
    if (!file) return;
    if (file.size > 12 * 1024 * 1024) {
      toast("warn", "That file is very large.", " Keep it under 12 MB.");
      return;
    }
    el.bomFile.hidden = false;
    el.bomFile.innerHTML = `<span class="spinner-inline"></span> Reading ${esc(file.name)}…`;
    el.bomClear.hidden = false;

    let base64;
    try {
      base64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
        reader.onerror = () => reject(new Error("could not read the file"));
        reader.readAsDataURL(file);
      });
    } catch (err) {
      el.bomFile.innerHTML = `<span class="bad">${esc(err.message)}</span>`;
      return;
    }

    try {
      const res = await fetch("/api/bom/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: base64, name: file.name }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const message = data?.error?.message || `Could not read that file (${res.status}).`;
        el.bomFile.innerHTML = `<span class="bad">${esc(message)}</span>`;
        el.bomOut.innerHTML = "";
        state.bomItems = [];
        el.bomRun.disabled = true;
        toast("error", "Could not read that BOM.", ` ${message}`);
        return;
      }
      state.bomItems = data.items || [];
      state.bomMeta = data.meta || {};
      // The file's own build size is the starting point, because it is what
      // the person who sent the file meant. It is a default, not a fact: the
      // box above the table is what is actually ordered against.
      state.bomUnits = Math.max(1, parseInt(state.bomMeta.unitsInFile, 10) || 1);
      el.bomFile.innerHTML = `<strong>${esc(file.name)}</strong><br>${
        bomFileMeta(data.meta || {}, state.bomItems.length, data.truncated, data.maxLines)}`;
      renderBomItems();
      if (!state.bomItems.length) {
        toast("warn", "No part numbers found in that file.", "");
      }
    } catch (err) {
      el.bomFile.innerHTML = `<span class="bad">${esc(err.message)}</span>`;
      toast("error", "Could not reach the agent backend.", ` ${err.message}`);
    }
  }

  function clearBom() {
    state.bomItems = [];
    state.bomMeta = null;
    state.bomUnits = 1;
    state.bomJob = null;
    if (state.bomStream) { state.bomStream.close(); state.bomStream = null; }
    el.bomInput.value = "";
    el.bomFile.hidden = true;
    el.bomFile.innerHTML = "";
    el.bomClear.hidden = true;
    el.bomOut.innerHTML = "";
    el.bomRun.disabled = true;
    el.bomRun.querySelector(".btn__label").textContent = "Price these parts";
  }

  function bomResultsShell(total) {
    el.bomOut.innerHTML = `
      <div class="bomrun">
        <div class="bomrun__bar">
          <div class="progress"><div class="progress__fill" id="bom-progress"></div></div>
          <span class="bomrun__count" id="bom-count">0 / ${total}</span>
        </div>
        <div class="tablewrap">
          <table class="grid-table" id="bom-results">
            <thead><tr>
              <th>#</th><th>Part number</th><th class="num">Qty</th><th>Supplier</th>
              <th class="num">Stock</th><th class="num">Unit</th><th class="num">Total price</th>
              <th>Component link</th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
        <div id="bom-summary"></div>
      </div>`;
  }

  function bomLineRows(line) {
    const ccy = state.bomCurrency;
    if (!line.offers || !line.offers.length) {
      const why = line.status === "error" ? line.message : (line.message || "Not found");
      return `
        <tr data-bomline="${line.line}" class="bomrow bomrow--none">
          <td class="dim">${line.line}</td>
          <td class="mono">${esc(line.mpn)}</td>
          <td class="num">${numFmt.format(line.quantity)}</td>
          <td colspan="5" class="bad">${esc(why)}</td>
        </tr>`;
    }
    const bestId = line.best ? line.best.id : null;
    const oos = line.status === "outOfStock";
    return line.offers.map((offer, i) => {
      const isBest = offer.id === bestId;
      const short = (offer.stock || 0) < line.quantity;
      // A line you cannot buy in the quantity you asked for is the one a buyer
      // has to make a decision about, so it is marked on the row itself rather
      // than left to a tag inside one cell.
      const moq = !!offer.moqRaised;
      return `
        <tr data-bomline="${line.line}" class="bomrow${isBest ? " bomrow--best" : ""}${moq ? " bomrow--moq" : ""}${i ? " bomrow--alt" : ""}">
          <td class="dim">${i === 0 ? line.line : ""}</td>
          <td class="mono">${i === 0 ? esc(line.mpn) : ""}${line.searchedAs && i === 0
            ? `<span class="tag-asis" title="The part number as written is not in any catalogue. It was found after dropping the annotation, and searched as ${esc(line.searchedAs)}.">searched as ${esc(line.searchedAs)}</span>`
            : ""}</td>
          <td class="num">${i === 0 ? numFmt.format(line.quantity) : ""}</td>
          <td>${esc(offer.sourceLabel || "")}${oos && i === 0
            ? `<span class="tag-oos" title="${esc(line.message || "")}">out of stock</span>`
            : ""}${isBest
            ? (line.partial
                ? `<span class="tag-partial" title="${esc(partialReason(line))}">best of ${line.suppliers}</span>`
                : '<span class="tag-best">best</span>')
            : ""}${
            moq ? `<span class="tag-moq" title="${esc(moqReason(offer, line.quantity))}">${
              // Min is the least of this part the supplier will sell in ANY of
              // its packagings: cut tape at 10 beside a re-reel at 500 and a
              // full reel at 5,000 makes the answer 10. Tagging the row with
              // whichever SKU happened to win the price comparison put "min
              // 5,000" against a part element14 sells ten of. The basket this
              // row actually prices is still spelled out in the tooltip.
              offer.moqApplied
                ? `min ${numFmt.format(offer.packagingMoq ?? offer.moq)}`
                : offer.multiple
                ? `buy ×${numFmt.format(offer.multiple)}`
                : `min ${numFmt.format(offer.pricedQty)}`}</span>` : ""}</td>
          <td class="num${short ? " bad" : ""}">${offer.stock == null ? "—" : numFmt.format(offer.stock)}</td>
          <td class="num">${offer.unitPriceDisplay == null && oos
            ? '<span class="dim" title="This supplier lists the part but publishes no price while it is out of stock.">no price</span>'
            : money(offer.unitPriceDisplay, ccy)}</td>
          <td class="num">${offer.extendedPriceDisplay == null && oos
            ? `<span class="dim">${esc(offer.leadTime ? `lead ${offer.leadTime} wks` : "—")}</span>`
            : money(offer.extendedPriceDisplay, ccy)}</td>
          <td class="linkcell">${offer.url
            ? `<a href="${esc(offer.url)}" target="_blank" rel="noopener noreferrer"
                  title="${esc(offer.url)}">${esc(offer.url)}</a>`
            : "—"}</td>
        </tr>`;
    }).join("");
  }

  function partialReason(line) {
    const missing = (line.missingSuppliers || []).filter(Boolean);
    const who = missing.length ? missing.join(" and ") : "a supplier";
    return `${who} did not answer for this part, so this is the best of the `
         + `${line.suppliers} supplier${line.suppliers === 1 ? "" : "s"} that did. `
         + `A cheaper offer may exist. Search this part on its own to re-check it.`;
  }

  function moqReason(offer, wanted) {
    const buy = numFmt.format(offer.pricedQty);
    const want = numFmt.format(wanted);
    const parts = [];
    if (offer.moqApplied) {
      // The tag shows the lowest minimum across packagings; this says which
      // packaging the priced row is, when the two differ.
      parts.push(offer.packagingMoq && offer.packagingMoq < offer.moq
        ? `a minimum of ${numFmt.format(offer.packagingMoq)} on its smallest packaging, and ${numFmt.format(offer.moq)} on the one priced here (${offer.sku || "this SKU"})`
        : `a minimum order of ${numFmt.format(offer.moq)}`);
    }
    if (offer.multipleApplied) parts.push(`orders in multiples of ${numFmt.format(offer.multiple)}`);
    const why = parts.length ? parts.join(" and ") : "a supplier order constraint";
    // The price on the row is for the quantity asked for. The minimum is a
    // fact about placing the order, not about what the build costs, so it is
    // said here in full rather than folded into the figure.
    const at = offer.moqExtendedPriceDisplay != null
      ? ` Buying that many would cost ${money(offer.moqExtendedPriceDisplay, state.bomCurrency)} instead.`
      : "";
    return `The price on this row is for the ${want} the BOM calls for. This supplier has `
         + `${why}, so the basket would actually hold ${buy}.${at}`;
  }

  function renderBomSummary(stateData) {
    const t = stateData.totals || {};
    const node = $("#bom-summary");
    if (!node) return;
    const cards = [
      ["Lines priced", `${t.priced || 0} / ${t.lines || 0}`, ""],
      ["Out of stock", String(t.outOfStock || 0), t.outOfStock ? "warn" : ""],
      ["Not found", String(t.notFound || 0), t.notFound ? "warn" : ""],
      ["Short of stock", String(t.shortOfStock || 0), t.shortOfStock ? "warn" : ""],
      [`Total for ${state.bomUnits} unit${state.bomUnits === 1 ? "" : "s"}`,
       money(t.totalCost, t.currency || state.bomCurrency), "big"],
    ];
    // A minimum order quantity is the usual reason a BOM total is bigger than
    // expected, so it is called out rather than left to look like a mistake.
    // The total is for the quantity asked for, full stop. What the same basket
    // would cost at supplier minimums is a real number a buyer needs before
    // placing the order, so it is given -- named, beside the total, and never
    // mistakable for it.
    const atMin = t.moqTotalCost != null && t.totalCost != null
                  && t.moqTotalCost > t.totalCost
      ? ` Rounded up to those minimums the same basket would cost
          <b>${money(t.moqTotalCost, t.currency || state.bomCurrency)}</b>, which is not
          the figure above.`
      : "";
    const moqNote = t.moqRaised
      ? `<p class="hint">The total is for the quantity your BOM asks for.
         ${t.moqRaised} line${t.moqRaised === 1 ? " has" : "s have"} a supplier minimum order
         quantity or order multiple above that, so ${t.moqRaised === 1 ? "that basket" : "those baskets"}
         would hold more pieces than you need. Those rows are <b>highlighted</b>, and the tag on
         each shows the minimum.${atMin}</p>`
      : "";
    // A line the total could not include has to say so, or the figure reads as
    // covering the whole BOM when it does not.
    const partialNote = t.partial
      ? `<p class="hint warn">${t.partial} line${t.partial === 1 ? "" : "s"} could not be
         compared across every supplier because one did not answer, usually a distributor
         rate limit during a large run. Those rows say <b>best of N</b> instead of
         <b>best</b>. Re-run the file, or search those parts on their own, to confirm the
         cheapest offer.</p>`
      : "";
    const fxNote = t.unconverted
      ? `<p class="hint warn">${t.unconverted} priced line${t.unconverted === 1 ? "" : "s"}
         could not be converted to ${esc(t.currency || "")} and ${t.unconverted === 1 ? "is" : "are"}
         not in the total, which covers ${t.totalLines || 0} of ${t.priced || 0} priced lines.
         Check those rows at the supplier directly.</p>`
      : "";
    node.innerHTML = `
      <div class="bomsummary">
        ${cards.map(([label, value, kind]) => `
          <div class="bomsummary__cell${kind ? ` is-${kind}` : ""}">
            <span>${esc(label)}</span><b>${esc(value)}</b>
          </div>`).join("")}
        <div class="bomsummary__actions">
          <a class="btn btn--ghost" href="/api/bom/export.xlsx?job=${encodeURIComponent(state.bomJob)}"
             title="One row per part, the chosen supplier only — the sheet a buyer circulates">
            Best price quotes (Excel)
          </a>
          <a class="btn btn--ghost" href="/api/bom/export.xlsx?format=offers&job=${encodeURIComponent(state.bomJob)}"
             title="Every supplier that quoted each part, so the choice can be checked">
            All quotes (Excel)
          </a>
        </div>
      </div>
      ${moqNote}
      ${partialNote}
      ${fxNote}
      ${t.outOfStock ? `<p class="hint warn">${t.outOfStock} part${t.outOfStock === 1 ? " is" : "s are"}
        listed by a supplier but out of stock, so no price was published against
        ${t.outOfStock === 1 ? "it" : "them"}. Those rows are marked <b>out of stock</b> and keep
        their supplier link, in the exports as well — the part number is correct, it just
        cannot be bought from stock today.</p>` : ""}
      ${t.notFound ? `<p class="hint">A part number that no supplier lists is usually a typo in
        the BOM, an internal part number rather than the manufacturer's, or genuinely
        obsolete. Those lines are in the exports too, marked as not found.</p>` : ""}`;
  }

  async function runBomPricing() {
    if (!state.bomItems.length || state.bomRunning) return;

    // Take the per-unit figures as they are on screen: the user may have
    // corrected one the file got wrong, and that edit has to be what gets
    // priced. The order quantity is derived from it, never typed.
    $$("[data-qty]").forEach((input) => {
      const item = state.bomItems[Number(input.dataset.qty)];
      if (item) item.perUnit = Math.max(1, parseInt(input.value, 10) || 1);
    });
    const unitsBox = $("#bom-units");
    if (unitsBox) state.bomUnits = Math.max(1, parseInt(unitsBox.value, 10) || 1);
    refreshQuantities();

    state.bomRunning = true;
    state.bomCurrency = el.currency.value || state.currency;
    el.bomRun.disabled = true;
    el.bomRun.classList.add("is-loading");
    bomResultsShell(state.bomItems.length);

    let job;
    try {
      const start = (ignoreQuota) => fetch("/api/bom/price", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: state.bomItems, currency: state.bomCurrency,
                               units: state.bomUnits, ignoreQuota }),
      });
      let res = await start(false);
      job = await res.json();
      // Running out of quota part-way through is worse than not starting: the
      // early lines spend it and the rest come back unpriced. So the server
      // stops first and the choice to go ahead anyway is the user's.
      if (res.status === 409 && job?.error?.code === "quota") {
        const e = job.error;
        if (!confirm(`${e.message}

Price it anyway? Lines beyond the limit will be `
                   + `quoted from the other suppliers only, and may miss the cheapest offer.`)) {
          state.bomRunning = false;
          el.bomRun.disabled = false;
          el.bomRun.classList.remove("is-loading");
          toast("warn", "Pricing cancelled.", ` ${e.provider} is out of API calls for today.`);
          return;
        }
        res = await start(true);
        job = await res.json();
      }
      if (!res.ok) throw new Error(job?.error?.message || `Pricing failed (${res.status}).`);
    } catch (err) {
      state.bomRunning = false;
      el.bomRun.disabled = false;
      el.bomRun.classList.remove("is-loading");
      toast("error", "Could not start pricing.", ` ${err.message}`);
      return;
    }

    state.bomJob = job.jobId;
    const total = job.lines;
    let done = 0;

    const stream = new EventSource(`/api/bom/stream?job=${encodeURIComponent(job.jobId)}`);
    state.bomStream = stream;

    stream.addEventListener("line", (ev) => {
      const line = JSON.parse(ev.data);
      done += 1;
      const body = $("#bom-results tbody");
      if (body) body.insertAdjacentHTML("beforeend", bomLineRows(line));
      const fill = $("#bom-progress");
      if (fill) fill.style.width = `${Math.round((done / total) * 100)}%`;
      const count = $("#bom-count");
      if (count) count.textContent = `${done} / ${total}`;
    });

    // A line re-priced after the first pass. Its rows are already on screen,
    // so they are swapped in place rather than appended.
    stream.addEventListener("repair", (ev) => {
      const line = JSON.parse(ev.data);
      const body = $("#bom-results tbody");
      if (!body) return;
      const existing = body.querySelectorAll(`[data-bomline="${CSS.escape(String(line.line))}"]`);
      if (!existing.length) return;
      const anchor = existing[0];
      anchor.insertAdjacentHTML("beforebegin", bomLineRows(line));
      existing.forEach((row) => row.remove());
    });

    const finish = (payload) => {
      stream.close();
      state.bomStream = null;
      state.bomRunning = false;
      el.bomRun.disabled = false;
      el.bomRun.classList.remove("is-loading");
      if (payload) renderBomSummary(payload);
    };

    stream.addEventListener("done", (ev) => {
      const payload = JSON.parse(ev.data);
      finish(payload);
      const t = payload.totals || {};
      const tail = [];
      if (t.outOfStock) tail.push(`${t.outOfStock} out of stock`);
      if (t.notFound) tail.push(`${t.notFound} not found at any supplier`);
      toast("info", `Priced ${t.priced || 0} of ${t.lines || 0} lines.`,
            tail.length ? ` ${tail.join(", ")}.` : "");
    });

    stream.addEventListener("error", () => {
      // EventSource fires this both for a server error event and for the
      // connection closing at the end of the stream, so only treat it as a
      // failure while the run is still going.
      if (state.bomRunning) {
        finish(null);
        toast("error", "The pricing stream stopped early.",
              " Whatever finished is still exportable.");
      }
    });
  }

  function wireBom() {
    el.bomToggle.addEventListener("click", () => {
      const open = el.bomPanel.hidden;
      el.bomPanel.hidden = !open;
      el.bomToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) el.bomPanel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });

    el.bomDrop.addEventListener("click", () => el.bomInput.click());
    el.bomDrop.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); el.bomInput.click(); }
    });
    el.bomInput.addEventListener("change", () => acceptBomFile(el.bomInput.files[0]));

    ["dragenter", "dragover"].forEach((name) =>
      el.bomDrop.addEventListener(name, (ev) => {
        ev.preventDefault();
        el.bomDrop.classList.add("dropzone--over");
      }));
    ["dragleave", "dragend", "drop"].forEach((name) =>
      el.bomDrop.addEventListener(name, () => el.bomDrop.classList.remove("dropzone--over")));
    el.bomDrop.addEventListener("drop", (ev) => {
      ev.preventDefault();
      acceptBomFile(ev.dataTransfer?.files?.[0]);
    });

    el.bomRun.addEventListener("click", runBomPricing);
    el.bomClear.addEventListener("click", clearBom);

    el.bomOut.addEventListener("click", (ev) => {
      if (ev.target.closest("#bom-addskipped")) {
        const skipped = (state.bomMeta && state.bomMeta.skipped) || [];
        skipped.forEach((row) => state.bomItems.push(
          { ...row, quantities: {}, perUnit: row.quantity || 1 }));
        state.bomMeta = { ...state.bomMeta, skipped: [], skippedRows: 0 };
        state.bomItems.forEach((item, i) => { item.line = i + 1; });
        renderBomItems();
        toast("info", `Added ${skipped.length} row(s).`, " Check their quantities before pricing.");
        return;
      }
      const drop = ev.target.closest("[data-drop]");
      if (!drop) return;
      state.bomItems.splice(Number(drop.dataset.drop), 1);
      state.bomItems.forEach((item, i) => { item.line = i + 1; });
      renderBomItems();
    });

    el.bomOut.addEventListener("input", (ev) => {
      if (ev.target.id === "bom-units") {
        state.bomUnits = Math.max(1, parseInt(ev.target.value, 10) || 1);
        refreshQuantities();
        return;
      }
      const cell = ev.target.closest("[data-qty]");
      if (!cell) return;
      const item = state.bomItems[Number(cell.dataset.qty)];
      if (!item) return;
      item.perUnit = Math.max(1, parseInt(cell.value, 10) || 1);
      refreshQuantities();
    });
  }

  /* ---------------------------------------------------------------- events */

  function wireEvents() {
    wirePhoto();
    wireBom();

    el.form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      runSearch();
    });

    el.examples.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-example]");
      if (!btn) return;
      el.q.value = btn.dataset.example;
      runSearch();
    });

    el.browseToggle.addEventListener("click", () => {
      const open = el.categoryPanel.hidden;
      el.categoryPanel.hidden = !open;
      el.browseToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });

    el.categoryFilter.addEventListener("input", debounce(() => {
      state.categoryFilter = el.categoryFilter.value.trim().toLowerCase();
      renderCategories();
    }, 120));

    el.categoryGrid.addEventListener("click", (ev) => {
      const expand = ev.target.closest("[data-expand]");
      if (expand) {
        state.categoryOpen.add(expand.dataset.expand);
        renderCategories();
        return;
      }
      const btn = ev.target.closest("[data-category]");
      if (!btn) return;
      el.q.value = btn.dataset.category;
      el.categoryPanel.hidden = true;
      el.browseToggle.setAttribute("aria-expanded", "false");
      runSearch();
    });

    el.refreshBtn.addEventListener("click", () => runSearch({ fresh: true }));
    el.exportBtn.addEventListener("click", exportCsv);

    el.filter.addEventListener("input", debounce(() => {
      state.filter = el.filter.value.trim().toLowerCase();
      renderResults();
    }, 140));

    el.onlyStock.addEventListener("change", () => {
      state.onlyStock = el.onlyStock.checked;
      renderResults();
    });
    el.onlyFulfillable.addEventListener("change", () => {
      state.onlyFulfillable = el.onlyFulfillable.checked;
      renderResults();
    });
    el.sort.addEventListener("change", () => {
      state.sort = el.sort.value;
      renderResults();
      syncUrl();
    });

    $$("[data-view]").forEach((btn) => btn.addEventListener("click", () => {
      state.view = btn.dataset.view;
      $$("[data-view]").forEach((b) => b.classList.toggle("is-active", b === btn));
      renderResults();
    }));

    // Changing quantity or currency changes the quoted price break, so both
    // need a fresh round trip rather than a client-side re-render.
    el.currency.addEventListener("change", () => { if (state.results.length) runSearch(); });
    el.qty.addEventListener("change", () => { if (state.results.length) runSearch(); });

    el.sourceSummary.addEventListener("click", openDrawer);
    $$("[data-close-drawer]").forEach((n) => n.addEventListener("click", closeDrawer));

    el.themeToggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("hw-theme", next); } catch { /* private mode */ }
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !el.drawer.hidden) closeDrawer();
      if (ev.key === "/" && document.activeElement !== el.q &&
          !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        ev.preventDefault();
        el.q.focus();
        el.q.select();
      }
    });

    window.addEventListener("popstate", () => {
      readUrl();
      el.q.value = state.query;
      if (state.query) runSearch({ push: false });
    });
  }

  function openDrawer() {
    el.drawer.hidden = false;
    $("[data-close-drawer]", el.drawer).focus();
  }
  function closeDrawer() {
    el.drawer.hidden = true;
    el.sourceSummary.focus();
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  }

  /* ------------------------------------------------------------- url state */

  function readUrl() {
    const p = new URLSearchParams(location.search);
    state.query = (p.get("q") || "").trim();
    state.quantity = Math.max(1, parseInt(p.get("qty") || "100", 10) || 100);
    if (p.get("currency")) state.currency = p.get("currency").toUpperCase();
    if (p.get("sort") && SORTERS[p.get("sort")]) state.sort = p.get("sort");
    el.qty.value = state.quantity;
    if (el.currency.querySelector(`option[value="${state.currency}"]`)) {
      el.currency.value = state.currency;
    }
    el.sort.value = state.sort;
  }

  function syncUrl(push = false) {
    const p = new URLSearchParams({
      q: state.query, qty: String(state.quantity),
      currency: state.currency, sort: state.sort,
    });
    const url = `${location.pathname}?${p}`;
    if (push) history.pushState(null, "", url);
    else history.replaceState(null, "", url);
  }

  /* ---------------------------------------------------------------- search */

  function runSearch(opts = {}) {
    const query = el.q.value.trim();
    if (!query) {
      el.q.focus();
      toast("warn", "Enter something to search.", " A part number, category or description.");
      return;
    }

    if (state.stream) { state.stream.close(); state.stream = null; }
    const token = ++state.searchToken;

    // A page load or refresh always re-queries the suppliers, so what a
    // customer sees on arrival is current stock and current pricing.
    if (state.firstLoad) {
      opts = { ...opts, fresh: true };
      state.firstLoad = false;
    }

    state.query = query;
    state.quantity = Math.max(1, parseInt(el.qty.value, 10) || 1);
    state.currency = el.currency.value || "USD";
    state.results = [];
    state.summary = null;
    state.category = null;
    state.elapsedMs = null;
    state.fetchedAt = null;
    state.cached = false;
    state.loading = true;
    renderCrumb();
    el.qty.value = state.quantity;
    syncUrl(opts.push !== false);
    setLoading(true);

    state.providers = state.meta.providers.map((p) => ({
      key: p.key, label: p.label, kind: p.kind,
      state: p.enabled ? "pending" : "disabled",
      message: p.enabled ? "Querying…" : p.reason,
      count: 0,
    }));
    renderSources();
    renderSkeleton();

    const params = new URLSearchParams({
      q: state.query, qty: String(state.quantity),
      currency: state.currency, sort: state.sort,
      limit: "15",
    });
    if (opts.fresh) params.set("fresh", "1");

    let stream;
    try {
      stream = new EventSource(`/api/search/stream?${params}`);
    } catch {
      return fallbackSearch(params, token);
    }
    state.stream = stream;
    let sawAnything = false;
    let settled = false;

    stream.addEventListener("provider", (ev) => {
      if (token !== state.searchToken) return;
      sawAnything = true;
      const data = JSON.parse(ev.data);
      upsertProvider(data.provider);
      if (data.results?.length) state.results.push(...data.results);
      markBest(state.results);
      renderSources();
      renderResults();
    });

    stream.addEventListener("start", (ev) => {
      if (token !== state.searchToken) return;
      state.category = JSON.parse(ev.data).category || null;
      renderCrumb();
    });

    stream.addEventListener("done", (ev) => {
      if (token !== state.searchToken) return;
      sawAnything = true;
      const data = JSON.parse(ev.data);
      state.category = data.category || null;
      state.fetchedAt = data.fetchedAt || null;
      state.cached = Boolean(data.cached);
      state.results = data.results || [];
      state.providers = data.providers || state.providers;
      state.summary = data.summary;
      stream.close();
      state.stream = null;
      settled = true;
      finishSearch(data.elapsedMs);
    });

    stream.addEventListener("error", (ev) => {
      if (token !== state.searchToken || settled) return;
      let handled = false;
      if (ev.data) {
        try {
          toast("error", "Search failed.", ` ${JSON.parse(ev.data).message}`);
          handled = true;
        } catch { /* not a server-sent payload */ }
      }
      stream.close();
      state.stream = null;
      settled = true;
      if (!sawAnything && !handled) {
        // Connection dropped before any data — retry once over plain JSON.
        fallbackSearch(params, token);
        return;
      }
      finishSearch();
    });
  }

  async function fallbackSearch(params, token) {
    try {
      const res = await fetch(`/api/search?${params}`);
      const data = await res.json();
      if (token !== state.searchToken) return;
      if (!res.ok) {
        toast("error", "Search failed.", ` ${data?.error?.message || res.status}`);
        state.results = [];
        finishSearch();
        return;
      }
      state.results = data.results || [];
      state.providers = data.providers || [];
      state.summary = data.summary;
      state.category = data.category || null;
      state.fetchedAt = data.fetchedAt || null;
      state.cached = Boolean(data.cached);
      finishSearch(data.elapsedMs);
    } catch (err) {
      if (token !== state.searchToken) return;
      toast("error", "Could not reach the agent backend.", ` ${err.message}`);
      state.results = [];
      finishSearch();
    }
  }

  function finishSearch(elapsedMs) {
    state.loading = false;
    state.elapsedMs = elapsedMs ?? state.elapsedMs;
    setLoading(false);
    renderCrumb();
    renderSources();
    renderResults();

    const failed = state.providers.filter((p) => p.state === "error");
    failed.forEach((p) => toast("error", `${p.label} did not respond.`, ` ${p.message}`));

    const usable = state.providers.filter((p) => p.state === "ok");
    if (!usable.length && !failed.length) {
      toast("info", "No supplier returned a match.", " Try a broader term or the exact part number.");
    }
  }

  function upsertProvider(status) {
    const idx = state.providers.findIndex((p) => p.key === status.key);
    if (idx >= 0) state.providers[idx] = { ...state.providers[idx], ...status };
    else state.providers.push(status);
  }

  /** Mark the cheapest offer per part while results are still streaming in. */
  function markBest(rows) {
    const groups = new Map();
    rows.forEach((row) => {
      row.isBestPrice = false;
      row.isBestAvailable = false;
      const key = String(row.mpn || "").toLowerCase().replace(/[^a-z0-9]/g, "");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    });
    groups.forEach((group) => {
      const priced = group.filter((r) => price(r) !== Infinity);
      if (!priced.length) return;
      if (priced.length > 1) {
        priced.reduce((a, b) => (price(a) <= price(b) ? a : b)).isBestPrice = true;
        const ready = priced.filter((r) => (r.stock ?? 0) >= state.quantity);
        if (ready.length > 1) ready.reduce((a, b) => (price(a) <= price(b) ? a : b)).isBestAvailable = true;
      }
    });
  }

  function setLoading(on) {
    el.searchBtn.classList.toggle("is-loading", on);
    el.searchBtn.disabled = on;
    el.refreshBtn.disabled = on;
    el.results.setAttribute("aria-busy", on ? "true" : "false");
    $(".btn__label", el.searchBtn).textContent = on ? "Searching" : "Search";
  }

  /* ---------------------------------------------------------------- render */

  function renderSources() {
    el.sources.hidden = false;
    el.sources.innerHTML = state.providers.map((p) => {
      const kind = p.state === "ok" && p.kind === "sample" ? "sample" : p.state;
      const count = p.state === "ok" ? `<span class="source__count">${p.count}</span>` : "";
      const ms = p.ms ? ` · ${p.ms} ms` : "";
      return `<span class="source" data-state="${esc(kind)}" title="${esc(p.message || "")}${esc(ms)}">
        <span class="dot"></span>${esc(p.label)}${count}</span>`;
    }).join("");
  }

  function renderStats() {
    const elapsedMs = state.elapsedMs;
    const rows = visibleRows();
    if (!state.summary && !rows.length) { el.stats.hidden = true; return; }

    const priced = rows.filter((r) => (r.unitPriceDisplay ?? r.unitPrice) != null);
    const cheapest = priced.length ? priced.reduce((a, b) => (price(a) <= price(b) ? a : b)) : null;
    const ready = rows.filter((r) => (r.stock ?? 0) >= state.quantity);
    const parts = new Set(rows.map((r) => String(r.mpn).toLowerCase()));
    const suppliers = new Set(rows.map((r) => r.sourceLabel));

    const tiles = [
      { label: "Offers found", value: numFmt.format(rows.length),
        sub: `${parts.size} part${parts.size === 1 ? "" : "s"} · ${suppliers.size} supplier${suppliers.size === 1 ? "" : "s"}` },
      { label: "Meets required stock", value: numFmt.format(ready.length),
        sub: `of ${rows.length} for ${numFmt.format(state.quantity)} pcs` },
      { label: "Lowest unit price", accent: true,
        value: cheapest ? money(shownPrice(cheapest), state.currency) : "—",
        sub: cheapest ? esc(cheapest.sourceLabel) : "no priced offer" },
      { label: "Total price, cheapest", accent: true,
        // The cheapest unit price is routinely a reel with a minimum in the
        // thousands, so this total has to be what that basket actually costs.
        value: cheapest
          ? money((cheapest.moqRaised
              ? (cheapest.moqExtendedPriceDisplay ?? cheapest.moqExtendedPrice)
              : cheapest.extendedPriceDisplay) ?? null, state.currency)
          : "—",
        // "supplier minimum" only when the figure IS the published minimum;
        // if an order multiple rounded it further up, that number is ours.
        sub: cheapest && cheapest.moqRaised
          ? (cheapest.pricedQty === cheapest.moq
              ? `for ${numFmt.format(cheapest.moq)} pcs — supplier minimum`
              : `for ${numFmt.format(cheapest.pricedQty)} pcs — smallest order`)
          : cheapest && cheapest.pricedShort
          ? `${numFmt.format(cheapest.stock)} in stock, balance on lead time`
          : `for ${numFmt.format(state.quantity)} pcs` },
      freshnessTile(elapsedMs),
    ];

    el.stats.hidden = false;
    el.stats.innerHTML = tiles.map((t) => `
      <div class="stat${t.accent ? " stat--accent" : ""}">
        <p class="stat__label">${esc(t.label)}</p>
        <p class="stat__value">${t.value}</p>
        <p class="stat__sub">${t.sub}</p>
      </div>`).join("");
  }

  /** Shows exactly when these figures came off the suppliers' systems. */
  function freshnessTile(elapsedMs) {
    const answered = state.providers.filter((p) => p.state === "ok").length;
    if (!state.fetchedAt) {
      return { label: "Data freshness", value: "—", sub: "no source answered" };
    }
    const when = new Date(state.fetchedAt);
    const clock = when.toLocaleTimeString(undefined, { hour12: false });
    const ms = elapsedMs != null ? ` in ${numFmt.format(elapsedMs)} ms` : "";
    return {
      label: "Data freshness",
      accent: !state.cached,
      value: state.cached ? "Cached" : "Live",
      sub: `${clock} · ${answered} source${answered === 1 ? "" : "s"}${ms}`,
    };
  }

  function renderSkeleton() {
    el.toolbar.hidden = true;
    el.stats.hidden = true;
    el.results.innerHTML = `<div class="table-wrap">${
      Array.from({ length: 6 }, () => `
        <div class="skeleton-row">
          ${Array.from({ length: 6 }, () => '<div class="skeleton-bar"></div>').join("")}
        </div>`).join("")
    }</div>`;
  }

  function visibleRows() {
    let rows = state.results.slice();
    if (state.onlyStock) rows = rows.filter((r) => (r.stock ?? 0) > 0);
    if (state.onlyFulfillable) rows = rows.filter((r) => (r.stock ?? 0) >= state.quantity);
    if (state.filter) {
      const needle = state.filter;
      rows = rows.filter((r) => [
        r.mpn, r.manufacturer, r.description, r.sourceLabel, r.sku, r.package, r.category,
      ].some((v) => v && String(v).toLowerCase().includes(needle)));
    }
    rows.sort(SORTERS[state.sort] || SORTERS.price_asc);
    return rows;
  }

  function renderResults() {
    if (state.loading && !state.results.length) return;
    const rows = visibleRows();
    el.toolbar.hidden = false;
    // Index across every result, not just the visible rows, so a filtered-out
    // supplier can still lend its photo to the rows that remain.
    indexPhotos(state.results);

    if (!rows.length) {
      el.results.innerHTML = emptyHtml();
      renderStats();
      return;
    }

    const body = state.view === "grouped" ? groupedBody(rows) : rows.map(rowHtml).join("");
    el.results.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr>
            ${headCell("Component", "name_asc")}
            ${headCell("Supplier", "supplier_asc")}
            ${headCell("Available stock", "stock_desc", true)}
            <th class="num">Required stock</th>
            ${headCell(`Unit price (${state.currency})`, "price_asc", true)}
            <th class="num">Total price</th>
            <th>Link</th>
          </tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>`;

    $$("thead th.is-sortable").forEach((th) => th.addEventListener("click", () => {
      const key = th.dataset.sort;
      // Clicking the active column flips its direction where one exists.
      const flip = { price_asc: "price_desc", price_desc: "price_asc",
                     stock_desc: "stock_asc", stock_asc: "stock_desc" };
      state.sort = state.sort === key && flip[key] ? flip[key] : key;
      el.sort.value = SORTERS[state.sort] ? state.sort : "price_asc";
      renderResults();
      syncUrl();
    }));

    renderStats();
  }

  function headCell(label, sortKey, numeric = false) {
    const active = state.sort === sortKey ||
      (sortKey === "price_asc" && state.sort === "price_desc") ||
      (sortKey === "stock_desc" && state.sort === "stock_asc");
    const arrow = /_desc$/.test(state.sort) ? "▼" : "▲";
    return `<th class="is-sortable${active ? " is-sorted" : ""}${numeric ? " num" : ""}"
      data-sort="${sortKey}" title="Sort by ${esc(label)}">${esc(label)}<span class="arrow">${arrow}</span></th>`;
  }

  function groupedBody(rows) {
    const groups = new Map();
    rows.forEach((r) => {
      const key = String(r.mpn).toLowerCase();
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    return Array.from(groups.entries()).map(([, group]) => {
      const head = `<tr class="group-head"><td colspan="7">${esc(group[0].mpn)}
        <span class="count">— ${group.length} offer${group.length === 1 ? "" : "s"}${
          group[0].manufacturer ? ` · ${esc(group[0].manufacturer)}` : ""}</span></td></tr>`;
      return head + group.map(rowHtml).join("");
    }).join("");
  }

  function rowHtml(row) {
    const qty = row.requiredQty ?? state.quantity;
    // pricedQty is the smallest quantity this supplier will actually sell: the
    // required amount raised to the MOQ and rounded to the order multiple.
    const pricedQty = row.pricedQty ?? qty;
    const unit = row.unitPriceDisplay ?? row.unitPrice;
    // extendedPrice is unit x requiredQty -- deliberately, so a BOM total is
    // not inflated by every supplier's minimum. But this row labels its total
    // "for N pcs (MOQ)" whenever moqRaised, and unit x requiredQty does not buy
    // N pcs: element14 lists the same 0805 part as cut tape (MOQ 10) and as a
    // re-reel (MOQ 500) off the same price ladder, so the re-reel row quoted
    // 25 x the 500-off rate -- a fifth of what the 500-piece reel costs, for a
    // basket you cannot place. When the minimum bites, the total has to be the
    // minimum's total, which the server already carries alongside.
    const line = (row.moqRaised
      ? (row.moqExtendedPriceDisplay ?? row.moqExtendedPrice)
      : row.extendedPriceDisplay)
      ?? (unit != null ? unit * pricedQty : null);

    const badges = [
      row.isBestPrice ? '<span class="badge badge--best">Best price</span>' : "",
      row.isBestAvailable && !row.isBestPrice ? '<span class="badge badge--avail">Best in stock</span>' : "",
      row.source === "demo" ? '<span class="badge badge--sample" title="Offline reference data, not a live quote">Sample</span>' : "",
      row.priceTierMet === false ? `<span class="badge badge--tier" title="This distributor publishes no price break at ${pricedQty} pcs; showing its lowest listed tier">Indicative</span>` : "",
    ].join("");

    let stockCell;
    if (row.stock == null) {
      stockCell = '<span class="stock stock--unknown">Not published</span>';
    } else if (row.stock === 0) {
      stockCell = '<span class="stock stock--none">Out of stock</span>';
    } else if (row.stock >= qty) {
      stockCell = `<span class="stock stock--ok">${numFmt.format(row.stock)}</span>`;
    } else {
      stockCell = `<span class="stock stock--short">${numFmt.format(row.stock)}</span>
        <span class="stock__note">${numFmt.format(qty - row.stock)} short</span>`;
    }

    // A distributor quoting in its own currency is the only exact figure we
    // have. Mouser and element14 already converted their list price into the
    // storefront currency at their own rate, so converting again at the market
    // rate cannot recover what they actually charge -- it is an estimate, and
    // the row has to say so rather than presenting it as the price.
    const nativeDiffers = row.currency && row.currency !== row.displayCurrency && row.unitPrice != null;
    const convHint = nativeDiffers
      ? `Converted at today's rate. ${esc(row.sourceLabel)} charges ${esc(money(row.unitPrice, row.currency))} — that is the exact price.`
      : "";

    return `<tr>
      <td data-label="Component">
        <div class="part">
          ${thumbHtml(row)}
          <div class="part__info">
            <div class="part__name">${esc(row.mpn)}${badges}</div>
            ${row.manufacturer || row.package || row.priceUnit ? `<p class="part__meta">${
              [row.manufacturer, row.package, row.priceUnit].filter(Boolean).map(esc).join(" · ")}</p>` : ""}
            ${row.description ? `<p class="part__desc">${esc(row.description)}</p>` : ""}
          </div>
        </div>
      </td>
      <td data-label="Supplier">
        <div class="supplier">
          <span class="supplier__name">${esc(row.sourceLabel)}</span>
          ${row.sku ? `<span class="supplier__sku">${esc(row.sku)}</span>` : ""}
          ${(() => {
            // Min is the least of this part the supplier sells in any of its
            // packagings -- cut tape at 10 beside re-reel at 500 answers "Min
            // 10". Where this row's own SKU starts higher, that is said too,
            // so the figure next to the price is never in doubt.
            const low = row.packagingMoq ?? row.moq;
            if (!low || low <= 1) return "";
            const own = row.moq && row.moq > low
              ? ` title="Lowest minimum across this part's packaging options at ${esc(row.sourceLabel)}. This row's own SKU (${esc(row.sku || "")}) starts at ${numFmt.format(row.moq)}."`
              : "";
            return `<span class="supplier__sku"${own}>Min ${numFmt.format(low)}${
              row.moq && row.moq > low ? ` (this SKU ${numFmt.format(row.moq)})` : ""}</span>`;
          })()}
        </div>
      </td>
      <td class="num" data-label="Available stock">${stockCell}</td>
      <td class="num" data-label="Required stock">${numFmt.format(qty)}</td>
      <td class="num" data-label="Unit price">
        ${unit != null
          ? `<span class="price${nativeDiffers ? " price--converted" : ""}"${
                nativeDiffers ? ` title="${convHint}"` : ""}>${
              money(unit, row.displayCurrency || state.currency)}${
              nativeDiffers ? "*" : ""}</span>${
              nativeDiffers ? `<span class="price__native" title="${convHint}">${
                money(row.unitPrice, row.currency)} exact</span>` : ""}`
          : '<span class="price price--none">On request</span>'}
        ${row.priceUnit ? `<span class="price__unit" title="What one unit of this price buys">per ${esc(row.priceUnit)}</span>` : ""}
        ${row.unitsPerPrice > 1 && row.pricePerBaseUnitDisplay != null
          ? `<span class="price__unit" title="Comparable rate: this price divided by the ${numFmt.format(row.unitsPerPrice)} ${esc(row.baseUnit || "unit")}s it contains">= ${
              money(row.pricePerBaseUnitDisplay, row.displayCurrency || state.currency)} / ${esc(row.baseUnit || "unit")}</span>`
          : ""}
      </td>
      <td class="num" data-label="Total price">
        ${line != null
          ? `<span class="price">${money(line, row.displayCurrency || state.currency)}</span>${
              row.moqRaised
                ? `<span class="price__native" title="This supplier's minimum order is ${numFmt.format(row.moq)}, so the total covers ${numFmt.format(pricedQty)} pcs rather than the ${numFmt.format(qty)} requested">for ${numFmt.format(pricedQty)} pcs (MOQ)</span>`
                : row.pricedShort
                ? `<span class="price__native" title="Priced at the ${numFmt.format(pricedQty)}-piece rate the supplier quotes. Only ${numFmt.format(row.stock)} are on the shelf; the balance ships on lead time">part on lead time</span>`
                : ""}`
          : '<span class="price price--none">—</span>'}
      </td>
      <td data-label="Link">
        ${row.url
          ? `<a class="link-btn" href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">
               View<svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path d="M14 4h6v6M20 4l-8 8M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
             </a>`
          : '<span class="link-btn" aria-disabled="true">No link</span>'}
      </td>
    </tr>`;
  }

  function emptyHtml() {
    const anyRaw = state.results.length > 0;
    if (anyRaw) {
      return `<div class="empty">
        <h2>No offers match your filters</h2>
        <p>${state.results.length} offer(s) came back, but none survived the current filters.
           Try clearing the text filter or the stock toggles.</p>
      </div>`;
    }
    const errored = state.providers.filter((p) => p.state === "error");
    const live = state.providers.filter((p) => p.state === "ok" || p.state === "empty");
    return `<div class="empty">
      <h2>Nothing found for “${esc(state.query)}”</h2>
      <p>${live.length} source(s) answered and had no match${
        errored.length ? `, and ${errored.length} could not be reached` : ""}.</p>
      <ul>
        <li>Search the exact manufacturer part number, e.g. <code>STM32F103C8T6</code>.</li>
        <li>Drop qualifiers — search <code>Artix-7</code> rather than <code>Artix-7 FPGA board 100T</code>.</li>
        <li>Open <strong>Data sources</strong> in the header to see which distributors are live.</li>
      </ul>
    </div>`;
  }

  /* ---------------------------------------------------------------- export */

  function exportCsv() {
    const rows = visibleRows();
    if (!rows.length) {
      toast("warn", "Nothing to export.", " Run a search first.");
      return;
    }
    const headers = ["Component", "Manufacturer", "Description", "Supplier", "Supplier SKU",
      "Available stock", "Required stock", "Priced quantity", "Min", "SKU minimum", "Unit price",
      "Line total", "Currency",
      "Package", "Lead time", "Data source", "Product link"];
    const cell = (v) => {
      const s = v == null ? "" : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const body = rows.map((r) => {
      const unit = r.unitPriceDisplay ?? r.unitPrice;
      const qty = r.requiredQty ?? state.quantity;
      const pricedQty = r.pricedQty ?? qty;
      // Same rule as the on-screen row: the sheet has a "Priced quantity"
      // column, so "Line total" must be the cost of that many, not of the
      // requested amount priced at a tier the requested amount cannot reach.
      const line = (r.moqRaised
        ? (r.moqExtendedPriceDisplay ?? r.moqExtendedPrice)
        : r.extendedPriceDisplay)
        ?? (unit == null ? null : unit * pricedQty);
      return [r.mpn, r.manufacturer, r.description, r.sourceLabel, r.sku, r.stock, qty,
        pricedQty, r.packagingMoq ?? r.moq, r.moq,
        unit == null ? "" : unit, line == null ? "" : line.toFixed(4),
        r.displayCurrency || state.currency, r.package, r.leadTime,
        r.source === "demo" ? "sample" : "live", r.url].map(cell).join(",");
    });
    // BOM so Excel reads part numbers and currency symbols correctly.
    const blob = new Blob(["﻿" + [headers.join(","), ...body].join("\r\n")],
      { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `hardware-agent-${state.query.replace(/[^\w-]+/g, "-").slice(0, 40) || "results"}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
    toast("info", `Exported ${rows.length} row(s).`, "");
  }

  /* ----------------------------------------------------------------- theme */

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("hw-theme"); } catch { /* private mode */ }
    const prefersLight = window.matchMedia?.("(prefers-color-scheme: light)").matches;
    document.documentElement.dataset.theme = saved || (prefersLight ? "light" : "dark");
  }

  init();
})();
