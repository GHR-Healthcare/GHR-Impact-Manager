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
        config = {
            'defaultMargin': os.environ.get('DEFAULT_MARGIN', '25'),
            'appVersion': os.environ.get('APP_VERSION', '1.7.6'),
            # 'msp' (default) or 'non_msp' — frontend uses this to hide tabs
            # that don't apply to the non-MSP instance (Pending, Per Diem).
            'dataSource': get_data_source(),
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