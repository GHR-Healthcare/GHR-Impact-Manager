"""
Rebuild the endpoint payload caches, off the request path.

Modelled on ghr-salespulse's placement-cache/refresh: a scheduler calls this on
a timer with an API key, and the app's own Data-health UI can call it as a
signed-in user. The expensive work then happens here, on a schedule nobody is
waiting on, instead of inside the request that wants to render a chart.

    GET  /api/cache/refresh              -- what is cached, and how old
    POST /api/cache/refresh              -- rebuild everything
    POST /api/cache/refresh?key=trend-data   -- rebuild one

Auth is deliberately NOT require_allowed_domain: a timer has no signed-in user
and no email domain to check. It accepts either a signed-in principal from the
app or CACHE_REFRESH_API_KEY from the scheduler, which is the same split
salespulse uses.
"""
import json
import os
import time

import azure.functions as func

from shared_code.auth import current_user_email
from shared_code.data_source import is_non_msp
from shared_code.endpoint_cache import cache_key, write_cache, cache_status


# What can be rebuilt. Each entry names the builder to call and the book it
# belongs to; the builders are imported lazily so one broken endpoint cannot
# stop the others from refreshing.
REFRESHABLE = ('trend-data', 'financial-data')


def _build(route):
    """Run the live build for one route and return its payload dict."""
    if route == 'trend-data':
        from GetTrendData import _bullhorn_trend_data, _symplr_trend_data
        assignments, weekly_revenue, errors = [], [], []
        for label, fn in (('bullhorn', _bullhorn_trend_data), ('symplr', _symplr_trend_data)):
            try:
                part = fn()
                assignments.extend(part['assignments'])
                weekly_revenue.extend(part['weekly_revenue'])
                for e in (part.get('errors') or []):
                    errors.append(e)
            except Exception as e:
                errors.append(f'{label}: {e}')
        return {'assignments': assignments, 'pending': [],
                'weekly_revenue': weekly_revenue, 'errors': errors}, errors

    if route == 'financial-data':
        from GetFinancialData import _bullhorn_financial_data, _symplr_financial_data
        # The same default window main() uses when no month params are given --
        # 13 months back through the end of the current month. Kept verbatim so
        # the refresh builds exactly what the request path would cache.
        date_from_sql = "DATEADD(MONTH, -13, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"
        date_to_sql = "DATEADD(MONTH, 1, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"
        monthly, errors = [], []
        for label, fn in (('bullhorn', _bullhorn_financial_data),
                          ('symplr', _symplr_financial_data)):
            try:
                monthly.extend(fn(date_from_sql, date_to_sql))
            except Exception as e:
                errors.append(f'{label}: {e}')
        return {'monthlyData': monthly}, errors

    raise ValueError(f'unknown cache key: {route}')


def main(req: func.HttpRequest) -> func.HttpResponse:
    # Status is readable by anyone who can reach the app; rebuilding is not.
    if req.method == 'GET':
        return func.HttpResponse(
            json.dumps({'entries': cache_status(),
                        'refreshable': list(REFRESHABLE)}, default=str),
            mimetype='application/json', status_code=200)

    supplied = (req.headers.get('x-api-key')
                or req.params.get('apiKey') or '')
    expected = os.environ.get('CACHE_REFRESH_API_KEY')
    user = current_user_email(req)
    # A signed-in user OR the scheduler's key. The key is compared only when one
    # is configured, so an unset setting cannot turn into an open endpoint.
    if not user and not (expected and supplied and supplied == expected):
        return func.HttpResponse(json.dumps({'error': 'unauthorized'}),
                                 mimetype='application/json', status_code=401)

    # This cache is per-book, and an instance only knows its own book, so a
    # refresh here rebuilds THIS instance's entries. The non-MSP app and the
    # MSP app each need their own scheduled call.
    book = 'non_msp' if is_non_msp() else 'msp'
    if book != 'non_msp':
        return func.HttpResponse(
            json.dumps({'skipped': 'caching is currently wired for the non-MSP book only',
                        'book': book}),
            mimetype='application/json', status_code=200)

    only = (req.params.get('key') or '').strip()
    routes = [only] if only else list(REFRESHABLE)
    results = []
    for route in routes:
        started = time.time()
        try:
            payload, errors = _build(route)
            ms = int((time.time() - started) * 1000)
            if errors:
                # Never overwrite a good cache with a partial build -- a failed
                # source would otherwise be served as fact until the next run.
                results.append({'key': route, 'ok': False, 'ms': ms, 'errors': errors,
                                'note': 'kept the previous cache'})
                continue
            wrote = write_cache(cache_key(route, book), payload, build_ms=ms,
                                refreshed_by=user or 'scheduler')
            results.append({'key': route, 'ok': bool(wrote), 'ms': ms})
        except Exception as e:
            results.append({'key': route, 'ok': False,
                            'ms': int((time.time() - started) * 1000), 'errors': [str(e)]})

    status = 200 if all(r.get('ok') for r in results) else 207
    return func.HttpResponse(json.dumps({'book': book, 'results': results}, default=str),
                             mimetype='application/json', status_code=status)
