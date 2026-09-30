#!/usr/bin/env node
//
// Creates the nightly cache-refresh Logic App for the non-MSP instance.
//
// WHY THIS EXISTS
// ---------------
// /api/cache/refresh rebuilds impactmgr.endpoint_cache for trend-data and
// financial-data. Without a caller, the cache only ever warms from whoever
// loads the page first -- and that person waits 14s, which is the problem the
// cache exists to remove.
//
// Modelled on ghr-salespulse/scripts/deploy-snapshot-scheduler.mjs, which
// solves the same shape for its placement and snapshot caches.
//
// USAGE
//   az staticwebapp appsettings list --name ghr-nonmsp-impactmgr -o json \
//     | node tools/deploy-cache-scheduler.mjs [--target prototype|production]
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

// The caching code currently lives on feature/msp-impact-prototype, which
// deploys to the prototype environment. Production non-MSP needs its own
// workflow once this merges to main -- pass --target production then.
const TARGETS = {
  prototype: {
    name: 'ghr-impactmgr-cache-refresh-prototype',
    uri: 'https://witty-stone-0bf78240f-prototype.eastus2.7.azurestaticapps.net/api/cache/refresh',
  },
  production: {
    name: 'ghr-impactmgr-cache-refresh',
    uri: 'https://witty-stone-0bf78240f.7.azurestaticapps.net/api/cache/refresh',
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
const tIdx = process.argv.indexOf('--target');
const targetName = tIdx > -1 ? process.argv[tIdx + 1] : 'prototype';
const target = TARGETS[targetName];
if (!target) {
  console.error(`Unknown --target ${targetName}. Use prototype or production.`);
  process.exit(1);
}

const stdin = await new Promise((resolve) => {
  let d = '';
  process.stdin.on('data', (c) => { d += c; });
  process.stdin.on('end', () => resolve(d));
});
if (!stdin.trim()) {
  console.error('Nothing on stdin. Pipe the app settings in:\n' +
    '  az staticwebapp appsettings list --name ghr-nonmsp-impactmgr -o json | node ' + process.argv[1]);
  process.exit(1);
}

// `az` prints a cryptography UserWarning to stdout on this machine, so skip to
// the first brace rather than parsing the whole stream.
const brace = stdin.indexOf('{');
if (brace < 0) { console.error('No JSON object found on stdin.'); process.exit(1); }
const settings = (JSON.parse(stdin.slice(brace)).properties) || JSON.parse(stdin.slice(brace));
const key = settings.CACHE_REFRESH_API_KEY;
if (!key) {
  console.error('CACHE_REFRESH_API_KEY is not set on ghr-nonmsp-impactmgr. Set it first,\n' +
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
          inputs: { method: 'POST', uri: target.uri, headers: { 'x-api-key': key } },
        },
      },
      outputs: {},
    },
  },
};

if (dryRun) {
  console.log(`DRY RUN — would PUT ${target.name} into ${RG}:`);
  console.log(redact(JSON.stringify(body, null, 2)));
  process.exit(0);
}

const existing = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
if (existing.status === 200) {
  const cur = JSON.parse(await existing.text());
  console.log(`${target.name} already exists (state: ${cur.properties?.state}). Not modifying it.`);
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
