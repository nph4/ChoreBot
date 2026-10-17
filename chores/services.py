from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import Chore, ChoreTemplate


def next_due_date(template, today):
    """Due today if never completed; otherwise every_days after the last completion."""
    last = (
        template.chores.filter(completed_at__isnull=False).order_by('-completed_at').first()
    )
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
