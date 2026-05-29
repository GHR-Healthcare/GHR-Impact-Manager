import azure.functions as func
import json
import os
import platform
from datetime import datetime, timezone


def main(req: func.HttpRequest) -> func.HttpResponse:
    """Anonymous health endpoint for external pollers (e.g. GHR Central).
    Returns 200 with app metadata. Does NOT call require_allowed_domain —
    this endpoint is intentionally unauthenticated."""
    body = {
        'status': 'ok',
        'app': os.environ.get('WEBSITE_SITE_NAME', 'unknown'),
        'python': platform.python_version(),
        'time': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
    }
    return func.HttpResponse(
        json.dumps(body),
        mimetype='application/json',
        status_code=200,
    )
