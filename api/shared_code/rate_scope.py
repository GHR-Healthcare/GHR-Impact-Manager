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


# --- 13-week contract value -------------------------------------------------
#
# Four endpoints computed `rate * hours * 13` with no bounds at all. The source
# carries values that formula cannot survive, measured against the Bullhorn
# mirror on 2026-09-30:
#
#   category     jobs   rate>1000   max rate   avg rate   max hours/week
#   Permanent   10053         498   800000.00    7325.79            70.00
#   Travel     469752         463   420000.00      98.38         41968.00
#   Local       21755           3    90000.00      93.79           432.00
#
# Two separate faults. Permanent roles put an ANNUAL SALARY in clientBillRate
# -- the average is 7,325 against travel's 98 -- so one perm row at 800,000
# x 40 x 13 contributes $416 MILLION. And hoursPerWeek is unvalidated: 41,968
# hours in a week is not a number, it is a typo that multiplies.
#
# One bad row dominates every total it lands in. Closed "Revenue Missed" read
# $126,963.2k on the widened book; the count of rows had grown 1.6x while the
# money grew 37x, which is what gave it away -- a bigger book cannot do that.
#
# Measured over 180 days of job orders, which is what the missed side prices:
#
#   verdict                              rows   value ($M)
#   dropped: permanent                    115     10,322.8
#   dropped: rate looks like a salary     108        227.5
#   kept                                48,241      2,099.9
#   no value either way                  3,545          0.0
#
# 223 rows out of ~52,000 carried $10.5 BILLION -- five times every legitimate
# row combined. Permanent averages $89.8M per row, which is simply a salary
# multiplied by 40 hours and 13 weeks.
#
# Out of range returns None, meaning unknown, rather than a number. A KPI that
# omits a seat it cannot price is recoverable; one that reports $127M is not.

# An hourly bill rate. Locums physicians and CRNAs reach several hundred an
# hour, so the ceiling is deliberately generous -- it exists to catch salaries
# and typos, not to second-guess a real rate.
MIN_HOURLY_RATE = 1
MAX_HOURLY_RATE = 1500

# A working week. 80 allows double shifts and still excludes 432 and 41,968.
MIN_HOURS_PER_WEEK = 1
MAX_HOURS_PER_WEEK = 80

# Permanent placements earn a one-off fee, not thirteen weeks of hours, so the
# formula does not describe them whatever the inputs look like.
NON_HOURLY_CATEGORIES = {'permanent', 'perm', 'direct hire', 'full-time'}


def value_13wk(rate, hours, category=None):
    """Thirteen weeks of billing, or None when the inputs cannot mean that."""
    if category and str(category).strip().lower() in NON_HOURLY_CATEGORIES:
        return None
    try:
        r = float(rate)
        h = float(hours)
    except (TypeError, ValueError):
        return None
    if not (MIN_HOURLY_RATE <= r <= MAX_HOURLY_RATE):
        return None
    if not (MIN_HOURS_PER_WEEK <= h <= MAX_HOURS_PER_WEEK):
        return None
    return round(r * h * 13, 2)
