"""Morning digest and evening nudge for overdue and due-today chores, sent through Apprise.

Nothing is sent on away days.
"""

import logging
from datetime import time

import apprise
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from .models import Chore, NotificationLog
from .services import generate_due_chores, is_away

logger = logging.getLogger(__name__)


def _parse_time(value):
    return time.fromisoformat(value) if value else None


def due_slot(now):
    """The notification kind whose send time has passed today, or None.

    Once the evening time passes, a morning digest that never went out is skipped
    rather than sent late, since the nudge covers the same chores.
    """
    morning = _parse_time(settings.NOTIFY_MORNING_AT)
    evening = _parse_time(settings.NOTIFY_EVENING_AT)
    if evening and now.time() >= evening:
        return NotificationLog.EVENING
    if morning and now.time() >= morning:
        return NotificationLog.MORNING
    return None


def open_chores(today):
    """Open chores that are overdue or due today, oldest first."""
    return list(
        Chore.objects.filter(completed_at__isnull=True, template__active=True, due_date__lte=today)
        .select_related('template', 'assignee')
    )


def _line(chore):
    t = chore.template
    details = ', '.join(filter(None, [t.area, f'~{t.estimated_minutes} min']))
    who = chore.assignee.get_username() if chore.assignee else 'unassigned'
    return f'- {t.title} ({details}) · {who}'


def build_message(kind, chores, today):
    """(title, Markdown body) listing chores under Overdue and Due today."""
    count = len(chores)
    noun = 'chore' if count == 1 else 'chores'
    title = f'ChoreBot: {count} {noun} today' if kind == NotificationLog.MORNING else f'ChoreBot: {count} {noun} still to do'
    sections = []
    overdue = [c for c in chores if c.due_date < today]
    due_today = [c for c in chores if c.due_date == today]
    if overdue:
        sections.append('**Overdue**\n' + '\n'.join(
            f'{_line(c)} · due {c.due_date:%a} {c.due_date.day} {c.due_date:%b}' for c in overdue
        ))
    if due_today:
        sections.append('**Due today**\n' + '\n'.join(_line(c) for c in due_today))
    return title, '\n\n'.join(sections)


def send(title, body):
    """Send to every NOTIFY_URLS destination. True only if all of them accepted it."""
    destinations = apprise.Apprise()
    added = [destinations.add(url) for url in settings.NOTIFY_URLS]
    if not all(added):
        logger.warning('%d of %d NOTIFY_URLS were not recognised by Apprise', added.count(False), len(added))
    if not len(destinations):
        return False
    return destinations.notify(title=title, body=body, body_format=apprise.NotifyFormat.MARKDOWN) and all(added)


def send_due_notification(now=None):
    """Send the current slot's message if it hasn't gone out today. Returns the new log row, or None.

    The slot is logged even when nothing is due or a destination fails, so a bad URL
    can't cause a retry every minute or duplicates at the destinations that worked.
    """
    if not settings.NOTIFY_URLS:
        return None
    now = timezone.localtime(now)
    kind = due_slot(now)
    if kind is None or NotificationLog.objects.filter(kind=kind, date=now.date()).exists():
        return None
    if is_away(now.date()):
        return None
    try:
        # Claim the slot first so two notifiers can't both send it.
        log = NotificationLog.objects.create(kind=kind, date=now.date(), chore_count=0, delivered=False)
    except IntegrityError:
        return None
    generate_due_chores(now.date())  # nobody may have opened the dashboard yet today
    chores = open_chores(now.date())
    log.chore_count = len(chores)
    if chores:
        log.delivered = send(*build_message(kind, chores, now.date()))
    log.save(update_fields=['chore_count', 'delivered'])
    return log
