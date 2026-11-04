#!/usr/bin/env node

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

//
// Creates the nightly cache-refresh Logic App for one app instance.
//
// WHY THIS EXISTS
// ---------------
// /api/cache/refresh rebuilds impactmgr.endpoint_cache for whichever routes
// that book lists in REFRESHABLE_BY_BOOK. Without a caller, the cache only
// ever warms from whoever loads the page first -- and on the slowest payloads
// that person waits past the 45s gateway timeout and gets an error, while the
// function keeps running and leaves the cache warm for the NEXT person. That
// is the problem the cache exists to remove, reappearing as a worse one.
//
// Each app instance needs its OWN run: the cache is per-book and an instance
// only knows its own. --target msp aims at the MSP app, which refreshes the
// extensions and onboarding payloads; the non-MSP targets refresh trend,
// financial and stats.
//
// Modelled on ghr-salespulse/scripts/deploy-snapshot-scheduler.mjs, which
// solves the same shape for its placement and snapshot caches.
//
// USAGE
//   az staticwebapp appsettings list --name <the app for your target> -o json \
//     | node tools/deploy-cache-scheduler.mjs [--target prototype|production|msp]
//
// Create-only by default: an existing workflow is reported and left alone.
// Pass --update to overwrite one, which is how a change to the definition
// below (the schedule, the URI, the retry policy) actually gets rolled out.
//
// Add --dry-run to print the definition (key redacted) without writing.
//
// CACHE_REFRESH_API_KEY is piped in on stdin and only ever held in memory: it
// is never written to disk, never passed in argv (argv is visible in the
// process list), and is redacted from all output.
//
// Idempotent: if the workflow exists this exits without touching it, so it can
// never silently overwrite a definition someone edited in the portal. To change
// a schedule, edit it in the portal or delete the workflow first.
//
// To stop refreshes: set the Logic App state to Disabled. Nothing breaks --
// read_cache ignores anything older than 26h, so the endpoints fall back to
// computing live, which is exactly how they behaved before the cache existed.

import { execFileSync } from 'node:child_process';

const RG = 'GHR_Azure_Resources';
const LOCATION = 'eastus';

// The caching code merged to main in 3.0.0, so production is the default
// target. Pass --target prototype only to aim at a named review environment.
const TARGETS = {
  prototype: {
    name: 'ghr-impactmgr-cache-refresh-prototype',
    uri: 'https://witty-stone-0bf78240f-prototype.eastus2.7.azurestaticapps.net/api/cache/refresh',
    app: 'ghr-nonmsp-impactmgr',
  },
  production: {
    name: 'ghr-impactmgr-cache-refresh',
    uri: 'https://witty-stone-0bf78240f.7.azurestaticapps.net/api/cache/refresh',
    app: 'ghr-nonmsp-impactmgr',
  },
  // MSP. Added 2026-11-04, when extensions-data and onboarding-data became
  // refreshable. extensions-data:msp:aff1 is the slowest payload either book
  // builds (~52s measured), so this is the target that most needs a schedule.
  // Pipe the MSP app's settings in, not the non-MSP app's:
  //   az staticwebapp appsettings list --name ghr-impact-manager -o json \
  //     | node tools/deploy-cache-scheduler.mjs --target msp
  msp: {
    name: 'ghr-impactmgr-cache-refresh-msp',
    uri: 'https://lively-plant-057be0a0f.6.azurestaticapps.net/api/cache/refresh',
    app: 'ghr-impact-manager',
  },
};

// Nightly. Both payloads are a four-week window and monthly billings -- neither
// moves within a day. 4am ET lands well before anyone opens the app, and the
// endpoint's own 26h staleness limit leaves room for one missed run before it
// falls back to computing live.
const RECURRENCE = {
  frequency: 'Day',
  interval: 1,
  timeZone: 'Eastern Standard Time',
  schedule: { hours: [4], minutes: [0] },
};

const dryRun = process.argv.includes('--dry-run');
// Overwrite a workflow that already exists. Off by default so a schedule
// someone tuned in the portal is not silently reverted by a routine re-run;
// required to roll out a change to the definition in this file.
const update = process.argv.includes('--update');
const tIdx = process.argv.indexOf('--target');
// Defaults to production since the 3.0.0 merge. It used to default to
// 'prototype', which after the merge would wire the nightly refresh at the
// staging slot instead of production non-MSP -- and because the Logic App is
// idempotent by name, that would succeed silently while production never
// warmed its cache at all.
const targetName = tIdx > -1 ? process.argv[tIdx + 1] : 'production';
const target = TARGETS[targetName];
if (!target) {
  console.error(`Unknown --target ${targetName}. Use prototype, production or msp.`);
  process.exit(1);
}

const stdin = await new Promise((resolve) => {
  let d = '';
  process.stdin.on('data', (c) => { d += c; });
  process.stdin.on('end', () => resolve(d));
});
if (!stdin.trim()) {
  console.error('Nothing on stdin. Pipe the app settings in:\n' +
    `  az staticwebapp appsettings list --name ${target.app} -o json | node ` + process.argv[1]);
  process.exit(1);
}

// `az` prints a cryptography UserWarning to stdout on this machine, so skip to
// the first brace rather than parsing the whole stream.
const brace = stdin.indexOf('{');
if (brace < 0) { console.error('No JSON object found on stdin.'); process.exit(1); }
const settings = (JSON.parse(stdin.slice(brace)).properties) || JSON.parse(stdin.slice(brace));
const key = settings.CACHE_REFRESH_API_KEY;
if (!key) {
  console.error(`CACHE_REFRESH_API_KEY is not set on ${target.app}. Set it first,\n` +
    'or the endpoint will reject every run with 401.');
  process.exit(1);
}
const redact = (s) => String(s).split(key).join('***');

const sub = execFileSync('az', ['account', 'show', '--query', 'id', '-o', 'tsv'],
  { encoding: 'utf8' }).trim();
const token = execFileSync('az',
  ['account', 'get-access-token', '--resource', 'https://management.azure.com',
   '--query', 'accessToken', '-o', 'tsv'],
  { encoding: 'utf8', maxBuffer: 1 << 22 }).trim();

const url = `https://management.azure.com/subscriptions/${sub}/resourceGroups/${RG}` +
  `/providers/Microsoft.Logic/workflows/${target.name}?api-version=2019-05-01`;

const body = {
  location: LOCATION,
  properties: {
    state: 'Enabled',
    definition: {
      $schema: 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#',
      contentVersion: '1.0.0.0',
      parameters: {},
      triggers: { Recurrence: { type: 'Recurrence', recurrence: RECURRENCE } },
      actions: {
        HTTP: {
          type: 'Http',
          runAfter: {},
          inputs: {
            method: 'POST',
            uri: target.uri,
            headers: { 'x-api-key': key },
            // NO RETRIES, deliberately. /api/cache/refresh is fire-and-forget
            // by necessity: it is an HTTP-triggered function behind the same
            // 45s SWA gateway as everything else, and the MSP book's four
            // payloads take about two minutes to build. The gateway returns
            // 500 at 45s while the function keeps running and writes every
            // entry -- measured 2026-11-04, all four written 2m07s after a
            // request the caller saw fail.
            //
            // Logic Apps' default is 4 exponential retries on 5xx. Against an
            // endpoint that always 500s and always succeeds anyway, that is
            // five overlapping full rebuilds a night, hammering the warehouse
            // to redo work that was already done. Observed once before this
            // was set: one variant got built twice by two concurrent runs.
            //
            // So the run will be marked FAILED in Logic Apps while having
            // done its job. Check impactmgr.endpoint_cache.refreshed_at to
            // see whether a night's refresh actually landed; the run history
            // is not the signal.
            retryPolicy: { type: 'none' },
          },
        },
      },
      outputs: {},
    },
  },
};

if (dryRun) {
  console.log(`DRY RUN — would PUT ${target.name} into ${RG}${update ? ' (overwriting)' : ''}:`);
  console.log(redact(JSON.stringify(body, null, 2)));
  process.exit(0);
}

const existing = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
if (existing.status === 200 && !update) {
  const cur = JSON.parse(await existing.text());
  const live = cur.properties?.definition?.actions?.HTTP?.inputs || {};
  const drift = [];
  if (live.uri !== target.uri) drift.push(`uri: ${live.uri} -> ${target.uri}`);
  if ((live.retryPolicy?.type || 'unset') !== 'none') {
    drift.push(`retryPolicy: ${live.retryPolicy?.type || 'unset (4 exponential retries)'} -> none`);
  }
  console.log(`${target.name} already exists (state: ${cur.properties?.state}). Not modifying it.`);
  if (drift.length) {
    // Create-only protects a hand-edited schedule, but it also meant a fix to
    // this file could never reach a workflow that already existed -- the
    // retryPolicy added 2026-11-04 silently did not apply to the MSP schedule
    // created an hour earlier, and the run output read like success. Say what
    // differs and how to apply it.
    console.log('\nIts definition differs from this script:');
    for (const d of drift) console.log(`  - ${d}`);
    console.log(`\nRe-run with --update to overwrite it, or edit it in the portal.`);
  }
  process.exit(0);
}

const res = await fetch(url, {
  method: 'PUT',
  headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});
const text = await res.text();
if (!res.ok) {
  console.error(`Failed (${res.status}): ${redact(text).slice(0, 600)}`);
  process.exit(1);
}
console.log(`Created ${target.name} — nightly 4am ET -> ${target.uri}`);
