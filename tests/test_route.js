// Pruebas de site/route.js.   Correr:  node tests/test_route.js
const assert = require("assert");
const path = require("path");
const Route = require(path.join(__dirname, "..", "site", "route.js"));

let n = 0;
async function test(name, fn) { await fn(); n++; console.log("ok -", name); }

function permutations(a) {
  if (a.length <= 1) return [a];
  return a.flatMap((x, i) => permutations([...a.slice(0, i), ...a.slice(i + 1)]).map(p => [x, ...p]));
}
function cost(m, seq) { let t = 0, p = 0; for (const s of seq) { t += m[p][s]; p = s; } return t; }
function rnd(seed) { let s = seed; return () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296; }

(async () => {
  await test("Held-Karp coincide con fuerza bruta (asimetrico, n=1..7)", () => {
    const r = rnd(42);
    for (let size = 1; size <= 7; size++) for (let rep = 0; rep < 25; rep++) {
      const m = Array.from({ length: size + 1 }, (_, i) => Array.from({ length: size + 1 }, (_, j) => i === j ? 0 : 1 + r() * 100));
      const got = Route.solveOrder(m, size);
      assert.deepStrictEqual([...got].sort(), Array.from({ length: size }, (_, i) => i + 1));
      const best = Math.min(...permutations(Array.from({ length: size }, (_, i) => i + 1)).map(p => cost(m, p)));
      assert.ok(Math.abs(cost(m, got) - best) < 1e-9, `size ${size}`);
    }
  });

  await test("n=0 -> vacio; n grande usa heuristica y devuelve permutacion valida y no peor que el orden dado", () => {
    assert.deepStrictEqual(Route.solveOrder([[0]], 0), []);
    const r = rnd(7), size = 15;
    const m = Array.from({ length: size + 1 }, (_, i) => Array.from({ length: size + 1 }, (_, j) => i === j ? 0 : 1 + r() * 100));
    const got = Route.solveOrder(m, size);
    assert.deepStrictEqual([...got].sort((a, b) => a - b), Array.from({ length: size }, (_, i) => i + 1));
    assert.ok(cost(m, got) <= cost(m, Array.from({ length: size }, (_, i) => i + 1)));
  });

  const start = { lat: 21.3808, lng: -77.9169 };
  const A = { lat: 21.3900, lng: -77.9100 }, B = { lat: 21.3830, lng: -77.9150 }, C = { lat: 21.3950, lng: -77.9050 };

  await test("sin red: cae a estimacion, ordena por cercania en linea (B, A, C) y calcula ETAs acumuladas con espera", async () => {
    Route.fetch = () => Promise.reject(new Error("sin red"));
    const p = await Route.plan(start, [A, B, C]);
    assert.strictEqual(p.source, "estimate");
    assert.deepStrictEqual(p.order, [1, 0, 2]);            // B, A, C
    assert.strictEqual(p.etas.length, 3);
    assert.ok(p.etas[0] < p.etas[1] && p.etas[1] < p.etas[2]);
    const leg = (a, b) => Route.estSec(a, b);
    const expect1 = leg(start, B), expect2 = expect1 + Route.dwellSec + leg(B, A);
    assert.ok(Math.abs(p.etas[0] - expect1) <= 1);
    assert.ok(Math.abs(p.etas[1] - expect2) <= 1);
    assert.strictEqual(p.geometry.length, 4);
    assert.strictEqual(p.totalSec, p.etas[2]);
    assert.strictEqual(p.dist.length, 3);                   // metros por tramo (el servidor los usa para el ETA con GPS)
    assert.ok(Math.abs(p.dist[0] - Route.estM(start, B)) <= 1);
  });

  await test("con OSRM simulado: usa sus duraciones, su geometria y marca source=osrm", async () => {
    const calls = [];
    Route.fetch = async (url) => {
      calls.push(url);
      if (url.includes("/table/")) {
        // 3 puntos: start, X, Y. Y es mas cerca de start que X (contra la distancia recta a proposito).
        return { ok: true, json: async () => ({ code: "Ok", durations: [[0, 900, 100], [900, 0, 300], [100, 300, 0]] }) };
      }
      return { ok: true, json: async () => ({ code: "Ok", routes: [{ legs: [{ duration: 100, distance: 700 }, { duration: 300, distance: 1900 }],
        geometry: { coordinates: [[-77.9169, 21.3808], [-77.91, 21.39], [-77.9, 21.4]] } }] }) };
    };
    const p = await Route.plan(start, [A, B]);
    assert.strictEqual(p.source, "osrm");
    assert.deepStrictEqual(p.order, [1, 0]);               // Y (=B, indice 1) primero
    assert.deepStrictEqual(p.etas, [100, 100 + Route.dwellSec + 300]);
    assert.deepStrictEqual(p.dist, [700, 1900]);
    assert.deepStrictEqual(p.geometry[1], [21.39, -77.91]); // [lat,lng] invertido de GeoJSON
    assert.ok(calls[0].includes("/routed-bike/table/v1/driving/-77.916900,21.380800;"));
    assert.ok(calls[1].includes("/route/v1/driving/"));
  });

  await test("tabla OK pero ruta falla -> source=estimate (no finge precision)", async () => {
    Route.fetch = async (url) => url.includes("/table/")
      ? { ok: true, json: async () => ({ code: "Ok", durations: [[0, 5], [5, 0]] }) }
      : { ok: false, status: 500, json: async () => ({}) };
    const p = await Route.plan(start, [A]);
    assert.strictEqual(p.source, "estimate");
    assert.strictEqual(p.etas.length, 1);
  });

  await test("duracion null en la tabla se reemplaza por estimacion", async () => {
    Route.fetch = async () => ({ ok: true, json: async () => ({ code: "Ok", durations: [[0, null], [null, 0]] }) });
    const r = await Route.matrix([start, A]);
    assert.ok(r.m[0][1] > 0 && isFinite(r.m[0][1]));
  });

  await test("orden fijo (opts.order) no vuelve a optimizar ni pide la tabla", async () => {
    const urls = [];
    Route.fetch = async (u) => { urls.push(u); throw new Error("x"); };
    const p = await Route.plan(start, [A, B, C], { order: [2, 0, 1] });
    assert.deepStrictEqual(p.order, [2, 0, 1]);
    assert.ok(urls.every(u => !u.includes("/table/")));
  });

  await test("sin paradas -> plan vacio; timeout corta una respuesta colgada", async () => {
    assert.deepStrictEqual((await Route.plan(start, [])).order, []);
    Route.timeoutMs = 50;
    Route.fetch = () => new Promise(() => {});              // nunca responde
    const t0 = Date.now();
    const p = await Route.plan(start, [A]);
    assert.strictEqual(p.source, "estimate");
    assert.ok(Date.now() - t0 < 1500);
  });

  console.log(`\n${n} pruebas OK`);
})().catch(e => { console.error("FALLO:", e); process.exit(1); });
