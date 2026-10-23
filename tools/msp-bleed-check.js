#!/usr/bin/env node
/*
 * Did a new feature bleed into production MSP?
 *
 * NOTE (3.0.0, 2026-10-23): new MSP and new non-MSP HAVE now launched
 * together, so the rule below has been served. Against the 2.5.0 baseline this
 * tool now reports five differences by design -- three new tabs, four renames,
 * the redesigned job list, the standing bar's padding -- and exits 5. That is
 * the intended 3.0.0 surface, not a bleed. Use it to enumerate what a change
 * does to production MSP, and compare against 3.0.0 rather than c846d34 if you
 * want a clean exit; the BASELINE constant below is the thing to change.
 *
 * Production MSP had to keep rendering what it rendered at the 2.5.0 baseline
 * (c846d34) until new MSP and new non-MSP launched together. That rule has been
 * enforced by memory twice and failed twice: the standing revenue bar reached
 * production, and a wrapper added to gate the redesign broke scrolling on the
 * job list. Both were invisible in a diff and obvious in a browser.
 *
 * This renders index.html AS PRODUCTION MSP (dataSource: 'msp') at two git
 * refs and diffs what the page actually produces -- visible tabs and their
 * labels, body classes, the Open Jobs layout tree, KPI labels, whether the
 * legacy list is the thing on screen, and console errors.
 *
 * The deployed sites cannot be used for this: staticwebapp.config.json requires
 * an authenticated role on "/", so a headless browser lands on the Entra ID
 * sign-in page. It serves the files locally instead and stubs every /api/*
 * call, which needs no credentials and no network.
 *
 *   node tools/msp-bleed-check.js                  # baseline vs working tree
 *   node tools/msp-bleed-check.js main             # baseline vs main
 *   node tools/msp-bleed-check.js c846d34 main     # any two refs
 *
 * A ref of "." means the working tree. Exit code is the number of differences,
 * so it can gate a push.
 *
 * Requires playwright with chromium. If it is not resolvable, run
 * `npx playwright@1.63.0 --version` once to populate the npx cache.
 */

// --- CVE-2026-111497 (Node2Shell) runtime guard -------------------------
// Out-of-bounds write in Node's bundled Brotli decoder, reachable wherever a
// vulnerable Node decodes untrusted Brotli. fetch() advertises `br` and
// decodes Brotli responses, so these scripts are on that path. Fixed in
// 22.23.4 / 24.22.1 / 26.12.1; lines 20, 21, 23, 25 are end-of-life and will
// not be fixed. Inlined rather than imported because this repo has no
// package.json and the two scripts use different module systems -- see
// tools/node-version-guard.txt, and change both if you change either.
const FIXED = { 22: [22, 23, 4], 24: [24, 22, 1], 26: [26, 12, 1] };
{
  const v = process.versions.node.split('.').map(Number);
  const fix = FIXED[v[0]];
  const bad = !fix || v[1] < fix[1] || (v[1] === fix[1] && v[2] < fix[2]);
  if (bad && !process.env.ALLOW_VULNERABLE_NODE) {
    console.error(
      `\nRefusing to run on Node ${process.versions.node}.\n\n` +
      `CVE-2026-111497 is an out-of-bounds write in Node's bundled Brotli\n` +
      `decoder. This script uses fetch(), which decodes Brotli responses.\n\n` +
      `Fixed: 22.23.4+, 24.22.1+, 26.12.1+. Lines 20/21/23/25 are end-of-life\n` +
      `and will not be fixed.\n\n` +
      `  nvm install 24.22.1 && nvm use 24.22.1\n\n` +
      `Set ALLOW_VULNERABLE_NODE=1 to override, which is a decision to run a\n` +
      `known-vulnerable runtime, not a workaround.\n`);
    process.exit(1);
  }
}
// ------------------------------------------------------------------------

const { execSync } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), http = require('http');

const BASELINE = 'c846d34';           // the 2.5.0 production baseline
const PORT = 8873;

function resolvePlaywright() {
  const roots = [path.join(process.cwd(), 'node_modules'), path.join(os.homedir(), '.npm/_npx')];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    const direct = path.join(root, 'playwright');
    if (fs.existsSync(direct)) return require(direct);
    for (const d of fs.readdirSync(root)) {
      const p = path.join(root, d, 'node_modules/playwright');
      if (fs.existsSync(p)) return require(p);
    }
  }
  console.error('playwright not found — run: npx playwright@1.63.0 --version');
  process.exit(2);
}

const FIXTURES = {
  'get-config': { appVersion: 'bleed-check', dataSource: 'msp', defaultMargin: '25', otherInstanceUrl: '' },
  'closed-data': { rows: [] }, 'rate-intel': { peers: [], coverage: null }, 'workspace-state': {},
};

// What production MSP puts on the screen. Anything here changing is a bleed
// until new MSP launches.
function probe() {
  const vis = el => !!(el && el.offsetParent !== null);
  const lw = document.getElementById('listViewWrapper');
  const tree = [];
  if (lw) (function walk(n, d) {
    for (const c of n.children) {
      if (c.tagName !== 'DIV') continue;
      tree.push('  '.repeat(d) + (c.id ? '#' + c.id : '') + '.' + (c.className || '').trim().split(/\s+/).join('.'));
      if (d < 2) walk(c, d + 1);
    }
  })(lw, 0);
  return {
    tabs: [...document.querySelectorAll('button[id^="btnView"]')].filter(vis)
            .map(b => b.id.replace('btnView', '') + ':' + b.textContent.trim()).sort(),
    bodyClass: document.body.className.trim().split(/\s+/).sort().join(' '),
    kpis: [...document.querySelectorAll('#kpiContainer > div')]
            .map(d => (d.querySelector('span') || {}).textContent || '').map(t => t.trim()).filter(Boolean),
    legacyListOnScreen: vis(document.getElementById('jobsContainer')),
    redesignListHidden: (() => { const e = document.getElementById('listContent');
                                 return e ? e.classList.contains('hidden') : 'absent'; })(),
    listTree: tree.join('\n'),
  };
}

(async () => {
  const { chromium } = resolvePlaywright();
  const refs = process.argv.slice(2);
  const [refA, refB] = refs.length === 0 ? [BASELINE, '.'] : refs.length === 1 ? [BASELINE, refs[0]] : refs;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bleed-'));
  const stage = (ref, name) => {
    const html = ref === '.' ? fs.readFileSync('index.html', 'utf8')
                             : execSync(`git show ${ref}:index.html`, { maxBuffer: 1 << 28 }).toString();
    fs.writeFileSync(path.join(dir, name), html);
  };
  stage(refA, 'a.html'); stage(refB, 'b.html');

  const server = http.createServer((req, res) => {
    const f = path.join(dir, path.basename(req.url.split('?')[0]));
    if (!fs.existsSync(f)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(fs.readFileSync(f));
  }).listen(PORT, '127.0.0.1');

  const capture = async file => {
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message.split('\n')[0].slice(0, 90)));
    await page.route('**/api/**', r => {
      const n = r.request().url().split('/api/')[1].split('?')[0];
      r.fulfill({ status: 200, contentType: 'application/json',
                  body: JSON.stringify(Object.prototype.hasOwnProperty.call(FIXTURES, n) ? FIXTURES[n] : []) });
    });
    await page.goto(`http://127.0.0.1:${PORT}/${file}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(4000);
    const out = await page.evaluate(probe);
    out.errors = errors;
    await browser.close();
    return out;
  };

  const A = await capture('a.html'), B = await capture('b.html');
  server.close(); fs.rmSync(dir, { recursive: true, force: true });

  console.log(`\n  production MSP:  ${refA}  vs  ${refB}\n`);
  let diffs = 0;
  for (const k of Object.keys(A)) {
    if (JSON.stringify(A[k]) === JSON.stringify(B[k])) { console.log(`  same  ${k}`); continue; }
    diffs++;
    console.log(`  DIFF  ${k}`);
    if (k === 'listTree') {
      const la = A[k].split('\n'), lb = B[k].split('\n');
      la.filter(x => !lb.includes(x)).forEach(x => console.log(`          only ${refA}: ${x}`));
      lb.filter(x => !la.includes(x)).forEach(x => console.log(`          only ${refB}: ${x}`));
    } else {
      console.log(`          ${refA}: ${JSON.stringify(A[k]).slice(0, 240)}`);
      console.log(`          ${refB}: ${JSON.stringify(B[k]).slice(0, 240)}`);
    }
  }
  console.log(diffs === 0
    ? '\n  NO BLEED — production MSP renders identically\n'
    : `\n  ${diffs} difference(s). New-but-hidden elements are expected; anything VISIBLE changing is a bleed.\n`);
  process.exit(diffs);
})();
