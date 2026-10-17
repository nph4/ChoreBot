from django.conf import settings
from django.db import models


class ChoreTemplate(models.Model):
    """A recurring SMART chore definition that dated Chore instances are generated from."""

    title = models.CharField(max_length=200)
    area = models.CharField(max_length=100, blank=True)
    done_when = models.CharField(
        max_length=300, help_text='What "done" looks like, e.g. "counters clear and wiped".'
    )
    estimated_minutes = models.PositiveSmallIntegerField(default=10)
    every_days = models.PositiveSmallIntegerField(default=7)
    default_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['area', 'title']
        constraints = [
            models.CheckConstraint(condition=models.Q(every_days__gte=1), name='every_days_at_least_1'),
        ]

    def __str__(self):
        return f'{self.area}: {self.title}' if self.area else self.title


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
