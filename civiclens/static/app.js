/* CivicLens front end. Vanilla JS, no build step. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ dom --
  const $ = (sel) => document.querySelector(sel);
  const thread = $("#thread");
  const form = $("#askForm");
  const input = $("#questionInput");
  const sendBtn = $("#sendBtn");
  const suggBox = $("#suggestions");
  const citeCount = $("#citeCount");
  const gapCount = $("#gapCount");

  const state = {
    map: null,
    markers: [],
    layer: null,
    lastAnswer: null,
    datasets: [],
    busy: false,
    mapPoint: null,   // {lat, lon, label} from a map click
  };

  const DEFAULT_CENTER = [30.2672, -97.7431];

  // ------------------------------------------------------------ utilities --
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  /** Minimal, escape-first markdown: **bold**, `code`, line breaks. */
  function renderInline(text) {
    const frag = document.createDocumentFragment();
    const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[1] !== undefined) {
        frag.appendChild(el("strong", null, m[1]));
      } else {
        frag.appendChild(el("code", null, m[2]));
      }
      last = re.lastIndex;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }

  function scrollDown() {
    requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  }

  // ----------------------------------------------------------------- map --
  function initMap() {
    state.map = L.map("map", {
      center: DEFAULT_CENTER,
      zoom: 12,
      zoomControl: true,
      attributionControl: true,
    });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(state.map);

    // Clicking the map re-asks with that point as context. This is the
    // "drop a pin" path the refusals keep asking for.
    state.map.on("click", (e) => {
      state.mapPoint = { lat: e.latlng.lat, lon: e.latlng.lng };
      const q = state.lastAnswer && state.lastAnswer.question
        ? state.lastAnswer.question
        : "Tell me about this location";
      ask(q, { lat: e.latlng.lat, lon: e.latlng.lng, silentEcho: true });
    });
  }

  function clearLayers() {
    state.markers.forEach((m) => state.map.removeLayer(m));
    state.markers = [];
    if (state.layer) { state.map.removeLayer(state.layer); state.layer = null; }
  }

  const MARKER_STYLE = {
    query:    { color: "#4dd4ac", fillColor: "#4dd4ac", fillOpacity: 0.95, radius: 10, weight: 3 },
    permit:   { color: "#7cc4ff", fillColor: "#7cc4ff", fillOpacity: 0.7,  radius: 5,  weight: 2 },
    open:     { color: "#ff8a8a", fillColor: "#ff8a8a", fillOpacity: 0.85, radius: 6,  weight: 2 },
    closed:   { color: "#8ce99a", fillColor: "#8ce99a", fillOpacity: 0.5,  radius: 5,  weight: 1 },
  };

  function renderMap(mapData) {
    if (!state.map) return;
    clearLayers();

    if (!mapData || !mapData.center) {
      $("#mapAttribution").textContent = "No location in this answer";
      return;
    }
    const center = [mapData.center.lat, mapData.center.lon];
    state.map.setView(center, mapData.zoom || 13);

    const layers = mapData.layers || [];
    layers.forEach((lyr) => {
      if (lyr.type !== "marker" || !lyr.lat || !lyr.lon) return;
      const style = Object.assign({}, MARKER_STYLE[lyr.kind] || MARKER_STYLE.query);
      const marker = L.circleMarker([lyr.lat, lyr.lon], style);
      marker.bindPopup("<b>" + escapeHtml(lyr.label || "") + "</b>");
      marker.addTo(state.map);
      state.markers.push(marker);
    });

    // Fit to the answer's markers when there are several of them.
    if (state.markers.length > 1) {
      try {
        state.map.fitBounds(L.featureGroup(state.markers).getBounds().pad(0.15));
      } catch (err) { /* bounds unavailable - keep the centre view */ }
    }

    $("#mapAttribution").textContent =
      (mapData.label ? mapData.label + " · " : "") + layers.length + " feature" + (layers.length === 1 ? "" : "s");
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // ------------------------------------------------------------ rendering --
  function addUser(text) {
    const wrap = el("div", "msg");
    const bubble = el("div", "msg-user", text);
    wrap.appendChild(bubble);
    thread.appendChild(wrap);
    scrollDown();
  }

  function addThinking() {
    const wrap = el("div", "msg msg-bot");
    const t = el("div", "thinking");
    t.appendChild(el("span", null, "Querying the city's records"));
    const dots = el("span", "dots");
    dots.appendChild(el("i")); dots.appendChild(el("i")); dots.appendChild(el("i"));
    t.appendChild(dots);
    wrap.appendChild(t);
    wrap.appendChild(el("div", "skeleton"));
    wrap.appendChild(el("div", "skeleton"));
    wrap.appendChild(el("div", "skeleton"));
    thread.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  function confBadge(conf, label) {
    const b = el("span", "conf conf-" + (label || "low"));
    b.textContent = (label || "low") + " confidence " + Math.round((conf || 0) * 100) + "%";
    b.title = "How much of this answer is backed by retrieved records rather than inference.";
    return b;
  }

  function addBot(answer) {
    const wrap = el("div", "msg msg-bot");

    const meta = el("div", "msg-meta");
    meta.appendChild(confBadge(answer.confidence, answer.confidence_label));
    if (answer.refused) meta.appendChild(el("span", "refusal-badge", "refused"));
    if (answer.intent) {
      const i = el("span", null, "intent: " + answer.intent);
      meta.appendChild(i);
    }
    if (answer.elapsed_ms != null) meta.appendChild(el("span", null, answer.elapsed_ms + " ms"));
    wrap.appendChild(meta);

    wrap.appendChild(el("h2", "headline", answer.headline || ""));

    const sum = el("p", "summary");
    sum.appendChild(renderInline(answer.summary || ""));
    wrap.appendChild(sum);

    if ((answer.facts || []).length) {
      const chips = el("div", "chips");
      answer.facts.forEach((f) => {
        const c = el("span", "chip-fact");
        c.appendChild(el("span", "k", f.label));
        c.appendChild(el("span", "v", String(f.value)));
        if (f.note) c.title = f.note;
        chips.appendChild(c);
      });
      wrap.appendChild(chips);

      const notes = (answer.facts || []).filter((f) => f.note);
      if (notes.length) {
        const ul = el("ul", "fact-notes");
        notes.forEach((f) => {
          const li = el("li");
          li.appendChild(el("strong", null, f.label + ": "));
          li.appendChild(document.createTextNode(f.note));
          ul.appendChild(li);
        });
        wrap.appendChild(ul);
      }
    }

    if ((answer.caveats || []).length) {
      const ul = el("ul", "note-list");
      answer.caveats.forEach((t) => ul.appendChild(el("li", null, t)));
      wrap.appendChild(ul);
    }

    if ((answer.follow_ups || []).length) {
      const row = el("div", "followups");
      answer.follow_ups.forEach((q) => {
        const b = el("button", "followup", q);
        b.type = "button";
        b.addEventListener("click", () => { input.value = q; autosize(); ask(q); });
        row.appendChild(b);
      });
      wrap.appendChild(row);
    }

    thread.appendChild(wrap);
    scrollDown();
  }

  function addWelcome() {
    const w = el("div", "msg msg-bot welcome");
    w.appendChild(el("h2", null, "Ask your city. Check the receipts."));
    w.appendChild(el("p", null,
      "CivicLens answers questions about zoning, parcels, permits, 311 cases, flood zones, " +
      "council districts and neighbourhood plans using the city's own open-data records. " +
      "Every number links to the exact query that produced it, and anything it cannot verify " +
      "it refuses rather than guesses."));
    const grid = el("div", "grid");
    WELCOME.forEach((ex) => {
      const b = el("button", "welcome-card");
      b.type = "button";
      b.appendChild(el("div", "q", ex.q));
      b.appendChild(el("div", "why", ex.why));
      b.addEventListener("click", () => ask(ex.q));
      grid.appendChild(b);
    });
    w.appendChild(grid);
    thread.appendChild(w);
  }

  var WELCOME = [
    { q: "Can I build a duplex at 1507 Bouldin Ave?", why: "Reads real zoning; refuses to fake setbacks." },
    { q: "Is 1507 Bouldin Ave in a floodplain?", why: "Point-in-polygon against the FEMA layer." },
    { q: "Which council districts get the most 311 requests?", why: "Citywide aggregate with one citation." },
  ];

  // ---------------------------------------------------------- evidence UI --
  function renderEvidence(answer) {
    const cites = answer.citations || [];
    citeCount.textContent = cites.length;

    const box = $("#citations");
    box.innerHTML = "";
    if (!cites.length) {
      box.appendChild(el("p", "empty", "This answer made no data query."));
    }
    cites.forEach((c, i) => {
      const card = el("div", "cite");
      const head = el("button", "cite-head");
      head.type = "button";
      head.setAttribute("aria-expanded", "false");

      head.appendChild(el("span", "cite-caret", "▶"));
      const main = el("div", "cite-main");
      main.appendChild(el("div", "cite-title", c.title || c.dataset_name));
      main.appendChild(el("div", "cite-sub", c.dataset_name + " · id " + c.dataset_id + " · " + (c.record_id || "")));
      head.appendChild(main);
      head.appendChild(el("span", "cite-badge", c.dataset_key));

      const detail = el("div", "cite-detail");
      const entries = Object.entries(c.detail || {}).filter(([, v]) => v !== null && v !== undefined && v !== "");
      if (entries.length) {
        const dl = el("dl", "kv");
        entries.forEach(([k, v]) => {
          dl.appendChild(el("dt", null, k));
          dl.appendChild(el("dd", null, String(v)));
        });
        detail.appendChild(dl);
      }
      const links = el("div", "cite-links");
      const q = el("a", null, "Run this query ↗");
      q.href = c.query_url; q.target = "_blank"; q.rel = "noopener";
      const p = el("a", null, "Dataset page ↗");
      p.href = c.portal_page; p.target = "_blank"; p.rel = "noopener";
      links.appendChild(q); links.appendChild(p);
      detail.appendChild(links);

      head.addEventListener("click", () => {
        const open = card.classList.toggle("is-open");
        head.setAttribute("aria-expanded", open ? "true" : "false");
      });

      card.appendChild(head);
      card.appendChild(detail);
      box.appendChild(card);
      if (i === 0) setTimeout(() => head.click(), 0);
    });

    // facts tab mirrors the chips, with their notes
    const factsBox = $("#facts");
    factsBox.innerHTML = "";
    const loc = answer.location;
    if (loc) {
      const g = el("div", "limit-group");
      g.appendChild(el("h4", null, "Location resolved"));
      const ul = el("ul");
      ul.appendChild(el("li", null, loc.display + "  (via " + loc.method.replace(/_/g, " ") + ")"));
      if (loc.lat != null) ul.appendChild(el("li", null, "Point: " + loc.lat.toFixed(5) + ", " + loc.lon.toFixed(5)));
      (loc.notes || []).forEach((n) => ul.appendChild(el("li", null, n)));
      g.appendChild(ul);
      factsBox.appendChild(g);
    }
    if ((answer.facts || []).length) {
      const g = el("div", "limit-group");
      g.appendChild(el("h4", null, "Record values"));
      const dl = el("dl", "kv");
      answer.facts.forEach((f) => {
        dl.appendChild(el("dt", null, f.label));
        dl.appendChild(el("dd", null, String(f.value)));
      });
      g.appendChild(dl);
      factsBox.appendChild(g);
    }
    if ((answer.kb_refs || []).length) {
      const g = el("div", "limit-group");
      g.appendChild(el("h4", null, "Reference sources"));
      const ul = el("ul");
      answer.kb_refs.forEach((r) => {
        const li = el("li");
        const a = el("a", null, r.title || r.id);
        a.href = r.source_url; a.target = "_blank"; a.rel = "noopener";
        a.style.color = "var(--link)";
        li.appendChild(a);
        li.appendChild(document.createTextNode(" — " + (r.source_label || "")));
        ul.appendChild(li);
      });
      g.appendChild(ul);
      factsBox.appendChild(g);
    }
    if (!factsBox.childNodes.length) factsBox.appendChild(el("p", "empty", "No record values."));

    // limits tab
    const limits = (answer.data_gaps || []).length + (answer.caveats || []).length + (answer.location && answer.location.missing ? answer.location.missing.length : 0);
    gapCount.textContent = limits;
    const lim = $("#limits");
    lim.innerHTML = "";
    function group(title, items) {
      if (!items || !items.length) return;
      const g = el("div", "limit-group");
      g.appendChild(el("h4", null, title));
      const ul = el("ul");
      items.forEach((t) => ul.appendChild(el("li", null, t)));
      g.appendChild(ul);
      lim.appendChild(g);
    }
    group("Not established", answer.data_gaps);
    group("Caveats", answer.caveats);
    if (answer.location) group("Missing in the resolution step", answer.location.missing);
    if (!lim.childNodes.length) lim.appendChild(el("p", "empty", "No gaps recorded for this answer."));

    renderMap(answer.map);
  }

  function renderDatasets() {
    const box = $("#datasets");
    box.innerHTML = "";
    if (!state.datasets.length) return;
    state.datasets.forEach((d) => {
      const c = el("div", "ds");
      c.appendChild(el("div", "ds-name", d.name));
      c.appendChild(el("div", "ds-blurb", d.blurb));
      c.appendChild(el("div", "ds-id", "dataset id: " + d.id));
      const links = el("div", "cite-links");
      const a = el("a", null, "Open on data.austintexas.gov ↗");
      a.href = d.portal_page; a.target = "_blank"; a.rel = "noopener";
      links.appendChild(a);
      c.appendChild(links);
      box.appendChild(c);
    });
  }

  // --------------------------------------------------------------- asking --
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 150) + "px";
  }

  async function ask(question, opts) {
    opts = opts || {};
    const q = (question || "").trim();
    if (!q || state.busy) return;
    state.busy = true;
    sendBtn.disabled = true;

    if (!opts.silentEcho) addUser(q);
    else {
      const wrap = el("div", "msg");
      wrap.appendChild(el("div", "msg-user", "📍 " + opts.lat.toFixed(5) + ", " + opts.lon.toFixed(5)));
      thread.appendChild(wrap);
      scrollDown();
    }

    const placeholder = addThinking();
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, lat: opts.lat, lon: opts.lon }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const answer = await res.json();
      placeholder.remove();
      state.lastAnswer = answer;
      addBot(answer);
      renderEvidence(answer);
    } catch (err) {
      placeholder.remove();
      const wrap = el("div", "msg msg-bot");
      wrap.appendChild(el("div", "msg-meta")).appendChild(el("span", "refusal-badge", "error"));
      wrap.appendChild(el("h2", "headline", "Could not reach the engine"));
      wrap.appendChild(el("p", "summary", String(err)));
      thread.appendChild(wrap);
      scrollDown();
    } finally {
      state.busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  // ----------------------------------------------------------------- init --
  function initTabs() {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => {
          t.classList.remove("is-active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("is-active");
        tab.setAttribute("aria-selected", "true");
        document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("is-active"));
        const panel = document.getElementById("panel-" + tab.dataset.tab);
        if (panel) panel.classList.add("is-active");
      });
    });
  }

  function initModal() {
    const modal = $("#aboutModal");
    const open = () => { modal.hidden = false; loadHealth(); };
    const close = () => { modal.hidden = true; };
    $("#aboutBtn").addEventListener("click", open);
    $("#aboutClose").addEventListener("click", close);
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) close(); });
  }

  async function loadHealth() {
    const box = $("#healthBox");
    try {
      const h = await (await fetch("/api/health")).json();
      box.textContent =
        "status: " + h.status + "\n" +
        "version: " + h.version + "\n" +
        "city: " + h.city + "\n" +
        "datasets: " + h.datasets + "\n" +
        "upstream: " + h.upstream.domain + "\n" +
        "api calls this process: " + h.upstream.api_calls_this_process + "\n" +
        "cache entries: " + h.upstream.cache_entries;
    } catch (e) {
      box.textContent = "health check unavailable";
    }
  }

  async function boot() {
    initMap();
    initTabs();
    initModal();
    addWelcome();

    try {
      const city = await (await fetch("/api/city")).json();
      state.datasets = city.datasets || [];
      $("#cityBadge").textContent = city.name + ", " + city.state;
      $("#mapAttribution").textContent = city.name + " · " + state.datasets.length + " datasets";
      renderDatasets();
    } catch (e) { /* the app still works without the registry */ }

    try {
      const ex = await (await fetch("/api/examples")).json();
      (ex.examples || []).slice(0, 6).forEach((item) => {
        const b = el("button", "sugg", item.q);
        b.type = "button";
        b.title = item.why;
        b.addEventListener("click", () => ask(item.q));
        suggBox.appendChild(b);
      });
    } catch (e) { /* suggestions are optional */ }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value;
      input.value = "";
      autosize();
      ask(q);
    });

    input.addEventListener("input", autosize);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });

    autosize();
    input.focus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();