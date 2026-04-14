import azure.functions as func
import os
import json
import urllib.request
import urllib.parse


def get_graph_token():
    """Get an access token for Microsoft Graph using client credentials flow."""
    tenant_id = os.environ.get('GRAPH_TENANT_ID', '')
    client_id = os.environ.get('GRAPH_CLIENT_ID', '')
    client_secret = os.environ.get('GRAPH_CLIENT_SECRET', '')

    if not all([tenant_id, client_id, client_secret]):
        raise ValueError('Missing GRAPH_TENANT_ID, GRAPH_CLIENT_ID, or GRAPH_CLIENT_SECRET env vars')

    token_url = f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token'
    body = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': 'https://graph.microsoft.com/.default'
    }).encode('utf-8')

    req = urllib.request.Request(token_url, data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')

    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        return data['access_token']


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET /api/search-users?q=mike
    Returns up to 10 matching users from Microsoft Graph (displayName, mail, jobTitle).
    Requires app registration with User.Read.All application permission.
    Env vars: GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET
    """
    query = (req.params.get('q') or '').strip()

    if len(query) < 2:
        return func.HttpResponse(
            json.dumps({'users': []}),
            mimetype='application/json',
            status_code=200
        )

    try:
        token = get_graph_token()

        # Search by displayName prefix using $filter or $search
        encoded_query = urllib.parse.quote(query)
        graph_url = (
            f'https://graph.microsoft.com/v1.0/users'
            f'?$filter=startswith(displayName,\'{encoded_query}\') or startswith(givenName,\'{encoded_query}\')'
            f'&$select=id,displayName,mail,jobTitle,department'
            f'&$top=10'
            f'&$orderby=displayName'
        )

        graph_req = urllib.request.Request(graph_url)
        graph_req.add_header('Authorization', f'Bearer {token}')
        graph_req.add_header('ConsistencyLevel', 'eventual')

        with urllib.request.urlopen(graph_req, timeout=10) as resp:
            data = json.loads(resp.read())
            users = [
                {
                    'id': u.get('id', ''),
                    'displayName': u.get('displayName', ''),
                    'mail': u.get('mail', ''),
                    'jobTitle': u.get('jobTitle', ''),
                    'department': u.get('department', ''),
                }
                for u in data.get('value', [])
            ]

        return func.HttpResponse(
            json.dumps({'users': users}),
            mimetype='application/json',
            status_code=200
        )

    except ValueError as e:
        return func.HttpResponse(
            json.dumps({'error': str(e), 'users': []}),
            mimetype='application/json',
            status_code=500
        )
    except Exception as e:
        print(f'Graph search error: {e}')
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': 'Search unavailable', 'users': []}),
            mimetype='application/json',
            status_code=500
        )
