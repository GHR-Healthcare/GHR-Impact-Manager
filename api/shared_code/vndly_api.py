"""
Workday VNDLY Program Services API client.

WHY THIS EXISTS

Requested time off has no home in the MSP book today. The warehouse copy of
the Bullhorn placement omits customTextBlock9, and VNDLY's staging tables
carry no time-off column, so the app can neither read nor write it -- the
team is asked for RTO and it goes nowhere a recruiter working in VNDLY can
see it.

VNDLY does have it, as a CONTRACTOR custom field named "RTO", writable
through the one contractor write endpoint:

    PATCH /services/program/contractors/v2/contractors/{system_id}/
    { "custom_fields": { "RTO": "7/1-7/5, 8/26" } }

Contractor-level rather than work-order-level, which is semantically right:
time off belongs to the person, not the assignment, and follows them across
seats. Note the consequence -- a clinician holding two concurrent seats has
one RTO value. Of 107 VNDLY work orders ending in the next 45 days, 107
carry a Contractor System Id across 97 distinct contractors, so ~10 seats
share a contractor with another.

AUTH AND LIMITS (from the Program Services API docs, 2026-09)

  Authorization: Token <api-token>      -- static API key, not OAuth
  List GET             1,000 req/min
  POST / PATCH / PUT   1,000 req/min
  All GET              5,000 req/min
  429 responses carry {"code": "throttled", "message": "...available in N
  second"} and are retried once here.

The token is a tenant-wide credential. It is read from the environment at
call time, never logged, and never included in an exception message.
"""
import json
import os
import time
import urllib.error
import urllib.request

# Set in Azure app settings. The sentinel is what the placeholder setting
# carries before someone pastes the real value, so an unconfigured instance
# reports "not configured" rather than sending "REPLACE_ME" as a credential.
_SENTINEL = 'REPLACE_ME'
_TIMEOUT = 25          # SWA's gateway gives up at 45s; leave room to fail cleanly
_RTO_FIELD = 'RTO'     # confirm against GET /custom_fields/ before relying on it


def _config():
    base = (os.environ.get('VNDLY_API_BASE_URL') or '').strip().rstrip('/')
    token = (os.environ.get('VNDLY_API_TOKEN') or '').strip()
    if not base or not token or token == _SENTINEL:
        return None, None
    return base, token


def is_configured():
    """True when both the base URL and a real token are present."""
    base, token = _config()
    return bool(base and token)


def _request(method, path, payload=None):
    """
    One authenticated call. Returns (status, parsed_body_or_text).

    Raises RuntimeError only for "not configured" -- transport and HTTP errors
    come back as a status so callers can degrade instead of 500ing. The token
    never appears in any message this raises or returns.
    """
    base, token = _config()
    if not base:
        raise RuntimeError('vndly_not_configured')

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
            # One retry on throttling, per the documented 429 contract.
            if e.code == 429 and attempt == 1:
                time.sleep(2)
                continue
            try:
                return e.code, json.loads(raw) if raw else None
            except ValueError:
                return e.code, raw
        except Exception as e:
            # Deliberately does not interpolate the URL: it is safe, but the
            # header is not, and keeping the rule simple keeps it kept.
            return 0, f'{type(e).__name__}: {e}'
    return 0, 'unreachable'


def list_custom_fields():
    """
    GET /custom_fields/ -- use this to confirm the exact RTO field key and
    type in this tenant before writing to it. The payload sample in the docs
    names "RTO", but samples are generated per tenant and can drift.
    """
    return _request('GET', '/services/program/custom_fields/v2/custom_fields/')


def get_contractor(system_id):
    """GET one contractor, including its custom_fields object."""
    return _request(
        'GET', f'/services/program/contractors/v2/contractors/{system_id}/')


def get_contractor_rto(system_id):
    """(status, rto_text_or_None) for one contractor."""
    status, body = get_contractor(system_id)
    if status != 200 or not isinstance(body, dict):
        return status, None
    return status, ((body.get('custom_fields') or {}).get(_RTO_FIELD))


def set_contractor_rto(system_id, rto_text):
    """
    Write requested time off to the contractor's RTO custom field.

    Sends only custom_fields.RTO -- PATCH is a partial update, so nothing
    else on the contractor is touched. Passing '' clears the field, which is
    how "no time off requested" should be recorded rather than the string
    "None".
    """
    return _request(
        'PATCH',
        f'/services/program/contractors/v2/contractors/{system_id}/',
        {'custom_fields': {_RTO_FIELD: rto_text if rto_text is not None else ''}},
    )
