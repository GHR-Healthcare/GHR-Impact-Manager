import azure.functions as func
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.data_source import get_data_source

def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error
    try:
        ds = get_data_source()
        # Sibling instance URL for the MSP ↔ Non-MSP header toggle. Prefer
        # OTHER_INSTANCE_URL env var; fall back to the ghrhealthcare.com
        # custom hostnames so a fresh deploy works before the env var is set.
        default_other = (
            'https://impactmgr.ghrhealthcare.com'
            if ds == 'non_msp'
            else 'https://impactmgr-nonmsp.ghrhealthcare.com'
        )
        other_url = (os.environ.get('OTHER_INSTANCE_URL') or default_other).rstrip('/')
        config = {
            'defaultMargin': os.environ.get('DEFAULT_MARGIN', '25'),
            'appVersion': os.environ.get('APP_VERSION', '2.2.5'),
            # 'msp' (default) or 'non_msp' — frontend uses this to hide tabs
            # that don't apply to the non-MSP instance (Pending, Per Diem).
            'dataSource': ds,
            # Sibling instance URL for the header toggle. Empty string = hide toggle.
            'otherInstanceUrl': other_url,
            'otherInstanceLabel': 'MSP' if ds == 'non_msp' else 'Non-MSP',
        }
        
        return func.HttpResponse(
            json.dumps(config),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500
        )