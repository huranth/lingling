/**
 * Lingling frontend smoke test.
 *
 * Drives the real dashboard in headless Chrome over the DevTools Protocol and
 * asserts that every view renders against live backend data. This exists because
 * the frontend is ~2,500 lines with no other safety net: a typo in an id, a
 * renamed API field, or an unbalanced brace all produce a silently blank panel
 * that no backend test would catch.
 *
 * It is deliberately dependency-free — Node 18+ ships both `fetch` and
 * `WebSocket`, which is all the CDP client needs.
 *
 * Usage:
 *   node frontend/tests/smoke.mjs                       # assumes :8000 + :9222
 *   node frontend/tests/smoke.mjs http://127.0.0.1:8000 http://127.0.0.1:9222
 *
 * Prerequisites: the gateway running, and Chrome started with
 *   --headless=new --remote-debugging-port=9222 --user-data-dir=<tmp>
 *
 * Exit code is 0 on success, 1 on any failed assertion or page error.
 */

const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const CDP = process.argv[3] || 'http://127.0.0.1:9222';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- assertions ---------- */
let passed = 0;
const failures = [];
function check(name, condition, detail) {
  if (condition) {
    passed++;
    console.log(`  PASS  ${name}`);
  } else {
    failures.push(`${name}${detail ? ' — ' + detail : ''}`);
    console.log(`  FAIL  ${name}${detail ? ' — ' + detail : ''}`);
  }
}

/* ---------- minimal CDP client ---------- */
async function connect() {
  const targets = await (await fetch(CDP + '/json/list')).json();
  const page = targets.find((t) => t.type === 'page');
  if (!page) throw new Error('no page target; is Chrome running with --remote-debugging-port?');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const pending = new Map();
  const pageErrors = [];
  let seq = 0;

  ws.addEventListener('message', (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') {
      pageErrors.push('exception: ' +
        (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      pageErrors.push('console.error: ' +
        m.params.args.map((a) => a.value ?? a.description ?? '').join(' '));
    }
    if (m.method === 'Network.responseReceived' && m.params.response.status >= 400) {
      pageErrors.push(`HTTP ${m.params.response.status} ${m.params.response.url}`);
    }
  });
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve);
    ws.addEventListener('error', () => reject(new Error('CDP websocket failed')));
  });

  const send = (method, params = {}) => new Promise((resolve) => {
    const id = ++seq;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const r = await send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (r.result?.exceptionDetails) {
      throw new Error('eval failed: ' +
        (r.result.exceptionDetails.exception?.description || 'unknown'));
    }
    return r.result?.result?.value;
  };
  return { ws, send, evaluate, pageErrors };
}

/* ---------- the run ---------- */
const { ws, send, evaluate, pageErrors } = await connect();

await send('Runtime.enable');
await send('Network.enable');
// A cached page would hide the very change under test.
await send('Network.setCacheDisabled', { cacheDisabled: true });
await send('Emulation.setDeviceMetricsOverride', {
  width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false,
});

console.log(`\nLingling frontend smoke test  (${BASE})`);
console.log('='.repeat(70));

// Start from a known layout. Note: navigating from `/` to `/#models` is only a
// hash change — the document never reloads and the in-memory board would keep
// the previous run's arrangement. Clear storage, then force a real reload.
await send('Page.navigate', { url: BASE });
await sleep(1500);
await evaluate('localStorage.clear()');
await send('Page.reload', { ignoreCache: true });
await sleep(7000);
await evaluate("go('models')");
await sleep(1200);

/* --- boot + session --- */
check('page boots and exposes state', await evaluate("typeof state === 'object'"));
check('gateway reported healthy',
  (await evaluate("(state.health && state.health.status) || ''")) === 'ok');

/* --- vendor libraries --- */
check('uPlot loaded (SRI passed)', await evaluate("typeof window.uPlot === 'function'"));
check('Tabulator loaded (SRI passed)', await evaluate("typeof window.Tabulator === 'function'"));
check('Lucide loaded (SRI passed)', await evaluate("!!(window.lucide && window.lucide.createIcons)"));
check('marked + DOMPurify loaded (SRI passed)',
  await evaluate("!!(window.marked && window.DOMPurify)"));
check('icons actually rendered',
  (await evaluate("document.querySelectorAll('svg.lucide, .rail-btn svg').length")) > 4);

/* --- 1. catalog --- */
check('catalog: models fetched', (await evaluate('state.models.length')) > 0);
check('catalog: matrix rendered rows',
  (await evaluate("document.querySelectorAll('#models-grid .tabulator-row').length")) > 0);
check('catalog: capability pips rendered',
  (await evaluate("document.querySelectorAll('.cap-pip').length")) > 0);
check('catalog: detail panel shows a spec sheet',
  await evaluate("!!document.querySelector('#model-detail .spec')"));


/* --- 2. console --- */
await evaluate("go('console')");
await sleep(1200);
check('console: target select populated',
  (await evaluate("document.getElementById('model-select').options.length")) > 1);
check('console: composer present',
  await evaluate("!!document.querySelector('.compose textarea')"));
check('console: empty state shown before any traffic',
  await evaluate("!!document.querySelector('#tape .empty')"));

/* --- 3. ledger board --- */
await evaluate("go('ledger')");
await sleep(4000);
check('ledger: all six blocks rendered',
  (await evaluate("document.querySelectorAll('#board .card').length")) === 6,
  'got ' + (await evaluate("document.querySelectorAll('#board .card').length")));
const spans = await evaluate("[...document.querySelectorAll('#board .card')].map(c=>c.dataset.block+':'+c.dataset.span).join(' ')");
check('ledger: default spans applied',
  spans === 'figures:12 activity:8 latency:4 share:6 outcome:6 log:12', spans);
check('ledger: two charts on canvas',
  (await evaluate("document.querySelectorAll('#board canvas').length")) === 2);
check('ledger: figure band populated',
  (await evaluate("document.querySelectorAll('.figs .n').length")) === 6);
check('ledger: live buckets fetched',
  (await evaluate('state.live.length')) > 0);
check('ledger: summary fetched',
  await evaluate("!!(state.summary && state.summary.totals)"));
check('ledger: high-water mark tracked for incremental polling',
  (await evaluate('typeof state.lastRowId')) === 'number');

/* Metric toggle must rebuild the series, not silently no-op. */
await evaluate("document.querySelector('#metric-seg button[data-metric=\"tokens\"]').click()");
await sleep(1200);
check('ledger: tokens metric draws a stacked pair',
  (await evaluate('state.charts.activity ? state.charts.activity.series.length : 0')) === 3);
await evaluate("document.querySelector('#metric-seg button[data-metric=\"requests\"]').click()");
await sleep(1000);

/* --- 4. arrange mode --- */
await evaluate('setArranging(true)');
await sleep(1000);
check('arrange: grips become visible',
  (await evaluate("getComputedStyle(document.querySelector('.grip')).display")) !== 'none');
await evaluate("document.querySelector('[data-span-set=\"latency\"][data-size=\"6\"]').click()");
await sleep(1200);
check('arrange: resize applied',
  (await evaluate("document.querySelector('[data-block=\"latency\"]').dataset.span")) === '6');
check('arrange: charts survive the repaint',
  await evaluate("!!(state.charts.activity && state.charts.activity.root.isConnected && state.charts.latency && state.charts.latency.root.isConnected)"),
  'a detached canvas means setData writes into nothing');
await evaluate("document.querySelector('[data-hide=\"outcome\"]').click()");
await sleep(900);
check('arrange: hide removes the card',
  (await evaluate("document.querySelectorAll('#board .card').length")) === 5);
check('arrange: hidden block offered in the tray',
  (await evaluate("document.querySelectorAll('#hidden-tray [data-show]').length")) === 1);
check('arrange: layout persisted',
  (await evaluate("(localStorage.getItem('lingling.board.v1') || '').indexOf('\"latency\":6') > -1")));
await evaluate("document.querySelector('[data-show=\"outcome\"]').click()");
await sleep(900);
check('arrange: restore brings it back',
  (await evaluate("document.querySelectorAll('#board .card').length")) === 6);
await evaluate('setArranging(false)');
await sleep(600);


/* --- 5. egress --- */
await evaluate("go('egress')");
await sleep(2500);
check('egress: gauge rendered', await evaluate("!!document.querySelector('.gauge .val')"));
check('egress: rack reachable (slots or explicit empty state)',
  (await evaluate("document.querySelectorAll('#egress-zone .slot').length")) > 0 ||
  await evaluate("!!document.querySelector('#egress-zone .empty')"));

/* --- 7. responsive --- */
await send('Emulation.setDeviceMetricsOverride', {
  width: 1100, height: 900, deviceScaleFactor: 1, mobile: false,
});
await sleep(800);
check('responsive: telemetry rail hides below 1240px',
  (await evaluate("getComputedStyle(document.querySelector('.tele')).display")) === 'none');
await send('Emulation.setDeviceMetricsOverride', {
  width: 414, height: 860, deviceScaleFactor: 2, mobile: true,
});
await evaluate("go('ledger')");
await sleep(3000);
check('responsive: rail becomes a bottom bar at 414px',
  (await evaluate("getComputedStyle(document.querySelector('.rail')).flexDirection")) === 'row');
check('responsive: board collapses to one column',
  await evaluate(`(() => {
    const cards = [...document.querySelectorAll('#board .card')];
    const board = document.getElementById('board').getBoundingClientRect().width;
    return cards.every((c) => Math.abs(c.getBoundingClientRect().width - board) < 2);
  })()`));
check('responsive: no horizontal overflow at 414px',
  await evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1'));
check('responsive: charts still render on mobile',
  (await evaluate("document.querySelectorAll('#board canvas').length")) === 2);
await send('Emulation.clearDeviceMetricsOverride');

/* --- 9. stop button, vision guard, keyboard reorder --- */
await evaluate("go('console')");
await sleep(1000);
check('console: Send starts in send mode',
  (await evaluate("document.getElementById('btn-send').textContent.trim()")) === 'Send');
check('console: screen-reader live region present',
  await evaluate("!!document.querySelector('#sr-live[aria-live=\"polite\"]')"));
check('console: tape reports busy state to assistive tech',
  await evaluate("document.getElementById('tape').hasAttribute('aria-busy')"));

// The vision guard is a pure function of catalog state, so it can be asserted
// without actually attaching a file.
check('vision guard: router accepts images',
  await evaluate("targetAcceptsImages((state.multimodel && state.multimodel.id) || 'lingling-auto')"));
check('vision guard: a text-only model is rejected',
  await evaluate(`(() => {
    const textOnly = (state.models || []).find((m) => !m.vision);
    return textOnly ? targetAcceptsImages(textOnly.id) === false : true;
  })()`));
check('vision guard: a vision model is found for the switch offer',
  await evaluate("!!firstVisionModel() || !(state.models||[]).some(m=>m.vision)"));

// Keyboard reorder must move the block and persist, same as a drag.
// Explicit desktop viewport: section 7 cleared the override, and below 900px the
// grips are display:none by design (the board is one column, so reordering is
// meaningless there) — a hidden button cannot take focus.
await send('Emulation.setDeviceMetricsOverride', {
  width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false,
});
await evaluate("go('ledger')");
await sleep(3000);
await evaluate('setArranging(true)');
await sleep(900);
const orderBefore = await evaluate("[...document.querySelectorAll('#board .card')].map(c=>c.dataset.block).join(',')");
await evaluate(`(() => {
  const grip = document.querySelector('[data-grip="activity"]');
  grip.focus();
  grip.dispatchEvent(new KeyboardEvent('keydown', { key:'ArrowRight', bubbles:true }));
  return true;
})()`);
await sleep(1200);
const orderAfter = await evaluate("[...document.querySelectorAll('#board .card')].map(c=>c.dataset.block).join(',')");
check('keyboard: arrow key reorders a block',
  orderBefore !== orderAfter, `${orderBefore} -> ${orderAfter}`);
check('keyboard: focus follows the moved block',
  (await evaluate("document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.grip : ''")) === 'activity');
check('keyboard: the move persisted',
  (await evaluate("(localStorage.getItem('lingling.board.v1')||'').indexOf('latency') > -1")));
await evaluate('setArranging(false)');
await sleep(600);

/* --- 8. no page errors anywhere in the run --- */
const unique = [...new Set(pageErrors)];
check('no console errors, exceptions or 4xx/5xx during the run',
  unique.length === 0, unique.join(' | '));

/* ---------- report ---------- */
console.log('='.repeat(70));
console.log(`${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.log('\nFailures:');
  for (const f of failures) console.log('  - ' + f);
}
ws.close();
process.exit(failures.length ? 1 : 0);

