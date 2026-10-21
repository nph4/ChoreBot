"""Numbers for the Stats page, worked out from the chore history (nothing is stored).

Every function takes the range as start..today (inclusive). A chore counts in the
range by its final due date. One that's still open and due today hasn't had its
chance yet, so it's left out of the on-time figures until tomorrow.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from .models import AwayPeriod, Chore

# Nord frost, aurora and snow colors for people in the weekly chart, in order.
PERSON_COLORS = ['#88c0d0', '#a3be8c', '#ebcb8b', '#b48ead', '#d08770', '#5e81ac', '#8fbcbb', '#bf616a']


def outcome(chore, today):
    """'on_time', 'late', 'skipped', 'overdue' (open and past due), or None (open and not yet due)."""
    if chore.completed_at is None:
        return 'overdue' if chore.due_date < today else None
    if chore.skipped:
        return 'skipped'
    return 'on_time' if timezone.localdate(chore.completed_at) <= chore.due_date else 'late'


def days_late(chore):
    return max(0, (timezone.localdate(chore.completed_at) - chore.due_date).days)


def _due_in_range(start, today):
    chores = Chore.objects.filter(due_date__range=(start, today)).select_related('template', 'completed_by')
    return [(chore, kind) for chore in chores if (kind := outcome(chore, today)) is not None]


def _share(part, whole):
    return round(100 * part / whole) if whole else None


def outcome_counts(start, today):
    """How the chores due in the range ended: counts, total and the on-time share (a percentage or None)."""
    counts = {'on_time': 0, 'late': 0, 'skipped': 0, 'overdue': 0}
    for _, kind in _due_in_range(start, today):
        counts[kind] += 1
    total = sum(counts.values())
    return {**counts, 'total': total, 'on_time_pct': _share(counts['on_time'], total),
            'widths': {kind: 100 * n / total if total else 0 for kind, n in counts.items()}}


def household_streak(start, today):
    """(current, best): days in a row that ended with nothing overdue.

    Today doesn't count until it's over, and away days neither count nor break a
    streak, nor do days before the first chore was due. The current streak may
    reach back before start (up to a year); the best is the longest run within
    the range.
    """
    yesterday = today - timedelta(days=1)
    earliest = Chore.objects.order_by('due_date').values_list('due_date', flat=True).first()
    if earliest is None:
        return 0, 0
    first = max(min(start, today - timedelta(days=365)), earliest)
    start = max(start, earliest)
    late_days = set()
    for chore in Chore.objects.filter(due_date__lte=yesterday).exclude(completed_at__date__lt=first - timedelta(days=1)):
        closed = timezone.localdate(chore.completed_at) if chore.completed_at else today
        day = max(chore.due_date, first)
        while day < closed and day <= yesterday:
            late_days.add(day)
            day += timedelta(days=1)
    away = set()
    for period in AwayPeriod.objects.filter(end__gte=first, start__lte=yesterday):
        day = max(period.start, first)
        while day <= min(period.end, yesterday):
            away.add(day)
            day += timedelta(days=1)

    current = 0
    day = yesterday
    while day >= first and day not in late_days:
        current += day not in away
        day -= timedelta(days=1)

    best = run = 0
    day = start
    while day <= yesterday:
        if day in late_days:
            run = 0
        elif day not in away:
            run += 1
            best = max(best, run)
        day += timedelta(days=1)
    return current, best


@dataclass
class Week:
    start: object
    end: object
    minutes: int = 0
    by_person: dict = field(default_factory=dict)  # person name -> minutes


def weekly_minutes(start, today):
    """Estimated minutes of chores done (not skipped) in each 7-day block of the range, oldest first.

    Blocks end on today, so the range should be a whole number of weeks. Done
    chores count on the day they were done, for whoever ticked them.
    """
    weeks = []
    end = today
    while end >= start:
        weeks.append(Week(start=end - timedelta(days=6), end=end))
        end -= timedelta(days=7)
    weeks.reverse()
    for chore in _done_between(start, today):
        done = timezone.localdate(chore.completed_at)
        week = weeks[(done - weeks[0].start).days // 7]
        name = _person(chore)
        week.minutes += chore.template.estimated_minutes
        week.by_person[name] = week.by_person.get(name, 0) + chore.template.estimated_minutes
    return weeks


def _done_between(start, today):
    return [
        chore for chore in Chore.objects.filter(
            completed_at__isnull=False, skipped=False,
            completed_at__date__gte=start - timedelta(days=1), completed_at__date__lte=today + timedelta(days=1),
        ).select_related('template', 'completed_by')
        if start <= timezone.localdate(chore.completed_at) <= today
    ]


def _person(chore):
    return chore.completed_by.get_username() if chore.completed_by else 'Someone'


def by_person(start, today):
    """Per person who ticked chores done in the range: done count, minutes and on-time share, most minutes first."""
    people = {}
    for chore in _done_between(start, today):
        row = people.setdefault(_person(chore), {'name': _person(chore), 'done': 0, 'minutes': 0, 'on_time': 0})
        row['done'] += 1
        row['minutes'] += chore.template.estimated_minutes
        row['on_time'] += outcome(chore, today) == 'on_time'
    rows = sorted(people.values(), key=lambda r: (-r['minutes'], r['name']))
    for row in rows:
        row['on_time_pct'] = _share(row['on_time'], row['done'])
    return rows


def by_chore(start, today):
    """Per chore due in the range: done/due counts, on-time share, current on-time streak and average days late.

    Worst first: lowest on-time share, then most days late. The streak counts
    the chore's latest occurrences done on time, across all its history; a late,
    skipped or overdue one ends it.
    """
    rows = {}
    for chore, kind in _due_in_range(start, today):
        row = rows.setdefault(chore.template_id, {
            'template': chore.template, 'due': 0, 'done': 0, 'on_time': 0, 'late_days': 0,
        })
        row['due'] += 1
        if kind in ('on_time', 'late'):
            row['done'] += 1
            row['late_days'] += days_late(chore)
        row['on_time'] += kind == 'on_time'

    history = {}
    for chore in Chore.objects.filter(template_id__in=rows, due_date__lte=today).order_by('-due_date'):
        history.setdefault(chore.template_id, []).append(chore)
    for template_id, row in rows.items():
        row['on_time_pct'] = _share(row['on_time'], row['due'])
        row['avg_late'] = round(row['late_days'] / row['done'], 1) if row['done'] else None
        streak = 0
        for chore in history.get(template_id, []):
            kind = outcome(chore, today)
            if kind is None:
                continue
            if kind != 'on_time':
                break
            streak += 1
        row['streak'] = streak
    return sorted(rows.values(), key=lambda r: (r['on_time_pct'], -(r['avg_late'] or 0), r['template'].title))


def build(start, today):
    """Everything the Stats page shows."""
    weeks = weekly_minutes(start, today)
    people = sorted({name for week in weeks for name in week.by_person})
    colors = {name: PERSON_COLORS[i % len(PERSON_COLORS)] for i, name in enumerate(people)}
    tallest = max((week.minutes for week in weeks), default=0) or 1
    for week in weeks:
        week.segments = [
            (name, minutes, colors[name], 100 * minutes / tallest) for name, minutes in sorted(week.by_person.items())
        ]
    current, best = household_streak(start, today)
    return {
        'outcomes': outcome_counts(start, today),
        'streak': current,
        'best_streak': best,
        'weeks': weeks,
        'minutes_per_week': round(sum(week.minutes for week in weeks) / len(weeks)) if weeks else 0,
        'legend': list(colors.items()),
        'people': by_person(start, today),
        'chores': by_chore(start, today),
    }
