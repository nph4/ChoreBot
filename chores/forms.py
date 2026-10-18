from django import forms
from django.contrib.auth import get_user_model

YES_NO = [('yes', 'Yes'), ('no', 'No')]
WEEKLY, EVERY_OTHER_WEEK, CUSTOM = 'weekly', 'every_other_week', 'custom'


class AssignForm(forms.Form):
    assignee = forms.ModelChoiceField(
        queryset=get_user_model().objects.filter(is_active=True), required=False, empty_label='Unassigned'
    )
    include_future = forms.BooleanField(required=False, label='Also future ones')


class PickupSetupForm(forms.Form):
    same_schedule = forms.ChoiceField(
        label='Are all your bins picked up on the same schedule and day?',
        choices=YES_NO,
        widget=forms.RadioSelect,
    )
    has_compost = forms.ChoiceField(
        label='Do you also have compost/yard waste pickup?',
        choices=YES_NO,
        widget=forms.RadioSelect,
    )


class ScheduleForm(forms.Form):
    """One pickup schedule. Rendered once (shared) or once per bin."""

    # Hidden alternatives in the setup page would otherwise block browser submission.
    use_required_attribute = False

    frequency = forms.ChoiceField(
        label='How often?',
        choices=[(WEEKLY, 'Weekly'), (EVERY_OTHER_WEEK, 'Every other week'), (CUSTOM, 'Custom')],
        widget=forms.RadioSelect,
    )
    every_weeks = forms.IntegerField(label='Every how many weeks?', min_value=1, max_value=52, required=False)
    next_pickup = forms.DateField(
        label='Next pickup day',
        help_text='The date of an upcoming pickup; its weekday is your pickup day.',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    pickup_time = forms.ChoiceField(
        label='Pickup time',
        choices=[('am', 'Morning (AM) - due the night before'), ('pm', 'Afternoon (PM) - due that morning')],
        widget=forms.RadioSelect,
    )

    def clean(self):
        cleaned = super().clean()
        frequency = cleaned.get('frequency')
        if frequency == CUSTOM and not cleaned.get('every_weeks'):
            self.add_error('every_weeks', 'Enter how many weeks apart pickups are.')
        cleaned['weeks'] = {WEEKLY: 1, EVERY_OTHER_WEEK: 2}.get(frequency, cleaned.get('every_weeks'))
        return cleaned

    def as_schedule(self):
        """(every_weeks, next_pickup, am_pickup) for services.save_pickup_schedules."""
        c = self.cleaned_data
        return c['weeks'], c['next_pickup'], c['pickup_time'] == 'am'

    @staticmethod
    def initial_from(schedule, today):
        frequency = {1: WEEKLY, 2: EVERY_OTHER_WEEK}.get(schedule.every_weeks, CUSTOM)
        return {
            'frequency': frequency,
            'every_weeks': schedule.every_weeks if frequency == CUSTOM else None,
            'next_pickup': schedule.next_pickup_on_or_after(today),
            'pickup_time': 'am' if schedule.am_pickup else 'pm',
        }
