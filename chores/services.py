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
    """Due date for a template's next chore.

    Pickup templates follow their PickupSchedule. Others are due today if never
    completed, otherwise every_days after the last completion.
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
        return today
    return timezone.localdate(last.completed_at) + timedelta(days=template.every_days)


def generate_due_chores(today=None):
    """Create an open Chore for every active template that lacks one. Safe to call repeatedly."""
    today = today or timezone.localdate()
    created = []
    has_open = Chore.objects.filter(template=OuterRef('pk'), completed_at__isnull=True)
    for template in ChoreTemplate.objects.filter(active=True).exclude(Exists(has_open)):
        try:
            with transaction.atomic():
                created.append(
                    Chore.objects.create(
                        template=template,
                        assignee=template.default_assignee,
                        due_date=next_due_date(template, today),
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
