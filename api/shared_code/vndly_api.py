"""
Workday VNDLY Program Services API client. Multi-tenant.

WHY THIS EXISTS

Requested time off has nowhere to live in the MSP book. The warehouse copy of
the Bullhorn placement omits customTextBlock9 and VNDLY's staging tables carry
no time-off column, so the app can neither read nor write it -- the team is
asked for RTO and it lands somewhere nobody working in VNDLY can see.

VNDLY has it, as a CONTRACTOR custom field named "RTO", writable through the
one contractor write endpoint:

    PATCH /services/program/contractors/v2/contractors/{system_id}/
    { "custom_fields": { "RTO": "7/1-7/5, 8/26" } }

Contractor-level rather than work-order-level, which is semantically right --
time off belongs to the person and follows them across seats. The consequence
is that a clinician holding two concurrent seats has one RTO value: of 107
VNDLY work orders ending in the next 45 days, all 107 carry a Contractor
System Id, across 97 distinct contractors.

ONE TENANT PER MSP CLIENT

Each MSP client is a separate VNDLY tenant with its own host and its own API
token; the docs note throttling is set per tenant and enforced per key. The
warehouse loads all of them into one set of STAGING_VNDLY_* tables, so
tenancy is invisible downstream -- which means a write has to resolve which
tenant to call from the row itself.

The only usable key is [Health System]. [Health System Id] CANNOT be used:
it is numbered per tenant and collides once pooled -- measured 2026-09-21,
Cooper University Healthcare and Inspira Medical Centers are BOTH id 3,
Redeemer Health is 4, RUMC is 5. Keying on the id would send Inspira's
writes to Cooper.

Tenants as of 2026-09-21, by active work orders:

    Cooper University Healthcare     148 active, 64 ending <=45d
    Inspira Medical Centers, Inc.    111 active, 31 ending <=45d
    RUMC                              61 active,  9 ending <=45d
    Redeemer Health                   15 active,  3 ending <=45d

CONFIGURATION

One app setting, VNDLY_TENANTS, holding JSON keyed by the exact
[Health System] string the staging table carries:

    {
      "Cooper University Healthcare":  {"base_url": "https://...", "token": "..."},
      "Inspira Medical Centers, Inc.": {"base_url": "https://...", "token": "..."},
      "RUMC":                          {"base_url": "https://...", "token": "..."},
      "Redeemer Health":               {"base_url": "https://...", "token": "..."}
    }

One setting rather than eight so adding a tenant is a value edit, not a
deploy. Tokens are read at call time, never logged, and never interpolated
into an exception message.

AUTH AND LIMITS (Program Services API docs, 2026-09)

  Authorization: Token <api-token>      -- static API key, not OAuth
  List GET 1,000/min | POST/PATCH/PUT 1,000/min | all GET 5,000/min
  429 carries {"code": "throttled", ...} and is retried once here.
"""
import json
import os
import time
import urllib.error
import urllib.request

_SENTINEL = 'REPLACE_ME'
_TIMEOUT = 25          # SWA's gateway gives up at 45s; leave room to fail cleanly
_RTO_FIELD = 'RTO'     # confirm against GET /custom_fields/ before relying on it


def _tenants():
    """Parsed VNDLY_TENANTS, or {} when unset/placeholder/malformed."""
    raw = (os.environ.get('VNDLY_TENANTS') or '').strip()
    if not raw or raw == _SENTINEL:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        # Deliberately does not echo `raw` -- it holds every tenant's token.
        print(f'vndly_api: VNDLY_TENANTS is not valid JSON ({e}); treating as unset')
        return {}
    return parsed if isinstance(parsed, dict) else {}


def configured_tenants():
    """Health-system names that currently have usable credentials."""
    out = []
    for name, cfg in _tenants().items():
        if not isinstance(cfg, dict):
            continue
        base = (cfg.get('base_url') or '').strip()
        token = (cfg.get('token') or '').strip()
        if base and token and token != _SENTINEL:
            out.append(name)
    return sorted(out)


def is_configured(health_system=None):
    """True when that tenant has credentials, or any tenant does if None."""
    names = configured_tenants()
    return bool(names) if health_system is None else health_system in names


def _config(health_system):
    cfg = _tenants().get(health_system)
    if not isinstance(cfg, dict):
        return None, None
    base = (cfg.get('base_url') or '').strip().rstrip('/')
    token = (cfg.get('token') or '').strip()
    if not base or not token or token == _SENTINEL:
        return None, None
    return base, token


def _request(health_system, method, path, payload=None):
    """
    One authenticated call against a named tenant.

    Returns (status, parsed_body_or_text). Transport and HTTP errors come back
    as a status rather than raising, so a caller can degrade instead of 500ing.
    Status 0 means the call never completed. The token never appears in any
    message returned or raised.
    """
    base, token = _config(health_system)
    if not base:
        return 0, f'vndly_tenant_not_configured: {health_system}'

    url = f"{base}/{path.lstrip('/')}"
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header('Authorization', f'Token {token}')
        req.add_header('Accept', 'application/json')
        if body is not None:
            req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read().decode('utf-8', 'replace')
                try:
                    return resp.status, json.loads(raw) if raw else None
                except ValueError:
                    return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace') if e.fp else ''
            if e.code == 429 and attempt == 1:   # documented throttle contract
                time.sleep(2)
                continue
            try:
                return e.code, json.loads(raw) if raw else None
            except ValueError:
                return e.code, raw
        except Exception as e:
            return 0, f'{type(e).__name__}: {e}'
    return 0, 'unreachable'


def list_custom_fields(health_system):
    """
    Confirm the exact RTO field key and type for a tenant before writing.
    Payload samples in the docs are generated per tenant and can drift, and
    with four tenants they need not agree with each other.
    """
    return _request(health_system, 'GET',
                    '/services/program/custom_fields/v2/custom_fields/')


def get_contractor(health_system, system_id):
    """GET one contractor, including its custom_fields object."""
    return _request(
        health_system, 'GET',
        f'/services/program/contractors/v2/contractors/{system_id}/')


def get_contractor_rto(health_system, system_id):
    """(status, rto_text_or_None) for one contractor."""
    status, body = get_contractor(health_system, system_id)
    if status != 200 or not isinstance(body, dict):
        return status, None
    return status, ((body.get('custom_fields') or {}).get(_RTO_FIELD))


def set_contractor_rto(health_system, system_id, rto_text):
    """
    Write requested time off to the contractor's RTO custom field.

    Sends only custom_fields.RTO -- PATCH is a partial update, so nothing else
    on the contractor is touched. Passing '' clears the field, which is how
    "no time off requested" should be recorded rather than the string "None".
    """
    return _request(
        health_system, 'PATCH',
        f'/services/program/contractors/v2/contractors/{system_id}/',
        {'custom_fields': {_RTO_FIELD: rto_text if rto_text is not None else ''}},
    )
