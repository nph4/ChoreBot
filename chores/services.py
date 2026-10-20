from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import AwayPeriod, Chore, ChoreTemplate, PickupSchedule

PICKUP_CHORES = {
    PickupSchedule.GARBAGE: ('Take out garbage', 'Garbage bin at the curb'),
    PickupSchedule.RECYCLING: ('Take out recycling', 'Recycling bin at the curb'),
    PickupSchedule.COMPOST: ('Take out compost/yard waste', 'Compost/yard waste bin at the curb'),
}


def away_periods(on_or_after):
    """Away periods that haven't ended by on_or_after, in date order."""
    return list(AwayPeriod.objects.filter(end__gte=on_or_after))


def shift_out_of_away(day, schedule, periods):
    """Move a due date out of any away period.

    A pickup due while away is skipped: it moves to the first pickup after the
    period. Anything else moves to the day after the period ends.
    """
    moved = True
    while moved:
        moved = False
        for period in periods:
            if period.start <= day <= period.end:
                after = period.end + timedelta(days=1)
                day = schedule.next_due_date(after) if schedule is not None else after
                moved = True
    return day


def _rule_due_date(template, schedule, today):
    """next_due_date() before away periods are applied."""
    last = (
        template.chores.filter(completed_at__isnull=False).order_by('-completed_at').first()
    )
    if schedule is not None:
        if last is None:
            return schedule.next_due_date(today)
        # Move past both the cycle just finished and the day it was done (or skipped), so
        # an early or late completion never re-schedules the same pickup.
        after = max(last.due_date, timezone.localdate(last.completed_at)) + timedelta(days=1)
        return schedule.next_due_date(after)
    start = max(today, template.start_date) if template.start_date else today
    if template.is_fixed_days:
        if last is None:
            return template.next_fixed_day(start - timedelta(days=1))
        # Like pickups: never land on the day just done or the one it was due.
        return template.next_fixed_day(max(last.due_date, timezone.localdate(last.completed_at)))
    if last is None:
        return start
    if template.is_one_off:
        return None
    return timezone.localdate(last.completed_at) + timedelta(days=template.every_days)


def not_before_today(template, due, today):
    """A due date moved up to today if it's in the past; fixed-day chores go to their next day from today."""
    if due >= today:
        return due
    if template.is_fixed_days:
        return template.next_fixed_day(today - timedelta(days=1))
    return today


def next_due_date(template, today):
    """Due date for a template's next chore, or None if there is no next one.

    Pickup templates follow their PickupSchedule, and fixed-day templates their
    weekdays (the first one after the later of the last due date and the day it
    was closed). Others are due on today (or start_date, if later) when never
    completed, otherwise every_days after the last completion or skip. A completed one-off has no next chore. Dates that
    fall in an away period are moved out of it (see shift_out_of_away).
    """
    schedule = PickupSchedule.objects.filter(template=template).first()
    due = _rule_due_date(template, schedule, today)
    if due is None:
        return None
    return shift_out_of_away(due, schedule, away_periods(due))


def generate_due_chores(today=None):
    """Create an open Chore for every active template that lacks one. Safe to call repeatedly."""
    today = today or timezone.localdate()
    created = []
    has_open = Chore.objects.filter(template=OuterRef('pk'), completed_at__isnull=True)
    for template in ChoreTemplate.objects.filter(active=True).exclude(Exists(has_open)):
        due_date = next_due_date(template, today)
        if due_date is None:
            continue
        try:
            with transaction.atomic():
                created.append(
                    Chore.objects.create(
                        template=template,
                        assignee=template.default_assignee,
                        due_date=due_date,
                    )
                )
        except IntegrityError:
            # Another request created the open chore first.
            pass
    return created


def complete_chore(chore, user):
    """Mark a chore done and schedule the template's next occurrence."""
    if chore.is_done:
        return
    chore.completed_at = timezone.now()
    chore.completed_by = user
    chore.save(update_fields=['completed_at', 'completed_by'])
    generate_due_chores()


def skip_chore(chore, user):
    """Close a chore without doing it. The next one is scheduled from today, as if it were done."""
    if chore.is_done or chore.template.is_one_off:
        return
    chore.completed_at = timezone.now()
    chore.completed_by = user
    chore.skipped = True
    chore.save(update_fields=['completed_at', 'completed_by', 'skipped'])
    generate_due_chores()


UNDO_WINDOW = timedelta(minutes=10)


@transaction.atomic
def undo_close(chore):
    """Reopen a chore that was just done or skipped. Returns False if it's too late to undo.

    Only the template's latest closed chore can be reopened, within UNDO_WINDOW.
    The open chore that closing it scheduled is deleted first, keeping one open
    chore per template.
    """
    if chore.completed_at is None or timezone.now() - chore.completed_at > UNDO_WINDOW:
        return False
    latest = chore.template.chores.filter(completed_at__isnull=False).order_by('-completed_at').first()
    if latest != chore:
        return False
    chore.template.chores.filter(completed_at__isnull=True).delete()
    chore.completed_at = None
    chore.completed_by = None
    chore.skipped = False
    chore.save(update_fields=['completed_at', 'completed_by', 'skipped'])
    return True


def move_chore(chore, day):
    """Set an open chore's due date by hand. Away periods and frequency changes leave it there."""
    chore.due_date = day
    chore.moved_by_hand = True
    chore.save(update_fields=['due_date', 'moved_by_hand'])


def assign_chore(chore, user, include_future=False):
    """Assign an open chore (user may be None). include_future also makes them the template's default."""
    chore.assignee = user
    chore.save(update_fields=['assignee'])
    if include_future:
        chore.template.default_assignee = user
        chore.template.save(update_fields=['default_assignee'])


@transaction.atomic
def save_pickup_schedules(schedules):
    """Replace the pickup setup.

    `schedules` maps stream -> (every_weeks, next_pickup, am_pickup). Streams left
    out (e.g. compost removed) keep their schedule row but have their chore
    template deactivated, so re-adding the bin reuses it. Open chores
    for changed bins are dropped so they regenerate on the new schedule.
    """
    for stream, (every_weeks, next_pickup, am_pickup) in schedules.items():
        title, done_when = PICKUP_CHORES[stream]
        schedule = PickupSchedule.objects.filter(stream=stream).select_related('template').first()
        if schedule is None:
            template = ChoreTemplate.objects.create(
                title=title, area='Trash', done_when=done_when, estimated_minutes=5
            )
            schedule = PickupSchedule(stream=stream, template=template)
        template = schedule.template
        template.active = True
        template.every_days = 7 * every_weeks
        template.save(update_fields=['active', 'every_days'])
        schedule.every_weeks = every_weeks
        schedule.next_pickup = next_pickup
        schedule.am_pickup = am_pickup
        schedule.save()
        template.chores.filter(completed_at__isnull=True).delete()

    for schedule in PickupSchedule.objects.exclude(stream__in=schedules).select_related('template'):
        schedule.template.chores.filter(completed_at__isnull=True).delete()
        schedule.template.active = False
        schedule.template.save(update_fields=['active'])

    generate_due_chores()


def active_pickup_schedules():
    """Pickup schedules whose bin is currently in use, keyed by stream."""
    return {
        s.stream: s for s in PickupSchedule.objects.filter(template__active=True).select_related('template')
    }


def create_task(*, title, area, done_when, estimated_minutes, every_days, start_date, assignee,
                weekdays='', every_weeks=1):
    """Create a custom chore template (every_days=None and no weekdays for one-off) and its first chore."""
    template = ChoreTemplate.objects.create(
        title=title, area=area, done_when=done_when, estimated_minutes=estimated_minutes,
        every_days=every_days, weekdays=weekdays, every_weeks=every_weeks, start_date=start_date,
        default_assignee=assignee,
    )
    generate_due_chores()
    return template


@transaction.atomic
def update_template(template, *, title, area, done_when, estimated_minutes, assignee,
                    every_days=None, weekdays='', every_weeks=1, due_date=None, today=None):
    """Save edits to a chore and bring its open chore in line.

    A new due_date moves the open chore by hand. Otherwise a new repeat rule
    (every_days, weekdays or every_weeks) reschedules it from the last
    completion, but never into the past, and not if it was moved by hand. A new
    fixed-day rule counts its weeks from due_date (or today). Pickup templates
    ignore the repeat fields and due_date. A new assignee becomes the default
    and takes over the open chore.
    """
    today = today or timezone.localdate()
    is_pickup = PickupSchedule.objects.filter(template=template).exists()
    rule = (every_days, weekdays, every_weeks if weekdays else 1)
    frequency_changed = not is_pickup and rule != (template.every_days, template.weekdays, template.every_weeks)
    assignee_changed = assignee != template.default_assignee
    template.title, template.area, template.done_when = title, area, done_when
    template.estimated_minutes, template.default_assignee = estimated_minutes, assignee
    if frequency_changed:
        template.every_days, template.weekdays, template.every_weeks = rule
        if weekdays:
            template.start_date = due_date or today
    template.save()

    open_chore = template.chores.filter(completed_at__isnull=True).first()
    if open_chore is not None:
        if assignee_changed:
            open_chore.assignee = assignee
            open_chore.save(update_fields=['assignee'])
        if not is_pickup and due_date is not None and due_date != open_chore.due_date:
            move_chore(open_chore, due_date)
        elif frequency_changed and not open_chore.moved_by_hand:
            due = next_due_date(template, today)
            if due is not None:
                open_chore.due_date = not_before_today(template, due, today)
                open_chore.save(update_fields=['due_date'])
    generate_due_chores(today)  # e.g. a finished one-off that now repeats


@transaction.atomic
def remove_template(template):
    """Turn a chore off: deactivate it and drop its open chore. History is kept."""
    template.chores.filter(completed_at__isnull=True).delete()
    template.active = False
    template.save(update_fields=['active'])


@transaction.atomic
def enable_template(template, today=None):
    """Turn a chore back on. Its next one is due from today, so it never returns overdue."""
    today = today or timezone.localdate()
    template.active = True
    template.save(update_fields=['active'])
    if template.chores.filter(completed_at__isnull=True).exists():
        return
    due = next_due_date(template, today)
    if due is None:  # a finished one-off
        return
    if due < today:
        due = shift_out_of_away(not_before_today(template, due, today), None, away_periods(today))
    Chore.objects.create(template=template, assignee=template.default_assignee, due_date=due)


@transaction.atomic
def add_away_period(start, end, today=None):
    """Record an away period and move open chores due in it (from today on) out of it."""
    today = today or timezone.localdate()
    period = AwayPeriod.objects.create(start=start, end=end)
    periods = away_periods(max(start, today))
    schedules = {s.template_id: s for s in PickupSchedule.objects.all()}
    for chore in Chore.objects.filter(
        completed_at__isnull=True, due_date__gte=max(start, today), due_date__lte=end
    ).select_related('template'):
        chore.due_date = shift_out_of_away(chore.due_date, schedules.get(chore.template_id), periods)
        chore.save(update_fields=['due_date'])
    return period


def is_away(day):
    return AwayPeriod.objects.filter(start__lte=day, end__gte=day).exists()


def _projected_after(chore, schedule, today):
    """Due date of the occurrence after an open chore, assuming it's done on time (or today if late)."""
    template = chore.template
    if schedule is not None:
        return schedule.next_due_date(max(chore.due_date, today) + timedelta(days=1))
    if template.is_fixed_days:
        return template.next_fixed_day(max(chore.due_date, today))
    if template.is_one_off:
        return None
    return max(chore.due_date, today) + timedelta(days=template.every_days)


def calendar_entries(start, end, assignee=None, today=None):
    """Chores to show on a calendar between start and end (inclusive), keyed by date.

    Each entry is (kind, template, chore_or_None, assignee), where kind is 'done'
    or 'skipped' (shown on the day it was closed), 'open', 'overdue', or
    'projected' (a future repeat computed from the template's rule, moved out of
    away periods like next_due_date(); it doesn't exist yet). Pass assignee to
    keep only that person's entries.
    """
    today = today or timezone.localdate()
    entries = {}

    def add(day, kind, template, chore, who):
        if start <= day <= end and (assignee is None or who == assignee):
            entries.setdefault(day, []).append((kind, template, chore, who))

    for chore in Chore.objects.filter(
        completed_at__date__gte=start - timedelta(days=1), completed_at__date__lte=end + timedelta(days=1)
    ).select_related('template', 'completed_by', 'assignee'):
        kind = 'skipped' if chore.skipped else 'done'
        add(timezone.localdate(chore.completed_at), kind, chore.template, chore, chore.assignee)

    schedules = {s.template_id: s for s in PickupSchedule.objects.all()}
    periods = away_periods(today)
    for chore in Chore.objects.filter(completed_at__isnull=True, template__active=True).select_related(
        'template', 'assignee', 'template__default_assignee'
    ):
        add(chore.due_date, 'overdue' if chore.due_date < today else 'open', chore.template, chore, chore.assignee)
        schedule = schedules.get(chore.template_id)
        day = _projected_after(chore, schedule, today)
        while day is not None and (day := shift_out_of_away(day, schedule, periods)) <= end:
            add(day, 'projected', chore.template, None, chore.template.default_assignee)
            if schedule is not None:
                day = schedule.next_due_date(day + timedelta(days=1))
            elif chore.template.is_fixed_days:
                day = chore.template.next_fixed_day(day)
            else:
                day += timedelta(days=chore.template.every_days)

    for day_entries in entries.values():
        day_entries.sort(key=lambda e: (e[0] == 'projected', e[1].title))
    return entries
