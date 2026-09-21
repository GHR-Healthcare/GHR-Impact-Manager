"""
The category axis that rate comparisons rank within.

RATE RANK compares against Bullhorn job orders, whose category axis is
`employmentType` -- Travel / Local / Remote, an engagement type. MSP books a
programme instead, which fuses engagement type with service line: B4's Program
is 'Travel Nursing' / 'Local Contract Allied Health' / 'Contract (Nursing)',
VNDLY's Labor Type is 'Nursing' / 'Per Diem Nursing'. The two vocabularies do
not meet, so the engagement type is read back out of the programme name -- but
only where the name states it. 'Contract (Nursing)', the single largest B4
bucket at 6,871 rows, could be travel or local and is left unmapped rather
than guessed: a Local seat ranked against Travel peers reads as underpaid
however well it is priced.

Derivable on 16,749 of 29,106 B4 orders (58%). The rest carry no rank, which
is the honest answer.

Lifted out of GetClosed so GetPositions can rank open jobs on the same axis.
Two copies would have drifted, and a rank that means one thing on Closed and
another on Open Jobs is worse than no rank.
"""


def rate_category(programme):
    """Engagement type read out of a booking programme name, or None."""
    p = (programme or '').lower()
    if 'travel' in p:
        return 'Travel'
    if 'local' in p:
        return 'Local'
    if 'per diem' in p or 'perdiem' in p or 'prn' in p:
        return 'Per Diem'
    if 'remote' in p:
        return 'Remote'
    return None
