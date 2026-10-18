from datetime import timedelta

from django.conf import settings
from django.db import models


class ChoreTemplate(models.Model):
    """A SMART chore definition that dated Chore instances are generated from.

    every_days=None makes it a one-off: one chore is generated and, once completed, no more.
    """

    title = models.CharField(max_length=200)
    area = models.CharField(max_length=100, blank=True)
    done_when = models.CharField(
        max_length=300, help_text='What "done" looks like, e.g. "counters clear and wiped".'
    )
    estimated_minutes = models.PositiveSmallIntegerField(default=10)
    every_days = models.PositiveSmallIntegerField(default=7, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True, help_text='First occurrence is due no earlier than this.')
    default_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['area', 'title']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(every_days__gte=1) | models.Q(every_days__isnull=True),
                name='every_days_at_least_1',
            ),
        ]

    def __str__(self):
        return f'{self.area}: {self.title}' if self.area else self.title

    @property
    def is_one_off(self):
        return self.every_days is None


class Chore(models.Model):
    """One dated occurrence of a ChoreTemplate."""

    template = models.ForeignKey(ChoreTemplate, on_delete=models.CASCADE, related_name='chores')
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='chores'
    )
    due_date = models.DateField()
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    class Meta:
        ordering = ['due_date', 'template__area', 'template__title']
        constraints = [
            models.UniqueConstraint(
                fields=['template'],
                condition=models.Q(completed_at__isnull=True),
                name='one_open_chore_per_template',
            ),
        ]

    def __str__(self):
        return f'{self.template} (due {self.due_date})'

    @property
    def is_done(self):
        return self.completed_at is not None


class PickupSchedule(models.Model):
    """Curbside pickup for one bin; drives the due dates of its linked ChoreTemplate."""

    GARBAGE = 'garbage'
    RECYCLING = 'recycling'
    COMPOST = 'compost'
    STREAM_CHOICES = [
        (GARBAGE, 'Garbage'),
        (RECYCLING, 'Recycling'),
        (COMPOST, 'Compost/yard waste'),
    ]

    stream = models.CharField(max_length=20, choices=STREAM_CHOICES, unique=True)
    every_weeks = models.PositiveSmallIntegerField(default=1)
    next_pickup = models.DateField(help_text='Any known pickup date; anchors the cycle.')
    am_pickup = models.BooleanField(help_text='AM pickups are due the day before; PM pickups the same day.')
    template = models.OneToOneField(ChoreTemplate, on_delete=models.CASCADE, related_name='pickup_schedule')

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(every_weeks__gte=1), name='every_weeks_at_least_1'),
        ]

    def __str__(self):
        return f'{self.get_stream_display()} every {self.every_weeks} week(s)'

    def next_pickup_on_or_after(self, day):
        period = 7 * self.every_weeks
        cycles = -(-(day - self.next_pickup).days // period)  # ceiling division
        return self.next_pickup + timedelta(days=cycles * period)

    def next_due_date(self, on_or_after):
        """Earliest chore due date >= on_or_after, where due = pickup day (minus one for AM)."""
        lead = timedelta(days=1 if self.am_pickup else 0)
        return self.next_pickup_on_or_after(on_or_after + lead) - lead
