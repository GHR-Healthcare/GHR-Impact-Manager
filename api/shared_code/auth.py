"""
Tenant allowlist enforcement.

SWA Free's built-in Microsoft provider accepts ANY Microsoft account, so this
module gates every /api/* call to users whose email is in one of our domains.
Call require_allowed_domain(req) at the top of each function's main(); if it
returns a response, return it immediately.
"""
import base64
import json
import azure.functions as func


ALLOWED_DOMAINS = {
    'ghrhealthcare.com',
    'unitedanesthesia.com',
    'ghreducation.com',
}

# Claim types SWA may use to surface the user's email/UPN.
EMAIL_CLAIM_TYPES = {
    'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress',
    'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn',
    'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name',
    'preferred_username',
    'upn',
    'email',
    'emails',
}


def _extract_email(principal):
    candidates = []
    ud = principal.get('userDetails')
    if ud:
        candidates.append(ud)
    for c in (principal.get('claims') or []):
        ctype = c.get('typ') or c.get('type')
        if ctype in EMAIL_CLAIM_TYPES:
            v = c.get('val') or c.get('value')
            if v:
                candidates.append(v)
    for v in candidates:
        if isinstance(v, str) and '@' in v:
            return v.strip().lower()
    return ''


def require_allowed_domain(req):
    """Return None if the caller is in an allowed domain; otherwise an
    HttpResponse the caller should return immediately."""
    raw = req.headers.get('x-ms-client-principal')
    if not raw:
        return func.HttpResponse(
            json.dumps({'error': 'not_authenticated'}),
            status_code=401,
            mimetype='application/json',
        )
    try:
        principal = json.loads(base64.b64decode(raw).decode('utf-8'))
    except Exception:
        return func.HttpResponse(
            json.dumps({'error': 'invalid_principal'}),
            status_code=401,
            mimetype='application/json',
        )
    email = _extract_email(principal)
    if not email or '@' not in email:
        return func.HttpResponse(
            json.dumps({'error': 'no_email_in_principal'}),
            status_code=403,
            mimetype='application/json',
        )
    domain = email.rsplit('@', 1)[1]
    if domain not in ALLOWED_DOMAINS:
        return func.HttpResponse(
            json.dumps({'error': 'domain_not_allowed', 'domain': domain}),
            status_code=403,
            mimetype='application/json',
        )
    return None
