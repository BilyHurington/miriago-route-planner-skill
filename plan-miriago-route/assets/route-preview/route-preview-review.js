(function () {
  "use strict";

  // Review mode: reference images, external links, keep/delete decisions,
  // expandable point lists, and a keyboard-driven quick classification panel.
  // Decisions go to the local review server (serve-review) when it is running,
  // otherwise to this browser's localStorage; apply-review reads either.
  const MODEL = JSON.parse(document.getElementById("preview-data").textContent);
  const REVIEW = MODEL.review || { points: {}, excludedKinds: {}, storageKey: "miriago-review" };
  const REF = REVIEW.points;
  const KEY_DECISIONS = `${REVIEW.storageKey}:decisions`;
  const KEY_IMAGES = "miriago-review-images-on";
  const FAN_ZOOM = 16;
  const CLASS_NAMES = { keep: "建议保留", optional: "可选保留", drop: "建议丢弃" };
  const decisions = new Map();
  let deleted = new Set();
  let kept = new Set();
  let online = false;
  let packageAssets = false;
  let imagesOn = false;
  try { imagesOn = localStorage.getItem(KEY_IMAGES) === "1"; } catch (_) { /* storage blocked */ }

  const byId = new Map();
  const listItems = new Map();
  let focused = null;
  let fanGroups = [];

  const esc = (value) => String(value ?? "").replace(/[&<>"]/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]
  ));
  const $ = (id) => document.getElementById(id);

  function fullImage(id) {
    const ref = REF[id] || {};
    if (online && packageAssets && ref.fullAsset) return `/package-asset/${encodeURI(ref.fullAsset)}`;
    if (ref.remote && /^https:/.test(ref.remote)) return ref.remote;
    return ref.thumb || null;
  }

  function badge(ref) {
    const hint = ref && ref.hint;
    if (!hint || !hint.class) return "";
    return `<span class="cls ${esc(hint.class)}">${hint.famous ? "★ " : ""}${esc(CLASS_NAMES[hint.class] || hint.class)}</span>`;
  }

  function links(point) {
    const ref = REF[point.id] || {};
    const lat = point.latitude;
    const lng = point.longitude;
    const items = [];
    if (ref.anitabi && ref.anitabi.bangumiId && ref.anitabi.pointId) {
      items.push([`https://anitabi.cn/map?bangumiId=${encodeURIComponent(ref.anitabi.bangumiId)}&pid=${encodeURIComponent(ref.anitabi.pointId)}`, "anitabi"]);
    } else if (ref.sourceUrl && /^https?:/.test(ref.sourceUrl)) {
      items.push([ref.sourceUrl, "来源"]);
    }
    items.push([`https://earth.google.com/web/@${lat},${lng},40a,300d,35y,0h,60t,0r`, "Google Earth"]);
    items.push([`https://www.google.com/maps/@?api=1&map_action=map&center=${lat},${lng}&zoom=19&basemap=satellite`, "卫星图"]);
    items.push([`https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lng}`, "街景"]);
    return items;
  }

  // ---------- page chrome ----------
  function buildChrome() {
    const heading = document.querySelector(".sidebar-heading");
    const toggle = document.createElement("label");
    toggle.className = "binary-control";
    toggle.innerHTML = '<input id="toggle-ref" type="checkbox"><span>参考图</span>';
    heading.append(toggle);

    const bar = document.createElement("div");
    bar.id = "review-bar";
    bar.className = "review-bar";
    bar.innerHTML = `
      <span id="review-count">已判定：保留 0 · 删除 0</span>
      <span id="review-store" class="review-store">连接后端中…</span>
      <select id="hint-filter" title="按 AI 分类筛选列表">
        <option value="">全部点位</option><option value="keep">只看建议保留</option>
        <option value="optional">只看可选保留</option><option value="drop">只看建议丢弃</option>
        <option value="none">只看未分类</option>
      </select>
      <button id="mark-drop" type="button" title="把所有「建议丢弃」加入删除标记">标记全部建议丢弃</button>
      <button id="review-export" type="button">导出判定</button>
      <button id="review-clear" type="button">清空删除</button>`;
    heading.after(bar);

    const actions = document.querySelector(".topbar-actions");
    const quick = document.createElement("button");
    quick.id = "qc-open";
    quick.className = "qc-open";
    quick.type = "button";
    quick.title = "快速分类（快捷键 Q）";
    quick.textContent = "快速分类";
    actions.prepend(quick);

    document.body.insertAdjacentHTML("beforeend", `
      <div id="qc" role="dialog" aria-label="快速分类">
        <div class="qc-top">
          <select id="qc-group" title="片区"></select>
          <select id="qc-filter" title="筛选">
            <option value="">全部点位</option><option value="undecided">只看未判定</option>
            <option value="drop">只看 AI 建议丢弃</option><option value="optional">只看 AI 可选保留</option>
            <option value="keep">只看 AI 建议保留</option>
          </select>
          <span class="qc-prog" id="qc-prog"></span>
          <span class="qc-keys"><kbd>←</kbd><kbd>→</kbd> 翻页 · <kbd>K</kbd> 保留 · <kbd>D</kbd> 删除 · <kbd>U</kbd> 清除 · <kbd>[</kbd><kbd>]</kbd> 切换片区 · <kbd>空格</kbd> 原图 · <kbd>Esc</kbd> 退出</span>
          <button id="qc-close" type="button">退出</button>
        </div>
        <div class="qc-main">
          <div class="qc-left">
            <div class="qc-photo"><img id="qc-img" alt=""><span class="qc-stamp" id="qc-stamp"></span></div>
            <div class="qc-info" id="qc-info"></div>
          </div>
          <div id="qc-map"></div>
        </div>
        <div class="qc-strip" id="qc-strip"></div>
      </div>
      <div id="review-toast" class="review-toast"></div>
      <div id="ref-lightbox" role="dialog" aria-label="参考图原图"><img alt=""><p></p></div>`);
  }

  function toast(message, bad) {
    const element = $("review-toast");
    element.textContent = message;
    element.className = `review-toast show${bad ? " bad" : ""}`;
    clearTimeout(element.hideTimer);
    element.hideTimer = setTimeout(() => { element.className = "review-toast"; }, 1800);
  }

  function openBox(src, title) {
    if (!src) return;
    const box = $("ref-lightbox");
    box.querySelector("img").src = src;
    box.querySelector("p").textContent = title;
    box.classList.add("open");
  }

  function closeBox() {
    const box = $("ref-lightbox");
    box.classList.remove("open");
    box.querySelector("img").removeAttribute("src");
  }

  function applyImages() {
    document.body.classList.toggle("ref-on", imagesOn);
    if (imagesOn) {
      document.querySelectorAll(".pt-list.open img[data-src]:not([src]), .maplibregl-popup img[data-src]:not([src])")
        .forEach((image) => { image.src = image.dataset.src; });
    }
  }

  // ---------- decision store ----------
  function rebuildSets() {
    deleted = new Set([...decisions].filter(([, value]) => value === "delete").map(([key]) => key));
    kept = new Set([...decisions].filter(([, value]) => value === "keep").map(([key]) => key));
  }

  function saveLocal() {
    try { localStorage.setItem(KEY_DECISIONS, JSON.stringify(Object.fromEntries(decisions))); } catch (_) { /* ignore */ }
  }

  async function loadDecisions() {
    try {
      const ping = await fetch("/api/ping", { cache: "no-store" });
      if (!ping.ok) throw new Error(String(ping.status));
      packageAssets = Boolean((await ping.json()).packageAssets);
      const response = await fetch("/api/decisions", { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      online = true;
      decisions.clear();
      Object.entries(body.decisions).forEach(([key, value]) => decisions.set(key, value.decision));
      let local = {};
      try { local = JSON.parse(localStorage.getItem(KEY_DECISIONS) || "{}"); } catch (_) { local = {}; }
      const pending = Object.entries(local).filter(([key]) => REF[key] && !decisions.has(key));
      if (pending.length && confirm(`本浏览器里还有 ${pending.length} 条离线判定，导入到后端临时库？`)) {
        const changes = Object.fromEntries(pending);
        const bulk = await fetch("/api/decisions/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decisions: changes }),
        });
        if (bulk.ok) {
          pending.forEach(([key, value]) => decisions.set(key, value));
          localStorage.removeItem(KEY_DECISIONS);
        }
      }
    } catch (_) {
      online = false;
      try {
        Object.entries(JSON.parse(localStorage.getItem(KEY_DECISIONS) || "{}"))
          .forEach(([key, value]) => decisions.set(key, value));
      } catch (__) { /* ignore */ }
    }
    rebuildSets();
    paint();
    const state = $("review-store");
    state.textContent = online ? "已连接后端：判定实时写入临时库" : "未连接后端：判定仅保存在本浏览器，可导出";
    state.className = `review-store${online ? "" : " off"}`;
  }

  async function setDecisions(changes) {
    const previous = {};
    Object.keys(changes).forEach((key) => {
      previous[key] = decisions.get(key) ?? null;
      if (changes[key]) decisions.set(key, changes[key]); else decisions.delete(key);
    });
    rebuildSets();
    paint();
    window.dispatchEvent(new CustomEvent("review-decisions-changed"));
    if (!online) { saveLocal(); return true; }
    try {
      const keys = Object.keys(changes);
      const response = keys.length === 1
        ? await fetch(`/api/decisions/${encodeURIComponent(keys[0])}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: changes[keys[0]] }),
        })
        : await fetch("/api/decisions/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decisions: changes }),
        });
      if (!response.ok) throw new Error(String(response.status));
      return true;
    } catch (error) {
      Object.entries(previous).forEach(([key, value]) => {
        if (value) decisions.set(key, value); else decisions.delete(key);
      });
      rebuildSets();
      paint();
      window.dispatchEvent(new CustomEvent("review-decisions-changed"));
      toast(`保存失败，已撤回：${error.message}`, true);
      return false;
    }
  }

  const setDecision = (id, value) => setDecisions({ [id]: value });
  const toggleDelete = (id) => setDecision(id, deleted.has(id) ? null : "delete");

  function paint() {
    byId.forEach((entry, id) => {
      entry.element.classList.toggle("deleted", deleted.has(id));
      entry.element.classList.toggle("kept", kept.has(id));
    });
    listItems.forEach((item, id) => {
      item.classList.toggle("deleted", deleted.has(id));
      item.classList.toggle("kept", kept.has(id));
    });
    const count = $("review-count");
    if (count) count.textContent = `已判定：保留 ${kept.size} · 删除 ${deleted.size}`;
  }

  // ---------- popups and lists ----------
  window.__miriagoPopup = function (root, point) {
    const ref = REF[point.id] || {};
    if (ref.hint && ref.hint.class) {
      const hint = document.createElement("p");
      hint.className = "cls-why";
      hint.innerHTML = badge(ref) + esc(ref.hint.reason);
      root.append(hint);
    }
    const meta = document.createElement("p");
    meta.className = "ref-meta";
    meta.textContent = [ref.work, ref.episode].filter(Boolean).join(" · ");
    root.append(meta);
    let image = null;
    if (ref.thumb) {
      image = document.createElement("img");
      image.className = "ref-thumb";
      image.dataset.src = ref.thumb;
      image.alt = point.name;
      image.title = "点击查看原图";
      image.addEventListener("click", () => openBox(fullImage(point.id), point.name));
      root.append(image);
    } else {
      const missing = document.createElement("p");
      missing.className = "ref-missing";
      missing.textContent = "此点没有本地参考图";
      root.append(missing);
    }
    const wrap = document.createElement("div");
    wrap.className = "ref-links";
    links(point).forEach(([href, text]) => {
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      anchor.textContent = text;
      wrap.append(anchor);
    });
    const button = document.createElement("button");
    button.type = "button";
    button.className = "del";
    const sync = () => {
      const on = deleted.has(point.id);
      button.classList.toggle("on", on);
      button.textContent = on ? "已标记删除（点击恢复）" : "标记删除";
    };
    button.addEventListener("click", async () => { await toggleDelete(point.id); sync(); });
    wrap.append(button);
    root.append(wrap);
    const popup = new maplibregl.Popup({ offset: 18, closeButton: true, maxWidth: "300px" }).setDOMContent(root);
    popup.on("open", () => {
      if (imagesOn && image && !image.getAttribute("src")) image.src = ref.thumb;
      sync();
      focusList(point.id);
    });
    return popup;
  };

  function buildLists() {
    MODEL.groups.forEach((group) => {
      const row = document.querySelector(`.group-row[data-group-id="${CSS.escape(group.id)}"]`);
      if (!row) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "pt-expand";
      button.textContent = "▸";
      button.title = "展开或收起点位";
      button.addEventListener("click", (event) => { event.preventDefault(); openGroup(row); });
      row.prepend(button);
      row.classList.add("expandable");
      const list = document.createElement("ul");
      list.className = "pt-list";
      list.style.setProperty("--group-color", group.color);
      group.points.forEach((point) => {
        const ref = REF[point.id] || {};
        const item = document.createElement("li");
        item.className = "pt-item";
        item.innerHTML = `<span class="no">${point.order}</span><span class="nm"><b>${badge(ref)}${esc(point.name)}</b><small>${esc([ref.work, ref.episode].filter(Boolean).join(" · "))}</small></span><button class="x" type="button" title="标记删除或恢复">✕</button>${ref.thumb ? `<img data-src="${esc(ref.thumb)}" alt="">` : ""}`;
        item.addEventListener("click", (event) => {
          if (event.target.closest(".x")) { toggleDelete(point.id); return; }
          if (event.target.tagName === "IMG") { openBox(fullImage(point.id), point.name); return; }
          focusMap(point.id);
        });
        list.append(item);
        listItems.set(point.id, item);
      });
      row.after(list);
      const counts = { keep: 0, optional: 0, drop: 0 };
      group.points.forEach((point) => {
        const hint = (REF[point.id] || {}).hint;
        if (hint && hint.class in counts) counts[hint.class] += 1;
      });
      if (counts.keep + counts.optional + counts.drop) {
        const copy = row.querySelector(".group-copy");
        const summary = document.createElement("span");
        summary.className = "cls-count";
        summary.textContent = `保留 ${counts.keep} · 可选 ${counts.optional} · 丢弃 ${counts.drop}`;
        copy.append(summary);
      }
    });
  }

  function openGroup(row, open) {
    const button = row.querySelector(".pt-expand");
    const list = row.nextElementSibling;
    if (!button || !list || !list.classList.contains("pt-list")) return;
    const want = open === undefined ? !list.classList.contains("open") : open;
    list.classList.toggle("open", want);
    button.textContent = want ? "▾" : "▸";
    if (want && imagesOn) list.querySelectorAll("img[data-src]:not([src])").forEach((image) => { image.src = image.dataset.src; });
  }

  function setFocus(id) {
    if (focused) {
      const previous = byId.get(focused);
      if (previous) previous.element.classList.remove("focus");
      const item = listItems.get(focused);
      if (item) item.classList.remove("focus");
    }
    focused = id;
    const entry = byId.get(id);
    if (entry) entry.element.classList.add("focus");
    const item = listItems.get(id);
    if (item) item.classList.add("focus");
  }

  function focusList(id) {
    setFocus(id);
    const entry = byId.get(id);
    if (!entry) return;
    const row = document.querySelector(`.group-row[data-group-id="${CSS.escape(entry.groupId)}"]`);
    if (!row) return;
    openGroup(row, true);
    const item = listItems.get(id);
    if (item) item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function focusMap(id) {
    if (!byId.size) { window.addEventListener("preview-markers-ready", () => focusMap(id), { once: true }); return; }
    const entry = byId.get(id);
    if (!entry) return;
    const map = window.__miriagoPreview.map;
    const row = document.querySelector(`.group-row[data-group-id="${CSS.escape(entry.groupId)}"]`);
    const checkbox = row && row.querySelector('input[type="checkbox"]');
    if (checkbox && !checkbox.checked) checkbox.click();
    document.querySelectorAll(".maplibregl-popup").forEach((popup) => popup.remove());
    map.once("moveend", () => {
      applyFan();
      setTimeout(() => {
        if (!entry.marker.getPopup().isOpen()) entry.marker.togglePopup();
        setFocus(id);
      }, 60);
    });
    map.easeTo({ center: [entry.point.longitude, entry.point.latitude], zoom: Math.max(map.getZoom(), 18), duration: 500 });
  }

  // Markers sharing a coordinate spread out on a golden-angle spiral when zoomed in.
  function buildFan() {
    const buckets = new Map();
    byId.forEach((entry) => {
      const key = `${entry.point.latitude.toFixed(5)},${entry.point.longitude.toFixed(5)}`;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(entry);
    });
    fanGroups = [...buckets.values()].filter((group) => group.length > 1);
  }

  function applyFan() {
    const spread = window.__miriagoPreview.map.getZoom() >= FAN_ZOOM;
    fanGroups.forEach((group) => group.forEach((entry, index) => {
      let dx = 0;
      let dy = 0;
      if (spread) {
        const radius = 13 * Math.sqrt(index + 0.5);
        const angle = index * 2.39996;
        dx = Math.round(radius * Math.cos(angle));
        dy = Math.round(radius * Math.sin(angle));
      }
      entry.marker.setOffset([dx, dy]);
      const popup = entry.marker.getPopup();
      if (popup) popup.setOffset([dx, dy - 16]);
    }));
  }

  // ---------- quick classification ----------
  const QC = { open: false, gi: 0, list: [], i: 0, map: null, ready: false };

  function qcFilterList() {
    const group = MODEL.groups[QC.gi];
    const filter = $("qc-filter").value;
    QC.list = group.points.filter((point) => {
      const hint = (REF[point.id] || {}).hint || {};
      if (filter === "undecided") return !decisions.has(point.id);
      if (filter) return hint.class === filter;
      return true;
    });
  }

  function qcSelectGroup(index, keepIndex) {
    const count = MODEL.groups.length;
    QC.gi = ((index % count) + count) % count;
    $("qc-group").value = String(QC.gi);
    qcFilterList();
    if (!keepIndex) QC.i = 0;
    QC.i = Math.min(QC.i, Math.max(0, QC.list.length - 1));
    qcStrip();
    qcMapGroup();
    qcShow();
  }

  function qcStrip() {
    const strip = $("qc-strip");
    strip.innerHTML = "";
    if (!QC.list.length) { strip.innerHTML = '<div class="qc-empty">这个片区没有符合筛选条件的点</div>'; return; }
    QC.list.forEach((point, index) => {
      const ref = REF[point.id] || {};
      const hint = ref.hint || {};
      const card = document.createElement("div");
      card.className = `qc-th${decisions.get(point.id) ? ` ${decisions.get(point.id)}` : ""}`;
      card.dataset.k = String(index);
      card.innerHTML = `<span class="ai ${esc(hint.class || "")}"></span><span class="dec"></span>${ref.thumb ? `<img loading="lazy" src="${esc(ref.thumb)}" alt="">` : "<img alt=\"\">"}<span class="cap">${point.order}. ${esc(point.name)}</span>`;
      card.addEventListener("click", () => { QC.i = index; qcShow(); });
      strip.append(card);
    });
  }

  function qcShow() {
    const group = MODEL.groups[QC.gi];
    const point = QC.list[QC.i];
    const image = $("qc-img");
    const info = $("qc-info");
    const decided = group.points.filter((item) => decisions.has(item.id)).length;
    $("qc-prog").textContent = QC.list.length
      ? `第 ${QC.i + 1} / ${QC.list.length} 个${$("qc-filter").value ? `（片区共 ${group.points.length} 个）` : ""} · 本片区已判定 ${decided}`
      : "无点位";
    $("qc-strip").querySelectorAll(".qc-th").forEach((card) => card.classList.toggle("cur", Number(card.dataset.k) === QC.i));
    const current = $("qc-strip").querySelector(".qc-th.cur");
    if (current) current.scrollIntoView({ inline: "center", block: "nearest" });
    if (!point) {
      image.removeAttribute("src");
      info.innerHTML = "";
      $("qc-stamp").className = "qc-stamp";
      qcMapPoints();
      return;
    }
    const ref = REF[point.id] || {};
    const decision = decisions.get(point.id) || "";
    const source = fullImage(point.id);
    if (source) image.src = source; else image.removeAttribute("src");
    image.onclick = () => openBox(source, point.name);
    const next = QC.list[QC.i + 1];
    if (next && fullImage(next.id)) { const preload = new Image(); preload.src = fullImage(next.id); }
    $("qc-stamp").className = `qc-stamp ${decision}`;
    $("qc-stamp").textContent = decision === "keep" ? "保留" : decision === "delete" ? "删除" : "";
    info.innerHTML = `<h3>${point.order}. ${esc(point.name)}</h3><div class="sub">${esc(group.name)} · ${esc([ref.work, ref.episode].filter(Boolean).join(" · "))}</div>`
      + (ref.hint && ref.hint.class ? `<div class="ai">${badge(ref)}${esc(ref.hint.reason)}</div>` : '<div class="ai sub">无 AI 分类</div>')
      + `<div class="acts"><button class="keep ${decision === "keep" ? "on" : ""}" data-d="keep">保留 (K)</button><button class="delete ${decision === "delete" ? "on" : ""}" data-d="delete">删除 (D)</button><button data-d="">清除 (U)</button>`
      + links(point).map(([href, text]) => `<a target="_blank" rel="noopener noreferrer" href="${esc(href)}">${esc(text)}</a>`).join("")
      + "</div>";
    info.querySelectorAll("button[data-d]").forEach((button) => button.addEventListener("click", () => qcDecide(button.dataset.d || null, false)));
    qcMapPoints();
  }

  function qcNext(step) {
    if (!QC.list.length) return;
    const target = QC.i + step;
    if (target >= QC.list.length) { toast("本片区已到最后，按 ] 进入下一片区"); return; }
    if (target < 0) return;
    QC.i = target;
    qcShow();
  }

  async function qcDecide(value, advance) {
    const point = QC.list[QC.i];
    if (!point) return;
    if (!(await setDecision(point.id, value))) return;
    if (advance) {
      if ($("qc-filter").value === "undecided") {
        qcFilterList();
        qcStrip();
        QC.i = Math.min(QC.i, Math.max(0, QC.list.length - 1));
        if (!QC.list.length) toast("本片区已全部判定，按 ] 进入下一片区");
      } else if (QC.i < QC.list.length - 1) {
        QC.i += 1;
      } else {
        toast("本片区已到最后，按 ] 进入下一片区");
      }
    }
    qcStrip();
    qcShow();
  }

  function qcPointsGeo() {
    const group = MODEL.groups[QC.gi];
    const current = QC.list[QC.i];
    return {
      type: "FeatureCollection",
      features: group.points.map((point) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [point.longitude, point.latitude] },
        properties: {
          id: point.id,
          dec: decisions.get(point.id) || "",
          ai: ((REF[point.id] || {}).hint || {}).class || "",
          cur: current && current.id === point.id ? 1 : 0,
          inlist: QC.list.includes(point) ? 1 : 0,
        },
      })),
    };
  }

  function qcMapPoints() {
    if (!QC.ready) return;
    QC.map.getSource("qc-pts").setData(qcPointsGeo());
    const point = QC.list[QC.i];
    if (point && !QC.map.getBounds().contains([point.longitude, point.latitude])) {
      QC.map.easeTo({ center: [point.longitude, point.latitude], duration: 300 });
    }
  }

  function qcMapGroup() {
    if (!QC.ready) return;
    const group = MODEL.groups[QC.gi];
    const features = MODEL.routes.features.filter((feature) => feature.properties.groupId === group.id);
    QC.map.getSource("qc-route").setData({ type: "FeatureCollection", features });
    const bounds = new maplibregl.LngLatBounds();
    group.points.forEach((point) => bounds.extend([point.longitude, point.latitude]));
    features.forEach((feature) => feature.geometry.coordinates.forEach((coordinate) => bounds.extend(coordinate)));
    if (!bounds.isEmpty()) QC.map.fitBounds(bounds, { padding: 40, maxZoom: 18, duration: 0 });
    qcMapPoints();
  }

  function qcInitMap() {
    if (QC.map) return;
    const blank = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#e5e7eb" } }] };
    const first = MODEL.groups[0] && MODEL.groups[0].points[0];
    QC.map = new maplibregl.Map({
      container: "qc-map",
      style: MODEL.map.noBasemap ? blank : MODEL.map.styleUrl,
      center: first ? [first.longitude, first.latitude] : [0, 20],
      zoom: 15,
      attributionControl: { compact: true },
    });
    QC.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    QC.map.on("error", () => {});
    QC.map.on("load", () => {
      QC.map.addSource("qc-route", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      QC.map.addLayer({ id: "qc-route", type: "line", source: "qc-route", paint: { "line-color": ["coalesce", ["get", "color"], "#2563eb"], "line-width": 3, "line-opacity": 0.8 } });
      QC.map.addSource("qc-pts", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      QC.map.addLayer({ id: "qc-halo", type: "circle", source: "qc-pts", filter: ["==", ["get", "cur"], 1], paint: { "circle-radius": 16, "circle-color": "#38bdf8", "circle-opacity": 0.35 } });
      QC.map.addLayer({
        id: "qc-pts",
        type: "circle",
        source: "qc-pts",
        layout: { "circle-sort-key": ["get", "cur"] },
        paint: {
          "circle-radius": ["case", ["==", ["get", "cur"], 1], 9, 6],
          "circle-color": ["match", ["get", "dec"], "keep", "#16a34a", "delete", "#dc2626", "#ffffff"],
          "circle-stroke-width": ["case", ["==", ["get", "cur"], 1], 3, 2],
          "circle-stroke-color": ["case", ["==", ["get", "cur"], 1], "#0284c7", ["match", ["get", "ai"], "keep", "#16a34a", "optional", "#f59e0b", "drop", "#dc2626", "#64748b"]],
          "circle-opacity": ["case", ["==", ["get", "inlist"], 1], 1, 0.35],
        },
      });
      QC.map.on("click", "qc-pts", (event) => {
        const id = event.features[0].properties.id;
        const index = QC.list.findIndex((point) => point.id === id);
        if (index >= 0) { QC.i = index; qcShow(); } else toast("该点不在当前筛选结果中");
      });
      QC.map.on("mouseenter", "qc-pts", () => { QC.map.getCanvas().style.cursor = "pointer"; });
      QC.map.on("mouseleave", "qc-pts", () => { QC.map.getCanvas().style.cursor = ""; });
      QC.ready = true;
      qcMapGroup();
    });
  }

  function qcOpen(startId) {
    if (!MODEL.groups.length) return;
    const select = $("qc-group");
    if (!select.options.length) {
      MODEL.groups.forEach((group, index) => {
        const option = document.createElement("option");
        option.value = String(index);
        option.textContent = group.name;
        select.append(option);
      });
    }
    $("qc").classList.add("open");
    QC.open = true;
    qcInitMap();
    setTimeout(() => QC.map && QC.map.resize(), 50);
    const target = startId || focused;
    let groupIndex = QC.gi;
    if (target) {
      const found = MODEL.groups.findIndex((group) => group.points.some((point) => point.id === target));
      if (found >= 0) groupIndex = found;
    }
    qcSelectGroup(groupIndex);
    if (target) {
      const index = QC.list.findIndex((point) => point.id === target);
      if (index >= 0) { QC.i = index; qcShow(); }
    }
  }

  function qcClose() {
    $("qc").classList.remove("open");
    QC.open = false;
  }

  function bindControls() {
    $("ref-lightbox").addEventListener("click", closeBox);
    const toggle = $("toggle-ref");
    toggle.checked = imagesOn;
    applyImages();
    toggle.addEventListener("change", () => {
      imagesOn = toggle.checked;
      applyImages();
      try { localStorage.setItem(KEY_IMAGES, imagesOn ? "1" : "0"); } catch (_) { /* ignore */ }
    });
    $("hint-filter").addEventListener("change", (event) => {
      const value = event.target.value;
      listItems.forEach((item, id) => {
        const hint = (REF[id] || {}).hint;
        const cls = (hint && hint.class) || "none";
        item.classList.toggle("hide", Boolean(value) && cls !== value);
      });
      if (value) {
        document.querySelectorAll(".group-row").forEach((row) => {
          const list = row.nextElementSibling;
          if (list && list.classList.contains("pt-list") && list.querySelector(".pt-item:not(.hide)")) openGroup(row, true);
        });
      }
    });
    $("mark-drop").addEventListener("click", () => {
      const ids = Object.keys(REF).filter((id) => ((REF[id] || {}).hint || {}).class === "drop" && !deleted.has(id));
      if (!ids.length) { alert("没有新的「建议丢弃」点可标记"); return; }
      if (confirm(`把 ${ids.length} 个「建议丢弃」的点加入删除标记？之后仍可逐个恢复。`)) {
        setDecisions(Object.fromEntries(ids.map((id) => [id, "delete"])));
      }
    });
    $("review-clear").addEventListener("click", () => {
      if (deleted.size && confirm("清空所有删除标记？（保留标记不受影响）")) {
        setDecisions(Object.fromEntries([...deleted].map((id) => [id, null])));
      }
    });
    $("review-export").addEventListener("click", () => {
      const out = { exportedAt: new Date().toISOString(), decisions: Object.fromEntries(decisions) };
      const anchor = document.createElement("a");
      anchor.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: "application/json" }));
      anchor.download = "review-decisions.json";
      anchor.click();
    });
    $("qc-open").addEventListener("click", () => qcOpen());
    $("qc-close").addEventListener("click", qcClose);
    $("qc-group").addEventListener("change", (event) => qcSelectGroup(Number(event.target.value)));
    $("qc-filter").addEventListener("change", () => qcSelectGroup(QC.gi));
    window.addEventListener("review-decisions-changed", () => { if (QC.open) { qcStrip(); qcShow(); } });
    document.addEventListener("keydown", (event) => {
      if (event.target.closest && event.target.closest("input,select,textarea")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const box = $("ref-lightbox");
      if (box.classList.contains("open")) {
        if (event.key === " " || event.key === "Escape") { event.preventDefault(); closeBox(); }
        return;
      }
      if (!QC.open) {
        if (event.key === "q" || event.key === "Q") { event.preventDefault(); qcOpen(); }
        return;
      }
      const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
      const actions = {
        ArrowRight: () => qcNext(1),
        ArrowLeft: () => qcNext(-1),
        k: () => qcDecide("keep", true),
        d: () => qcDecide("delete", true),
        u: () => qcDecide(null, false),
        "]": () => qcSelectGroup(QC.gi + 1),
        PageDown: () => qcSelectGroup(QC.gi + 1),
        "[": () => qcSelectGroup(QC.gi - 1),
        PageUp: () => qcSelectGroup(QC.gi - 1),
        " ": () => { const point = QC.list[QC.i]; if (point) openBox(fullImage(point.id), point.name); },
        Escape: qcClose,
      };
      if (actions[key]) { event.preventDefault(); actions[key](); }
    });
  }

  window.addEventListener("preview-markers-ready", () => {
    const preview = window.__miriagoPreview;
    preview.groupMarkers.forEach((entries) => entries.forEach((entry) => {
      byId.set(entry.point.id, entry);
      const hint = (REF[entry.point.id] || {}).hint;
      if (hint && hint.class) entry.element.classList.add(`cls-${hint.class}`);
    }));
    buildFan();
    applyFan();
    preview.map.on("zoomend", applyFan);
    paint();
  });

  document.addEventListener("DOMContentLoaded", () => {
    buildChrome();
    buildLists();
    bindControls();
    paint();
    loadDecisions();
  });
})();
