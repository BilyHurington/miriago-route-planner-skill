(function () {
  "use strict";

  const data = JSON.parse(document.getElementById("preview-data").textContent);
  const visibleGroups = new Set(data.groups.map((group) => group.id));
  const groupMarkers = new Map();
  const excludedMarkers = [];
  const routeSourceData = data.routes;
  let excludedVisible = true;
  let mapReady = false;
  let basemapFallbackAttempted = false;

  document.getElementById("plan-title").textContent = data.plan.name;
  document.title = `${data.plan.name} · MiriaGo 路线预览`;
  document.getElementById("plan-meta").textContent = [
    data.plan.area,
    `${data.groups.length} 个片区`,
    `${data.excludedPoints.length} 个取舍点`,
  ].filter(Boolean).join(" · ");
  document.getElementById("group-count").textContent = `${data.groups.length} 个片区`;
  document.getElementById("generated-at").textContent = `生成：${formatDate(data.generatedAt)}`;
  document.getElementById("fingerprint").textContent = `路线：${data.integrity.routeFingerprint.slice(0, 12)}`;

  const blankStyle = {
    version: 8,
    sources: {},
    layers: [{
      id: "preview-background",
      type: "background",
      paint: { "background-color": "#eef2f3" },
    }],
  };
  const map = new maplibregl.Map({
    container: "map",
    style: data.map.noBasemap ? blankStyle : data.map.styleUrl,
    center: [0, 20],
    zoom: 2,
    attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(
    new maplibregl.AttributionControl({
      compact: true,
      customAttribution: data.map.noBasemap
        ? "MiriaGo 路线预览"
        : '<a href="https://openfreemap.org/" target="_blank" rel="noopener noreferrer">OpenFreeMap / OpenMapTiles contributors</a>',
    }),
    "bottom-right",
  );

  renderGroupList();
  renderUnlocatedPoints();

  map.on("load", () => {
    mapReady = true;
    map.addSource("preview-routes", { type: "geojson", data: filteredRoutes() });
    addRouteLayer("route-routed", "routed", ["get", "color"], 4, null);
    addRouteLayer("route-approximate", "approximate", "#64748b", 2.5, [2, 2]);
    addRouteLayer("route-skipped", "skipped", "#94a3b8", 2.5, [1, 2]);
    addRouteLayer("route-failed", "failed", "#ea580c", 3, [1, 1.5]);
    createMarkers();
    fitCoordinates(allVisibleCoordinates());
  });

  map.on("error", () => {
    if (!data.map.noBasemap) {
      const message = document.getElementById("map-message");
      message.textContent = "底图暂时无法加载，路线和点位仍可继续查看。";
      message.hidden = false;
      if (!mapReady && !basemapFallbackAttempted) {
        basemapFallbackAttempted = true;
        map.setStyle(blankStyle);
      }
    }
  });

  document.getElementById("fit-all").addEventListener("click", () => {
    fitCoordinates(allVisibleCoordinates());
  });
  document.getElementById("toggle-panel").addEventListener("click", () => {
    document.body.classList.toggle("panel-collapsed");
    window.setTimeout(() => map.resize(), 180);
  });
  document.getElementById("toggle-excluded").addEventListener("change", (event) => {
    excludedVisible = event.target.checked;
    excludedMarkers.forEach((entry) => {
      entry.element.style.display = excludedVisible ? "grid" : "none";
    });
  });

  function addRouteLayer(id, status, color, width, dasharray) {
    const paint = {
      "line-color": color,
      "line-width": width,
      "line-opacity": 0.9,
    };
    if (dasharray) {
      paint["line-dasharray"] = dasharray;
    }
    map.addLayer({
      id,
      type: "line",
      source: "preview-routes",
      filter: ["==", ["get", "status"], status],
      layout: { "line-cap": "round", "line-join": "round" },
      paint,
    });
  }

  function renderGroupList() {
    const list = document.getElementById("group-list");
    data.groups.forEach((group) => {
      const row = document.createElement("div");
      row.className = "group-row";

      const label = document.createElement("label");
      label.className = "group-toggle";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.setAttribute("aria-label", `显示 ${group.name}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          visibleGroups.add(group.id);
        } else {
          visibleGroups.delete(group.id);
        }
        updateGroupVisibility();
      });
      const swatch = document.createElement("span");
      swatch.className = "group-swatch";
      swatch.style.setProperty("--group-color", group.color);
      const copy = document.createElement("span");
      copy.className = "group-copy";
      const name = document.createElement("span");
      name.className = "group-name";
      name.textContent = group.name;
      const metrics = document.createElement("span");
      metrics.className = "group-metrics";
      metrics.textContent = [
        `${group.points.length} 个点`,
        formatDistance(group.distanceMeters),
        formatDuration(group.durationSeconds),
      ].filter(Boolean).join(" · ");
      const status = document.createElement("span");
      status.className = `group-status ${group.status}`;
      status.textContent = group.statusLabel;
      copy.append(name, metrics, status);
      label.append(checkbox, swatch, copy);

      const locate = document.createElement("button");
      locate.type = "button";
      locate.className = "locate-button";
      locate.textContent = "⌖";
      locate.title = `定位 ${group.name}`;
      locate.setAttribute("aria-label", `定位 ${group.name}`);
      locate.addEventListener("click", () => {
        fitCoordinates(groupCoordinates(group));
      });
      row.append(label, locate);
      list.append(row);
    });
  }

  function renderUnlocatedPoints() {
    if (!data.unlocatedPoints.length) return;
    const section = document.getElementById("unlocated-section");
    const list = document.getElementById("unlocated-list");
    data.unlocatedPoints.forEach((point) => {
      const item = document.createElement("li");
      item.textContent = point.name;
      list.append(item);
    });
    section.hidden = false;
  }

  function createMarkers() {
    data.groups.forEach((group) => {
      const entries = [];
      group.points.forEach((point) => {
        const element = document.createElement("button");
        element.type = "button";
        element.className = "route-marker";
        element.style.setProperty("--group-color", group.color);
        element.textContent = String(point.order);
        element.setAttribute("aria-label", `${group.name} 第 ${point.order} 站：${point.name}`);
        const marker = new maplibregl.Marker({ element, anchor: "center" })
          .setLngLat([point.longitude, point.latitude])
          .setPopup(pointPopup(point))
          .addTo(map);
        entries.push({ marker, element });
      });
      groupMarkers.set(group.id, entries);
    });

    data.excludedPoints.forEach((point) => {
      const element = document.createElement("button");
      element.type = "button";
      element.className = "excluded-marker";
      element.textContent = "×";
      element.setAttribute("aria-label", `已取舍：${point.name}`);
      const marker = new maplibregl.Marker({ element, anchor: "center" })
        .setLngLat([point.longitude, point.latitude])
        .setPopup(excludedPopup(point))
        .addTo(map);
      excludedMarkers.push({ marker, element });
    });
  }

  function pointPopup(point) {
    const root = document.createElement("div");
    root.className = "preview-popup";
    const title = document.createElement("h3");
    title.textContent = point.name;
    const detail = document.createElement("p");
    detail.textContent = `${point.groupName} · 第 ${point.order} 站`;
    root.append(title, detail);
    return new maplibregl.Popup({ offset: 18, closeButton: false }).setDOMContent(root);
  }

  function excludedPopup(point) {
    const root = document.createElement("div");
    root.className = "preview-popup";
    const title = document.createElement("h3");
    title.textContent = point.name;
    const detail = document.createElement("p");
    detail.textContent = "已取舍";
    root.append(title, detail);
    return new maplibregl.Popup({ offset: 18, closeButton: false }).setDOMContent(root);
  }

  function updateGroupVisibility() {
    groupMarkers.forEach((entries, groupId) => {
      const display = visibleGroups.has(groupId) ? "grid" : "none";
      entries.forEach((entry) => { entry.element.style.display = display; });
    });
    if (mapReady) {
      map.getSource("preview-routes").setData(filteredRoutes());
    }
  }

  function filteredRoutes() {
    return {
      type: "FeatureCollection",
      features: routeSourceData.features.filter(
        (feature) => visibleGroups.has(feature.properties.groupId),
      ),
    };
  }

  function allVisibleCoordinates() {
    const coordinates = [];
    data.groups.forEach((group) => {
      if (!visibleGroups.has(group.id)) return;
      coordinates.push(...groupCoordinates(group));
    });
    if (excludedVisible) {
      data.excludedPoints.forEach(
        (point) => coordinates.push([point.longitude, point.latitude]),
      );
    }
    return coordinates;
  }

  function groupCoordinates(group) {
    const feature = routeSourceData.features.find(
      (candidate) => candidate.properties.groupId === group.id,
    );
    if (feature) return feature.geometry.coordinates;
    return group.points.map((point) => [point.longitude, point.latitude]);
  }

  function fitCoordinates(coordinates) {
    if (!mapReady || !coordinates.length) return;
    if (coordinates.length === 1) {
      map.easeTo({ center: coordinates[0], zoom: 15, duration: 450 });
      return;
    }
    const bounds = coordinates.reduce(
      (value, coordinate) => value.extend(coordinate),
      new maplibregl.LngLatBounds(coordinates[0], coordinates[0]),
    );
    map.fitBounds(bounds, { padding: fitPadding(), maxZoom: 16, duration: 500 });
  }

  function fitPadding() {
    if (window.innerWidth > 720 || document.body.classList.contains("panel-collapsed")) {
      return 54;
    }
    const drawerHeight = document.getElementById("sidebar").getBoundingClientRect().height;
    return { top: 32, right: 32, bottom: drawerHeight + 24, left: 32 };
  }

  function formatDistance(value) {
    if (value === null || value === undefined) return null;
    if (value >= 1000) return `${(value / 1000).toFixed(1)} km`;
    return `${Math.round(value)} m`;
  }

  function formatDuration(value) {
    if (value === null || value === undefined) return null;
    return `${Math.max(1, Math.round(value / 60))} 分钟`;
  }

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { hour12: false });
  }
})();
