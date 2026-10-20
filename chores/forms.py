from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone

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
        help_text='Morning pickups are due the day before; afternoon pickups are due that morning.',
        choices=[('am', 'Morning'), ('pm', 'Afternoon')],
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


REPEAT_DAYS = {'once': None, 'daily': 1, 'weekly': 7, 'monthly': 30}


class TaskForm(forms.Form):
    """A custom chore, repeating or one-off."""

    title = forms.CharField(max_length=200)
    area = forms.CharField(max_length=100, required=False, help_text='Room or place, e.g. "Garage".')
    done_when = forms.CharField(max_length=300, label='Done when', help_text='What "done" looks like.')
    estimated_minutes = forms.IntegerField(label='Minutes', min_value=1, max_value=600, initial=15)
    repeat = forms.ChoiceField(
        choices=[('once', 'Once'), ('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly'), ('custom', 'Custom')],
        widget=forms.RadioSelect,
        initial='once',
        help_text='Repeats count from when the last one was done. Monthly means every 30 days.',
    )
    every_days = forms.IntegerField(label='Every how many days?', min_value=1, max_value=365, required=False)
    start_date = forms.DateField(label='Due', widget=forms.DateInput(attrs={'type': 'date'}))
    assignee = forms.ModelChoiceField(
        queryset=get_user_model().objects.filter(is_active=True).order_by('username'),
        required=False,
        empty_label='Unassigned',
        label='Assign to',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['start_date'].initial = timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        if 'repeat' not in self.fields:
            return cleaned
        repeat = cleaned.get('repeat')
        if repeat == 'custom':
            if not cleaned.get('every_days'):
                self.add_error('every_days', 'Enter how many days apart it repeats.')
        else:
            cleaned['every_days'] = REPEAT_DAYS.get(repeat)
        return cleaned

    def task_fields(self):
        """Keyword arguments for services.create_task."""
        c = self.cleaned_data
        return {
            'title': c['title'], 'area': c['area'], 'done_when': c['done_when'],
            'estimated_minutes': c['estimated_minutes'], 'every_days': c['every_days'],
            'start_date': c['start_date'], 'assignee': c['assignee'],
        }


class EditTaskForm(TaskForm):
    """TaskForm filled in from an existing template. Pickup chores leave out the schedule fields."""

    def __init__(self, *args, template, open_chore, is_pickup, **kwargs):
        every_days = template.every_days
        repeat = next((key for key, days in REPEAT_DAYS.items() if days == every_days), 'custom')
        kwargs['initial'] = {
            'title': template.title, 'area': template.area, 'done_when': template.done_when,
            'estimated_minutes': template.estimated_minutes, 'assignee': template.default_assignee,
            'repeat': repeat, 'every_days': every_days if repeat == 'custom' else None,
            'start_date': open_chore.due_date if open_chore else None,
        }
        super().__init__(*args, **kwargs)
        self.fields['start_date'].initial = None
        self.fields['start_date'].label = 'Next due'
        self.fields['start_date'].help_text = 'Changing it moves only the next one.'
        if is_pickup or open_chore is None:
            del self.fields['start_date']
        if is_pickup:
            del self.fields['repeat'], self.fields['every_days']

    def update_fields(self):
        """Keyword arguments for services.update_template."""
        c = self.cleaned_data
        return {
            'title': c['title'], 'area': c['area'], 'done_when': c['done_when'],
            'estimated_minutes': c['estimated_minutes'], 'assignee': c['assignee'],
            'every_days': c.get('every_days'), 'due_date': c.get('start_date'),
        }


class MoveForm(forms.Form):
    due_date = forms.DateField()


class AwayForm(forms.Form):
    start = forms.DateField(label='From', widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='Until', help_text='The last day you\'re away.', widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start'), cleaned.get('end')
        if start and end:
            if end < start:
                self.add_error('end', 'Must be on or after the first day.')
            elif end < timezone.localdate():
                self.add_error('end', "That's already over.")
        return cleaned


class NewUserForm(UserCreationForm):
    is_admin = forms.BooleanField(
        label='Administrator', required=False, help_text='Can create users and use the admin site.'
    )

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = user.is_superuser = self.cleaned_data['is_admin']
        if commit:
            user.save()
        return user
