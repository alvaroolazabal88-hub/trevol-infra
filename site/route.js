/* route.js — orden de paradas y tiempos para el repartidor (bicicleta). Sin dependencias.
 *
 * Usa el servidor publico de OSRM (perfil bici, datos de OpenStreetMap). Si no
 * responde en 6 s o falla, cae a una estimacion por distancia en linea recta
 * (x1.35 por las curvas de las calles, a 12 km/h): NUNCA se queda sin ruta,
 * solo marca source="estimate" para que la pantalla diga "aprox".
 * Los servidores publicos no tienen garantia de servicio; si algun dia se
 * necesita mas fiabilidad, se cambia Route.base por un OSRM propio.
 *
 *   Route.plan(start, stops, {order?}) -> Promise<{order, etas, legs, geometry, totalSec, source}>
 *       start = {lat,lng}; stops = [{lat,lng}, ...]
 *       order  = indices de stops en el orden a seguir (si se omite, se calcula el optimo)
 *       etas[i] = segundos hasta LLEGAR a la parada order[i] (incluye DWELL entre paradas)
 *       legs[i] = segundos del tramo anterior->parada order[i];  dist[i] = metros de ese tramo
 */
(function (root) {
  "use strict";

  var Route = {
    base: "https://routing.openstreetmap.de/routed-bike",
    bikeKmh: 12,
    detour: 1.35,
    dwellSec: 120,        // tiempo entregando en cada puerta antes de seguir
    timeoutMs: 6000,
    fetch: null           // inyectable para pruebas; por defecto window.fetch
  };

  function haversineKm(a, b) {
    var R = 6371.0088, rad = Math.PI / 180;
    var dp = (b.lat - a.lat) * rad, dl = (b.lng - a.lng) * rad;
    var x = Math.sin(dp / 2) * Math.sin(dp / 2) +
      Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.sqrt(x));
  }
  function estSec(a, b) { return haversineKm(a, b) * Route.detour / Route.bikeKmh * 3600; }
  function estM(a, b) { return haversineKm(a, b) * 1000 * Route.detour; }

  function coordStr(pts) {
    return pts.map(function (p) { return p.lng.toFixed(6) + "," + p.lat.toFixed(6); }).join(";");
  }

  function getJson(url) {
    var f = Route.fetch || root.fetch.bind(root);
    var ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, Route.timeoutMs);
    var race = new Promise(function (_, rej) { setTimeout(function () { rej(new Error("timeout")); }, Route.timeoutMs + 200); });
    var req = f(url, ctl ? { signal: ctl.signal } : undefined).then(function (r) {
      if (!r.ok) throw new Error("http " + r.status);
      return r.json();
    });
    return Promise.race([req, race]).then(
      function (v) { clearTimeout(timer); return v; },
      function (e) { clearTimeout(timer); throw e; });
  }

  // Matriz de tiempos (segundos) entre todos los puntos. -> {m, source}
  function matrix(points) {
    var n = points.length;
    function fallback() {
      var m = [];
      for (var i = 0; i < n; i++) { m.push([]); for (var j = 0; j < n; j++) m[i].push(i === j ? 0 : estSec(points[i], points[j])); }
      return { m: m, source: "estimate" };
    }
    if (n < 2 || n > 20) return Promise.resolve(fallback());
    return getJson(Route.base + "/table/v1/driving/" + coordStr(points) + "?annotations=duration").then(function (j) {
      if (!j || j.code !== "Ok" || !j.durations || j.durations.length !== n) throw new Error("bad table");
      var m = j.durations.map(function (row, i) {
        return row.map(function (v, k) { return (typeof v === "number" && isFinite(v)) ? v : estSec(points[i], points[k]); });
      });
      return { m: m, source: "osrm" };
    }).catch(fallback);
  }

  // Mejor orden para visitar todas las paradas empezando en el nodo 0 (sin volver).
  // m: matriz (n+1)x(n+1); nodo 0 = inicio; devuelve indices 1..n en orden.
  function solveOrder(m, n) {
    if (n <= 0) return [];
    if (n === 1) return [1];
    if (n > 12) return heuristic(m, n);
    var N = 1 << n, INF = 1e18, dp = [], par = [], i, j, k;
    for (i = 0; i < N; i++) { dp.push(new Float64Array(n).fill(INF)); par.push(new Int8Array(n).fill(-1)); }
    for (j = 0; j < n; j++) dp[1 << j][j] = m[0][j + 1];
    for (var mask = 1; mask < N; mask++) {
      for (j = 0; j < n; j++) {
        if (!(mask & (1 << j))) continue;
        var cur = dp[mask][j]; if (cur >= INF) continue;
        for (k = 0; k < n; k++) {
          if (mask & (1 << k)) continue;
          var nm = mask | (1 << k), c = cur + m[j + 1][k + 1];
          if (c < dp[nm][k]) { dp[nm][k] = c; par[nm][k] = j; }
        }
      }
    }
    var best = INF, last = 0;
    for (j = 0; j < n; j++) if (dp[N - 1][j] < best) { best = dp[N - 1][j]; last = j; }
    var out = [], mk = N - 1, cj = last;
    while (cj !== -1) { out.push(cj + 1); var pj = par[mk][cj]; mk ^= (1 << cj); cj = pj; }
    return out.reverse();
  }

  function pathCost(m, seq) {
    var t = 0, prev = 0;
    for (var i = 0; i < seq.length; i++) { t += m[prev][seq[i]]; prev = seq[i]; }
    return t;
  }
  // Vecino mas cercano + mejora 2-opt, para mas de 12 paradas (raro en bici).
  function heuristic(m, n) {
    var left = [], i;
    for (i = 1; i <= n; i++) left.push(i);
    var seq = [], prev = 0;
    while (left.length) {
      var bi = 0;
      for (i = 1; i < left.length; i++) if (m[prev][left[i]] < m[prev][left[bi]]) bi = i;
      prev = left.splice(bi, 1)[0]; seq.push(prev);
    }
    var improved = true, guard = 0;
    while (improved && guard++ < 50) {
      improved = false;
      for (var a = 0; a < seq.length - 1; a++) {
        for (var b = a + 1; b < seq.length; b++) {
          var cand = seq.slice(0, a).concat(seq.slice(a, b + 1).reverse(), seq.slice(b + 1));
          if (pathCost(m, cand) + 1e-9 < pathCost(m, seq)) { seq = cand; improved = true; }
        }
      }
    }
    return seq;
  }

  // Ruta real por las calles pasando por todos los puntos en orden. -> {legs, geometry, source}
  function drive(points) {
    function fallback() {
      var legs = [], dist = [];
      for (var i = 1; i < points.length; i++) { legs.push(estSec(points[i - 1], points[i])); dist.push(estM(points[i - 1], points[i])); }
      return { legs: legs, dist: dist, geometry: points.map(function (p) { return [p.lat, p.lng]; }), source: "estimate" };
    }
    if (points.length < 2) return Promise.resolve({ legs: [], dist: [], geometry: points.map(function (p) { return [p.lat, p.lng]; }), source: "estimate" });
    return getJson(Route.base + "/route/v1/driving/" + coordStr(points) + "?overview=full&geometries=geojson&steps=false").then(function (j) {
      var r = j && j.code === "Ok" && j.routes && j.routes[0];
      if (!r || !r.legs || r.legs.length !== points.length - 1) throw new Error("bad route");
      return {
        legs: r.legs.map(function (l) { return l.duration; }),
        dist: r.legs.map(function (l, i) { return typeof l.distance === "number" ? l.distance : estM(points[i], points[i + 1]); }),
        geometry: r.geometry.coordinates.map(function (c) { return [c[1], c[0]]; }),
        source: "osrm"
      };
    }).catch(fallback);
  }

  Route.plan = function (start, stops, opts) {
    opts = opts || {};
    if (!stops.length) return Promise.resolve({ order: [], etas: [], legs: [], dist: [], geometry: [], totalSec: 0, source: "estimate" });
    var pts = [start].concat(stops);
    var pOrder = opts.order
      ? Promise.resolve({ order: opts.order.slice(), tsrc: "osrm" })
      : matrix(pts).then(function (r) { return { order: solveOrder(r.m, stops.length).map(function (i) { return i - 1; }), tsrc: r.source }; });
    return pOrder.then(function (o) {
      var seqPts = [start].concat(o.order.map(function (i) { return stops[i]; }));
      return drive(seqPts).then(function (d) {
        var t = 0, etas = [];
        for (var i = 0; i < d.legs.length; i++) { t += d.legs[i]; etas.push(Math.round(t)); t += Route.dwellSec; }
        return {
          order: o.order, etas: etas, legs: d.legs, dist: d.dist, geometry: d.geometry,
          totalSec: etas.length ? etas[etas.length - 1] : 0,
          source: (d.source === "osrm" && o.tsrc === "osrm") ? "osrm" : "estimate"
        };
      });
    });
  };

  Route.haversineKm = haversineKm;
  Route.estSec = estSec;
  Route.estM = estM;
  Route.solveOrder = solveOrder;
  Route.matrix = matrix;

  root.Route = Route;
  if (typeof module !== "undefined") module.exports = Route;
})(typeof window !== "undefined" ? window : this);
