from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import Chore, ChoreTemplate, PickupSchedule

PICKUP_CHORES = {
    PickupSchedule.GARBAGE: ('Take out garbage', 'Garbage bin at the curb'),
    PickupSchedule.RECYCLING: ('Take out recycling', 'Recycling bin at the curb'),
    PickupSchedule.COMPOST: ('Take out compost/yard waste', 'Compost/yard waste bin at the curb'),
}


def next_due_date(template, today):
    """Due date for a template's next chore, or None if there is no next one.

    Pickup templates follow their PickupSchedule. Others are due on today (or
    start_date, if later) when never completed, otherwise every_days after the
    last completion. A completed one-off has no next chore.
    """
    last = (
        template.chores.filter(completed_at__isnull=False).order_by('-completed_at').first()
    )
    schedule = PickupSchedule.objects.filter(template=template).first()
    if schedule is not None:
        if last is None:
            return schedule.next_due_date(today)
        # Move past both the cycle just finished and the day it was done, so an
        # early or late completion never re-schedules the same pickup.
        after = max(last.due_date, timezone.localdate(last.completed_at)) + timedelta(days=1)
        return schedule.next_due_date(after)
    if last is None:
        return max(today, template.start_date) if template.start_date else today
    if template.is_one_off:
        return None
    return timezone.localdate(last.completed_at) + timedelta(days=template.every_days)


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


def create_task(*, title, area, done_when, estimated_minutes, every_days, start_date, assignee):
    """Create a custom chore template (every_days=None for one-off) and its first chore."""
    template = ChoreTemplate.objects.create(
        title=title, area=area, done_when=done_when, estimated_minutes=estimated_minutes,
        every_days=every_days, start_date=start_date, default_assignee=assignee,
    )
    generate_due_chores()
    return template


def _projected_after(chore, schedule, today):
    """Due date of the occurrence after an open chore, assuming it's done on time (or today if late)."""
    template = chore.template
    if schedule is not None:
        return schedule.next_due_date(max(chore.due_date, today) + timedelta(days=1))
    if template.is_one_off:
        return None
    return max(chore.due_date, today) + timedelta(days=template.every_days)


def calendar_entries(start, end, assignee=None, today=None):
    """Chores to show on a calendar between start and end (inclusive), keyed by date.

    Each entry is (kind, template, chore_or_None, assignee), where kind is 'done'
    (shown on its completion day), 'open', 'overdue', or 'projected' (a future
    repeat computed from the template's rule; it doesn't exist yet). Pass
    assignee to keep only that person's entries.
    """
    today = today or timezone.localdate()
    entries = {}

    def add(day, kind, template, chore, who):
        if start <= day <= end and (assignee is None or who == assignee):
            entries.setdefault(day, []).append((kind, template, chore, who))

    for chore in Chore.objects.filter(
        completed_at__date__gte=start - timedelta(days=1), completed_at__date__lte=end + timedelta(days=1)
    ).select_related('template', 'completed_by', 'assignee'):
        add(timezone.localdate(chore.completed_at), 'done', chore.template, chore, chore.assignee)

    schedules = {s.template_id: s for s in PickupSchedule.objects.all()}
    for chore in Chore.objects.filter(completed_at__isnull=True, template__active=True).select_related(
        'template', 'assignee', 'template__default_assignee'
    ):
        add(chore.due_date, 'overdue' if chore.due_date < today else 'open', chore.template, chore, chore.assignee)
        schedule = schedules.get(chore.template_id)
        day = _projected_after(chore, schedule, today)
        while day is not None and day <= end:
            add(day, 'projected', chore.template, None, chore.template.default_assignee)
            if schedule is not None:
                day = schedule.next_due_date(day + timedelta(days=1))
            else:
                day += timedelta(days=chore.template.every_days)

    for day_entries in entries.values():
        day_entries.sort(key=lambda e: (e[0] == 'projected', e[1].title))
    return entries
