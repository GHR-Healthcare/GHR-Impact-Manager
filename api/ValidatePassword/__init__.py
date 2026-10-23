import azure.functions as func
import hmac
import os
import json
from shared_code.auth import require_allowed_domain, current_user_email


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Validates the admin password for disabling privacy mode.
    Password is stored in the PRIVACY_PASSWORD environment variable.
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    # Fails closed. This used to fall back to the literal '2026' when the
    # variable was unset, so any signed-in user could un-redact competitor and
    # affiliate worker names with a guessable code. The variable is set on all
    # four environments, so there is nothing operational behind the default --
    # it was only a way for a misconfigured deploy to ship a known password.
    # (GH #16)
    correct_password = os.environ.get('PRIVACY_PASSWORD') or ''
    if not correct_password:
        print('ValidatePassword: PRIVACY_PASSWORD is not configured; refusing.')
        return func.HttpResponse(
            json.dumps({'valid': False}),
            mimetype='application/json', status_code=200)

    try:
        body = req.get_json() or {}
        submitted_password = body.get('password') or ''
    except Exception:
        # A malformed body is a failed attempt, not a server fault, and the
        # exception text is not echoed back. (GH #16, #29)
        return func.HttpResponse(
            json.dumps({'valid': False}),
            mimetype='application/json', status_code=400)

    # Constant-time: a plain == leaks how much of the password matched through
    # its timing, which matters because this endpoint is otherwise a free
    # oracle -- there is no attempt throttling, and adding real throttling
    # needs shared state these stateless functions do not have. Failed
    # attempts are logged so the attempt rate is at least visible.
    is_valid = hmac.compare_digest(str(submitted_password), str(correct_password))
    if not is_valid:
        print(f'ValidatePassword: failed attempt by {current_user_email(req) or "unknown"}')

    return func.HttpResponse(
        json.dumps({'valid': is_valid}),
        mimetype='application/json', status_code=200)
