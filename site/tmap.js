/* tmap.js — mapa minimo con mosaicos de OpenStreetMap. Sin dependencias (~5 KB).
 * Se carga solo cuando hace falta (selector de ubicacion, seguimiento, repartidor).
 *
 *   var map = TMap.create(el, {lat, lng, zoom, centerPin, onMove: function(center, zoom){}})
 *   map.setView(lat, lng, zoom)     map.getCenter() -> {lat,lng}     map.getZoom()
 *   map.fitBounds([[lat,lng],...], paddingPx)
 *   map.setMarker(id, lat, lng, {html, cls})   map.removeMarker(id)
 *   map.setLine([[lat,lng],...], {color, width})   map.clearLine()
 *   map.resize()    map.destroy()
 *
 * Los mosaicos vienen de tile.openstreetmap.org: sirve para el volumen de un
 * negocio chico (su politica pide poco uso y atribucion, incluida abajo). Si
 * algun dia crece, cambia TMap.tileUrl por un proveedor de mosaicos propio.
 */
(function (root) {
  "use strict";

  var TS = 256, MINZ = 3, MAXZ = 18;
  var TMap = { tileUrl: "https://tile.openstreetmap.org/{z}/{x}/{y}.png" };

  function project(lat, lng, z) {
    var s = TS * Math.pow(2, z);
    var sin = Math.max(-0.9999, Math.min(0.9999, Math.sin(lat * Math.PI / 180)));
    return { x: (lng + 180) / 360 * s, y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * s };
  }
  function unproject(x, y, z) {
    var s = TS * Math.pow(2, z), n = Math.PI - 2 * Math.PI * y / s;
    return { lat: 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))), lng: x / s * 360 - 180 };
  }
  TMap.project = project;
  TMap.unproject = unproject;

  var CSS = ".tm{position:relative;overflow:hidden;background:#dfe6e3;touch-action:none;user-select:none;-webkit-user-select:none;cursor:grab}" +
    ".tm:active{cursor:grabbing}" +
    ".tm-tiles,.tm-mk{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}" +
    ".tm-tiles img{position:absolute;width:256px;height:256px;max-width:none;-webkit-user-drag:none;user-select:none}" +
    ".tm-svg{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;overflow:visible}" +
    ".tm-m{position:absolute;left:0;top:0;transform:translate(-50%,-50%);pointer-events:none}" +
    ".tm-m.tm-bottom{transform:translate(-50%,-100%)}" +
    ".tm-dot{width:18px;height:18px;border-radius:50%;background:#7ED321;border:3px solid #0B1B2B;box-shadow:0 0 0 3px rgba(126,211,33,.55)}" +
    ".tm-dest{width:26px;height:26px}" +
    ".tm-num{min-width:26px;height:26px;padding:0 4px;box-sizing:border-box;border-radius:13px;background:#0B1B2B;color:#7ED321;border:2px solid #7ED321;font:800 12px/22px -apple-system,Segoe UI,Roboto,sans-serif;text-align:center}" +
    ".tm-home{width:22px;height:22px;border-radius:6px;background:#0B1B2B;border:2px solid #7ED321;color:#7ED321;font:800 12px/18px sans-serif;text-align:center}" +
    ".tm-cpin{position:absolute;left:50%;top:50%;width:34px;height:44px;margin:-44px 0 0 -17px;pointer-events:none;filter:drop-shadow(0 3px 3px rgba(0,0,0,.4))}" +
    ".tm-ctl{position:absolute;right:8px;top:8px;display:flex;flex-direction:column;gap:6px}" +
    ".tm-ctl button{width:36px;height:36px;border-radius:10px;border:none;background:#0B1B2B;color:#7ED321;font:700 20px/1 sans-serif;box-shadow:0 2px 6px rgba(0,0,0,.35);cursor:pointer}" +
    ".tm-attr{position:absolute;right:0;bottom:0;background:rgba(255,255,255,.8);padding:1px 5px;font:10px sans-serif}" +
    ".tm-attr a{color:#333}";
  var cssDone = false;
  function injectCss() {
    if (cssDone) return;
    cssDone = true;
    var st = document.createElement("style");
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  var PIN_SVG = '<svg class="tm-cpin" viewBox="0 0 34 44"><path d="M17 43C17 43 3 27 3 16a14 14 0 0 1 28 0c0 11-14 27-14 27Z" fill="#E0554B" stroke="#fff" stroke-width="2.5"/><circle cx="17" cy="16" r="5.5" fill="#fff"/></svg>';

  TMap.create = function (el, opts) {
    opts = opts || {};
    injectCss();
    el.classList.add("tm");
    el.innerHTML = "";

    var layerTiles = document.createElement("div"); layerTiles.className = "tm-tiles";
    var svgNS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgNS, "svg"); svg.setAttribute("class", "tm-svg");
    var line = document.createElementNS(svgNS, "polyline");
    line.setAttribute("fill", "none"); line.setAttribute("stroke-linejoin", "round"); line.setAttribute("stroke-linecap", "round");
    line.style.display = "none";
    svg.appendChild(line);
    var layerMk = document.createElement("div"); layerMk.className = "tm-mk";
    el.appendChild(layerTiles); el.appendChild(svg); el.appendChild(layerMk);

    if (opts.centerPin) el.insertAdjacentHTML("beforeend", PIN_SVG);

    var ctl = document.createElement("div"); ctl.className = "tm-ctl";
    var bIn = document.createElement("button"); bIn.type = "button"; bIn.textContent = "+"; bIn.setAttribute("aria-label", "Acercar");
    var bOut = document.createElement("button"); bOut.type = "button"; bOut.textContent = "−"; bOut.setAttribute("aria-label", "Alejar");
    ctl.appendChild(bIn); ctl.appendChild(bOut); el.appendChild(ctl);
    ["pointerdown", "dblclick"].forEach(function (ev) { ctl.addEventListener(ev, function (e) { e.stopPropagation(); }); });

    var attr = document.createElement("div"); attr.className = "tm-attr";
    attr.innerHTML = '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>';
    el.appendChild(attr);

    var z = clampZ(opts.zoom == null ? 14 : opts.zoom);
    var c = project(opts.lat == null ? 21.3808 : opts.lat, opts.lng == null ? -77.9169 : opts.lng, z);
    var cx = c.x, cy = c.y;
    var tiles = {}, markers = {}, linePts = null, raf = 0, dead = false;

    function clampZ(v) { return Math.max(MINZ, Math.min(MAXZ, Math.round(v))); }
    function size() { return { w: el.clientWidth, h: el.clientHeight }; }
    function schedule() { if (!raf && !dead) raf = requestAnimationFrame(function () { raf = 0; render(); }); }
    function fireMove() { if (opts.onMove) opts.onMove(api.getCenter(), z); }

    function render() {
      var s = size(); if (!s.w || !s.h) return;
      var tlx = cx - s.w / 2, tly = cy - s.h / 2, n = Math.pow(2, z);
      var x0 = Math.floor(tlx / TS), x1 = Math.floor((tlx + s.w) / TS);
      var y0 = Math.floor(tly / TS), y1 = Math.floor((tly + s.h) / TS);
      var need = {};
      for (var ty = y0; ty <= y1; ty++) {
        if (ty < 0 || ty >= n) continue;
        for (var tx = x0; tx <= x1; tx++) {
          var wx = ((tx % n) + n) % n, key = z + "/" + wx + "/" + ty + "@" + tx;
          need[key] = true;
          var img = tiles[key];
          if (!img) {
            img = document.createElement("img");
            img.alt = ""; img.draggable = false;
            img.onerror = function () { this.style.visibility = "hidden"; };
            img.src = TMap.tileUrl.replace("{z}", z).replace("{x}", wx).replace("{y}", ty);
            layerTiles.appendChild(img);
            tiles[key] = img;
          }
          img.style.left = Math.round(tx * TS - tlx) + "px";
          img.style.top = Math.round(ty * TS - tly) + "px";
        }
      }
      for (var k in tiles) if (!need[k]) { layerTiles.removeChild(tiles[k]); delete tiles[k]; }

      for (var id in markers) {
        var m = markers[id], p = project(m.lat, m.lng, z);
        m.el.style.left = Math.round(p.x - tlx) + "px";
        m.el.style.top = Math.round(p.y - tly) + "px";
      }
      if (linePts && linePts.length > 1) {
        line.setAttribute("points", linePts.map(function (q) {
          var p = project(q[0], q[1], z); return Math.round(p.x - tlx) + "," + Math.round(p.y - tly);
        }).join(" "));
      }
    }

    function zoomAt(dz, px, py) {
      var nz = clampZ(z + dz); if (nz === z) return;
      var s = size(), f = Math.pow(2, nz - z);
      if (px == null) { px = s.w / 2; py = s.h / 2; }
      var wx = cx - s.w / 2 + px, wy = cy - s.h / 2 + py;   // punto del mundo bajo (px,py)
      cx = wx * f - px + s.w / 2; cy = wy * f - py + s.h / 2; z = nz;
      for (var k in tiles) layerTiles.removeChild(tiles[k]);
      tiles = {};
      schedule(); fireMove();
    }

    // ---- gestos: arrastrar, pellizcar, doble toque, rueda
    var ptrs = {}, pinch0 = 0;
    function count() { return Object.keys(ptrs).length; }
    function dist() { var a = Object.keys(ptrs).map(function (k) { return ptrs[k]; }); return Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y); }
    function onDown(e) {
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (count() === 2) pinch0 = dist();
    }
    function onMoveP(e) {
      var p = ptrs[e.pointerId]; if (!p) return;
      if (count() === 1) {
        cx -= e.clientX - p.x; cy -= e.clientY - p.y;
        p.x = e.clientX; p.y = e.clientY;
        schedule(); fireMove();
      } else if (count() === 2) {
        p.x = e.clientX; p.y = e.clientY;
        var d = dist(), r = d / (pinch0 || d);
        if (r > 1.5) { zoomAt(1); pinch0 = d; } else if (r < 0.66) { zoomAt(-1); pinch0 = d; }
      }
    }
    function onUp(e) { delete ptrs[e.pointerId]; pinch0 = 0; }
    var lastWheel = 0;
    function onWheel(e) {
      e.preventDefault();
      var now = Date.now(); if (now - lastWheel < 250) return; lastWheel = now;
      var r = el.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1 : -1, e.clientX - r.left, e.clientY - r.top);
    }
    function onDbl(e) { var r = el.getBoundingClientRect(); zoomAt(1, e.clientX - r.left, e.clientY - r.top); }
    el.addEventListener("pointerdown", onDown);
    el.addEventListener("pointermove", onMoveP);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onUp);
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("dblclick", onDbl);
    bIn.addEventListener("click", function () { zoomAt(1); });
    bOut.addEventListener("click", function () { zoomAt(-1); });

    var ro = null;
    if (root.ResizeObserver) { ro = new ResizeObserver(schedule); ro.observe(el); }

    var api = {
      setView: function (lat, lng, zoom) {
        if (zoom != null && clampZ(zoom) !== z) {
          z = clampZ(zoom);
          for (var k in tiles) layerTiles.removeChild(tiles[k]);
          tiles = {};
        }
        var p = project(lat, lng, z); cx = p.x; cy = p.y; schedule();
      },
      getCenter: function () { return unproject(cx, cy, z); },
      getZoom: function () { return z; },
      fitBounds: function (pts, pad) {
        pad = pad == null ? 40 : pad;
        var s = size(); if (!pts.length || !s.w || !s.h) return;
        var la = pts.map(function (p) { return p[0]; }), lo = pts.map(function (p) { return p[1]; });
        var minLa = Math.min.apply(0, la), maxLa = Math.max.apply(0, la), minLo = Math.min.apply(0, lo), maxLo = Math.max.apply(0, lo);
        var zz = 16;
        if (pts.length > 1) {
          for (zz = MAXZ; zz > MINZ; zz--) {
            var a = project(maxLa, minLo, zz), b = project(minLa, maxLo, zz);
            if (Math.abs(b.x - a.x) <= s.w - 2 * pad && Math.abs(b.y - a.y) <= s.h - 2 * pad) break;
          }
        }
        api.setView((minLa + maxLa) / 2, (minLo + maxLo) / 2, Math.min(zz, 17));
      },
      setMarker: function (id, lat, lng, o) {
        o = o || {};
        var m = markers[id];
        if (!m) {
          var d = document.createElement("div");
          layerMk.appendChild(d);
          m = markers[id] = { el: d };
        }
        m.lat = lat; m.lng = lng;
        m.el.className = "tm-m" + (o.bottom ? " tm-bottom" : "");
        m.el.innerHTML = o.html || '<div class="tm-dot"></div>';
        schedule();
      },
      removeMarker: function (id) {
        if (markers[id]) { layerMk.removeChild(markers[id].el); delete markers[id]; }
      },
      setLine: function (pts, o) {
        o = o || {};
        linePts = pts;
        line.setAttribute("stroke", o.color || "#1E6BD6");
        line.setAttribute("stroke-width", o.width || 5);
        line.setAttribute("stroke-opacity", ".85");
        line.style.display = pts && pts.length > 1 ? "" : "none";
        schedule();
      },
      clearLine: function () { linePts = null; line.style.display = "none"; },
      resize: function () { schedule(); },
      destroy: function () {
        dead = true; if (ro) ro.disconnect();
        el.removeEventListener("pointerdown", onDown); el.removeEventListener("pointermove", onMoveP);
        el.removeEventListener("pointerup", onUp); el.removeEventListener("pointercancel", onUp);
        el.removeEventListener("wheel", onWheel); el.removeEventListener("dblclick", onDbl);
        el.innerHTML = ""; el.classList.remove("tm");
      }
    };
    schedule();
    return api;
  };

  root.TMap = TMap;
  if (typeof module !== "undefined") module.exports = TMap;
})(typeof window !== "undefined" ? window : this);
