from datetime import timedelta

from django.conf import settings
from django.db import models


class ChoreTemplate(models.Model):
    """A SMART chore definition that dated Chore instances are generated from.

    every_days=None makes it a one-off: one chore is generated and, once completed, no more.
    weekdays makes it repeat on fixed days of the week instead (every_days is then None).
    """

    WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    title = models.CharField(max_length=200)
    area = models.CharField(max_length=100, blank=True)
    done_when = models.CharField(
        max_length=300, help_text='What "done" looks like, e.g. "counters clear and wiped".'
    )
    estimated_minutes = models.PositiveSmallIntegerField(default=10)
    every_days = models.PositiveSmallIntegerField(default=7, null=True, blank=True)
    weekdays = models.CharField(
        max_length=7, blank=True, help_text='Fixed days as digits, Mon=0 … Sun=6, e.g. "5" for Saturdays.'
    )
    every_weeks = models.PositiveSmallIntegerField(
        default=1, help_text='With weekdays: every this many weeks, counted from the week of start_date.'
    )
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
            models.CheckConstraint(condition=models.Q(every_weeks__gte=1), name='template_every_weeks_at_least_1'),
        ]

    def __str__(self):
        return f'{self.area}: {self.title}' if self.area else self.title

    @property
    def is_one_off(self):
        return self.every_days is None and not self.weekdays

    @property
    def is_fixed_days(self):
        return bool(self.weekdays)

    @property
    def repeat_label(self):
        if self.weekdays:
            days = ', '.join(self.WEEKDAY_NAMES[int(d)] for d in self.weekdays)
            return f'Every {days}' if self.every_weeks == 1 else f'Every {self.every_weeks} weeks on {days}'
        names = {None: 'Once', 1: 'Daily', 7: 'Weekly', 30: 'Monthly'}
        return names.get(self.every_days, f'Every {self.every_days} days')

    def next_fixed_day(self, after):
        """First date strictly after `after` that falls on one of the weekdays, in a week that counts.

        Weeks start on Monday. With every_weeks > 1, the weeks that count are those a
        whole number of every_weeks after the week of start_date.
        """
        anchor = self.start_date or after
        anchor_monday = anchor - timedelta(days=anchor.weekday())
        day = after + timedelta(days=1)
        for _ in range(7 * self.every_weeks):
            weeks = (day - anchor_monday).days // 7
            if str(day.weekday()) in self.weekdays and weeks % self.every_weeks == 0:
                return day
            day += timedelta(days=1)
        raise ValueError(f'No weekday set on {self!r}')


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
    skipped = models.BooleanField(default=False, help_text='Closed without being done; completed_at is when.')
    moved_by_hand = models.BooleanField(default=False, help_text='Due date set by hand; frequency changes keep it.')

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
        """Closed, whether done or skipped."""
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


class AwayPeriod(models.Model):
    """Days nobody is home: no notifications, pickups skipped, other chores moved to the day after."""

    start = models.DateField()
    end = models.DateField()

    class Meta:
        ordering = ['start']
        constraints = [
            models.CheckConstraint(condition=models.Q(end__gte=models.F('start')), name='away_end_not_before_start'),
        ]

    def __str__(self):
        return f'Away {self.start} to {self.end}'


class NotificationLog(models.Model):
    """One row per notification slot per day, so a digest is never sent twice."""

    MORNING = 'morning'
    EVENING = 'evening'
    KIND_CHOICES = [(MORNING, 'Morning digest'), (EVENING, 'Evening nudge')]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    chore_count = models.PositiveSmallIntegerField(help_text='0 means there was nothing to send.')
    delivered = models.BooleanField(help_text='Every destination accepted the message.')

    class Meta:
        ordering = ['-date', 'kind']
        constraints = [
            models.UniqueConstraint(fields=['kind', 'date'], name='one_notification_per_kind_per_day'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.date}'
