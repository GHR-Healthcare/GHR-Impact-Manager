"""
Canonicalisation for VNDLY [Reason for Modification].

VNDLY stores this as free-ish text and the same reason arrives in several
spellings, so any GROUP BY over the raw column splits one real reason across
two or more buckets. Observed in the live warehouse:

    Terminated - attendance                    40   |  Terminated-attendance                     18
    Professional resigned - no notice given    16   |  Professional Resigned-no notice given     15
    Professional Resigned-provided notice      12   |  Professional resigned - provided notice    3
    Agency cancelled - other - see notes        7   |  Agency cancelled - other- see notes         2
    Agency cancelled - professional became…     3   |  Agency cancelled -professional became…      1
    Hospital cancelled-workforce reduction      2   |  Hospital cancelled - workforce reduction    1
    Assignment complete                        82   |  Assignment Completed                      38

Most collapse under whitespace/case normalisation around the hyphen. A few
are genuine word-form differences ("complete" vs "completed") and need an
explicit alias.

Normalising on read rather than in the warehouse: the staging tables are
reloaded from VNDLY, so a fix written into them would be overwritten on the
next load. The durable fix belongs upstream in VNDLY's picklist.
"""

import re


# Word-form differences that survive punctuation/case normalisation.
# Keys and values are both in normalised form (see _normalize_key).
_ALIASES = {
    'assignment completed': 'assignment complete',
    'assignment completed - extension offered': 'assignment complete - extension offered',
}

# Display form for each canonical key. Anything not listed falls back to
# sentence-casing the key, which is right for the long tail.
_DISPLAY = {
    'organizational unit change': 'Organizational Unit Change',
    'date extension': 'Date Extension',
    'assignment complete': 'Assignment Complete',
    'assignment complete - extension offered': 'Assignment Complete - Extension Offered',
    'health system request': 'Health System Request',
    'admin configuration': 'Admin Configuration',
    'other': 'Other',
    'vendor/contractor request': 'Vendor/Contractor Request',
    'rate change': 'Rate Change',
    'ended in error': 'Ended in Error',
    'terminated - attendance': 'Terminated - Attendance',
    'terminated - compliance': 'Terminated - Compliance',
    'terminated - no call/no show': 'Terminated - No Call/No Show',
    'terminated - poor performance': 'Terminated - Poor Performance',
    'work has not met the performance expectations': 'Work Has Not Met the Performance Expectations',
    'professional resigned - no notice given': 'Professional Resigned - No Notice Given',
    'professional resigned - provided notice': 'Professional Resigned - Provided Notice',
    'agency cancelled - other - see notes': 'Agency Cancelled - Other (See Notes)',
    'agency cancelled - professional became unresponsive': 'Agency Cancelled - Professional Became Unresponsive',
    'hospital cancelled - workforce reduction': 'Hospital Cancelled - Workforce Reduction',
    'delayed start - compliance/onboarding incomplete': 'Delayed Start - Compliance/Onboarding Incomplete',
    'delayed start - vendor/contractor requested': 'Delayed Start - Vendor/Contractor Requested',
}


def _normalize_key(raw: str) -> str:
    """Lowercase, standardise spacing around hyphens/slashes, collapse runs."""
    s = (raw or '').strip().lower()
    if not s:
        return ''
    s = re.sub(r'\s*-\s*', ' - ', s)   # "x-y" / "x -y" / "x - y" → "x - y"
    s = re.sub(r'\s*/\s*', '/', s)     # "x / y" → "x/y"
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def canonical_reason(raw):
    """Canonical display form for a VNDLY modification reason.

    Returns None for blank input so callers can distinguish "no reason
    recorded" from a reason that normalised to something.
    """
    key = _normalize_key(raw)
    if not key:
        return None
    key = _ALIASES.get(key, key)
    if key in _DISPLAY:
        return _DISPLAY[key]
    # Long tail: sentence-case, preserving the " - " separators.
    return ' - '.join(part.strip().capitalize() for part in key.split(' - '))


# Coarse buckets, useful for grouping outcomes on the Closed stage and for
# categorising a delay on Onboarding.
def reason_category(raw):
    key = _normalize_key(raw)
    key = _ALIASES.get(key, key)
    if not key:
        return None
    if key.startswith('delayed start'):
        return 'Delayed Start'
    if key.startswith('terminated') or key.startswith('work has not met'):
        return 'Terminated'
    if key.startswith('professional resigned'):
        return 'Clinician Resigned'
    if key.startswith('agency cancelled'):
        return 'Agency Cancelled'
    if key.startswith('hospital cancelled'):
        return 'Client Cancelled'
    if key.startswith('assignment complete') or key == 'date extension':
        return 'Assignment Lifecycle'
    return 'Administrative'
