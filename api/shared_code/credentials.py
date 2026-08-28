"""
One credential vocabulary across Bullhorn and Symplr.

The two systems name the same credential differently, and neither is wrong —
they are just different books:

    Bullhorn   View_JobOrder.customText2   "Registered Nurse - ER"
                                           "Physical Therapist"
                                           "Certified Medical Assistant"
    Symplr     lt_order.nursetype          "RN"
                                           "PT"
                                           "CNA"

Bullhorn also folds the specialty into the same field after a " - ", so the
credential has to be split off before it can be compared.

Without this, filtering to RN returns Symplr rows only and filtering to
"Registered Nurse" returns Bullhorn rows only — the filter looks like it works
while silently dropping an entire source. Both sides normalise to the short
form, which is what people actually say.

Bullhorn carries ~200 distinct credential strings, so this covers the ones
that appear in volume and passes anything else through cleaned-up rather than
forcing it into a bucket. An unmapped credential is still filterable, it just
appears under its own name.
"""

import re


# Canonical short form -> the spellings each system uses for it.
# Longest match wins, so "Registered Respiratory Therapist" is not eaten by
# "Registered Nurse" or by "Respiratory Therapist".
_ALIASES = {
    'RN':   ['registered nurse', 'rn'],
    'LPN':  ['licensed practical nurse', 'lpn', 'lvn'],
    'CNA':  ['certified nursing assistant', 'certified nurse assistant', 'cna'],
    'NP':   ['nurse practitioner', 'np'],
    'CRNA': ['certified registered nurse anesthetist', 'crna'],
    'PA':   ['physician assistant', 'pa'],
    'MD':   ['physician', 'md', 'do'],
    'PT':   ['physical therapist', 'pt'],
    'PTA':  ['physical therapist assistant', 'pta'],
    'OT':   ['occupational therapist', 'ot'],
    'COTA': ['certified occupational therapy assistant', 'cota'],
    'SLP':  ['speech language pathologist', 'speech-language pathologist',
             'speech therapist', 'slp'],
    'SLPA': ['speech language pathology assistant', 'slpa'],
    'RRT':  ['registered respiratory therapist', 'rrt'],
    'RT':   ['respiratory therapist', 'rt'],
    'CMA':  ['certified medical assistant', 'cma'],
    'MA':   ['medical assistant', 'ma'],
    'PCT':  ['patient care technician', 'patient care tech', 'pct'],
    'PCA':  ['patient care assistant', 'pca'],
    'Surgical Tech':   ['surgical technologist', 'surgical tech', 'or tech', 'scrub tech'],
    'Sterile Proc':    ['sterile processing technician', 'sterile processing tech', 'spd tech'],
    'Pharmacy Tech':   ['pharmacy technician', 'pharmacy tech'],
    'Phlebotomist':    ['phlebotomist', 'phlebotomy tech'],
    'CT Tech':         ['ct technologist', 'ct tech'],
    'MRI Tech':        ['mri technologist', 'mri tech'],
    'X-Ray Tech':      ['x-ray technologist', 'x-ray tech', 'xray tech', 'radiologic tech',
                        'rad tech'],
    'Ultrasound Tech': ['ultrasound technologist', 'ultrasound tech', 'sonographer'],
    'Echo Tech':       ['echo technologist', 'echo tech'],
    'Cath Lab Tech':   ['cath lab technologist', 'cath lab tech'],
    'Lab Tech':        ['medical laboratory technician', 'medical lab tech', 'med tech',
                        'laboratory technician', 'lab tech'],
    'Histology Tech':  ['histology technician', 'histology tech'],
    'Mental Health Tech': ['mental health technician', 'mental health tech'],
    'Social Worker':   ['licensed clinical social worker', 'social worker', 'lcsw', 'msw', 'sw'],
    'Psychologist':    ['psychologist', 'school psychologist', 'school psych', 'psycol'],
    'BCBA':            ['board certified behavior analyst', 'bcba'],
    'RBT':             ['registered behavior technician', 'behavior technician', 'rbt'],
    'Dietitian':       ['registered dietitian', 'dietitian'],
    'Coder':           ['medical coder', 'coding auditor', 'coder'],
    'CDI':             ['cdi specialist', 'clinical documentation specialist'],
    'Paraprofessional': ['paraprofessional', 'para'],
    'DSP':             ['direct support professional', 'dsp'],
    'Teacher':         ['special ed teacher', 'special ed instructor', 'sub teacher', 'teacher'],
    'Patient Svc Rep': ['patient service representative', 'patient service rep', 'psr'],
}

# Built longest-first so a specific spelling wins over a substring of it.
_LOOKUP = sorted(
    ((alias, canon) for canon, aliases in _ALIASES.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)

# Which service line a canonical credential belongs to.
_SERVICE_LINE = {
    'Nursing':            {'RN', 'LPN', 'CNA'},
    'Advanced Practices': {'NP', 'CRNA', 'PA', 'MD'},
    'Allied': {
        'PT', 'PTA', 'OT', 'COTA', 'SLP', 'SLPA', 'RRT', 'RT', 'CMA', 'MA',
        'Surgical Tech', 'Sterile Proc', 'Pharmacy Tech', 'Phlebotomist',
        'CT Tech', 'MRI Tech', 'X-Ray Tech', 'Ultrasound Tech', 'Echo Tech',
        'Cath Lab Tech', 'Lab Tech', 'Histology Tech', 'Dietitian',
        'Social Worker', 'Psychologist', 'BCBA', 'RBT', 'Mental Health Tech',
    },
    'Non-Clinical': {
        'PCT', 'PCA', 'Coder', 'CDI', 'Paraprofessional', 'DSP', 'Teacher',
        'Patient Svc Rep',
    },
}
_LINE_OF = {cred: line for line, creds in _SERVICE_LINE.items() for cred in creds}


def split_credential(raw):
    """Bullhorn packs 'Credential - Specialty' into one field. Return the
    credential half; Symplr values pass through untouched."""
    s = (raw or '').strip()
    if not s:
        return ''
    return s.split(' - ', 1)[0].strip() if ' - ' in s else s


def normalize(raw):
    """Canonical short form for a credential, or the cleaned original.

    Unmapped values are returned as-is rather than bucketed, so a credential
    nobody has mapped yet is still filterable under its own name.
    """
    cred = split_credential(raw)
    if not cred:
        return ''
    key = re.sub(r'[^a-z0-9 /-]', '', cred.lower()).strip()
    key = re.sub(r'\s+', ' ', key)
    if not key:
        return ''
    for alias, canon in _LOOKUP:
        if key == alias:
            return canon
    # Fall back to a contained match so "Registered Nurse II" still lands on RN,
    # but only on a word boundary — "Coder" must not match inside "Recoder".
    for alias, canon in _LOOKUP:
        if len(alias) > 3 and re.search(r'\b' + re.escape(alias) + r'\b', key):
            return canon
    return cred


def service_line(raw):
    """Service line for a credential: Nursing / Advanced Practices / Allied /
    Non-Clinical, or 'Other' when the credential is unmapped."""
    return _LINE_OF.get(normalize(raw), 'Other')
