import calendar
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.views.decorators.http import require_POST

from .forms import AssignForm, AwayForm, EditTaskForm, MoveForm, NewUserForm, PickupSetupForm, ScheduleForm, TaskForm
from .models import AwayPeriod, Chore, ChoreTemplate, PickupSchedule
from .services import (
    active_pickup_schedules, add_away_period, assign_chore, calendar_entries, complete_chore, create_task,
    generate_due_chores, move_chore, remove_template, save_pickup_schedules, skip_chore, update_template,
)

UPCOMING_DAYS = 7


def _list_context(request):
    today = timezone.localdate()
    who = 'mine' if request.GET.get('who') == 'mine' else 'everyone'
    chores = Chore.objects.filter(
        completed_at__isnull=True,
        template__active=True,
        due_date__lte=today + timedelta(days=UPCOMING_DAYS),
    ).select_related('template', 'assignee')
    if who == 'mine':
        chores = chores.filter(assignee=request.user)
    return {
        'who': who,
        'overdue': [c for c in chores if c.due_date < today],
        'due_today': [c for c in chores if c.due_date == today],
        'upcoming': [c for c in chores if c.due_date > today],
        'members': get_user_model().objects.filter(is_active=True).order_by('username'),
    }


def _lists_response(request):
    """After a row action: the refreshed lists for htmx, or a redirect back to the dashboard."""
    if request.headers.get('HX-Request'):
        return render(request, 'chores/_chore_lists.html', _list_context(request))
    who = 'mine' if request.GET.get('who') == 'mine' else 'everyone'
    return redirect(f"{reverse('chores:dashboard')}?who={who}")


@login_required
def dashboard(request):
    generate_due_chores()
    context = _list_context(request)
    context['needs_pickup_setup'] = not PickupSchedule.objects.exists()
    today = timezone.localdate()
    context['away'] = AwayPeriod.objects.filter(start__lte=today, end__gte=today).order_by('-end').first()
    return render(request, 'chores/dashboard.html', context)


@login_required
@require_POST
def mark_done(request, pk):
    chore = get_object_or_404(Chore, pk=pk)
    complete_chore(chore, request.user)
    return _lists_response(request)


@login_required
@require_POST
def assign(request, pk):
    chore = get_object_or_404(Chore, pk=pk, completed_at__isnull=True)
    form = AssignForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Unknown household member.')
    assign_chore(chore, form.cleaned_data['assignee'], include_future=form.cleaned_data['include_future'])
    return _lists_response(request)


@login_required
@require_POST
def skip(request, pk):
    chore = get_object_or_404(Chore, pk=pk, completed_at__isnull=True)
    if chore.template.is_one_off:
        return HttpResponseBadRequest('One-off chores can be moved, not skipped.')
    skip_chore(chore, request.user)
    return _lists_response(request)


@login_required
@require_POST
def move(request, pk):
    chore = get_object_or_404(Chore, pk=pk, completed_at__isnull=True)
    form = MoveForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest('Enter a valid date.')
    move_chore(chore, form.cleaned_data['due_date'])
    return _lists_response(request)


@login_required
def edit_template(request, pk):
    template = get_object_or_404(ChoreTemplate, pk=pk, active=True)
    open_chore = template.chores.filter(completed_at__isnull=True).first()
    is_pickup = PickupSchedule.objects.filter(template=template).exists()
    form = EditTaskForm(request.POST or None, template=template, open_chore=open_chore, is_pickup=is_pickup)
    if request.method == 'POST' and form.is_valid():
        update_template(template, **form.update_fields())
        messages.success(request, f'“{template.title}” saved')
        return redirect('chores:dashboard')
    return render(request, 'chores/task_form.html', {'form': form, 'template': template, 'is_pickup': is_pickup})


@login_required
@require_POST
def remove(request, pk):
    template = get_object_or_404(ChoreTemplate, pk=pk, active=True)
    if PickupSchedule.objects.filter(template=template).exists():
        return HttpResponseBadRequest('Remove a bin from the pickup schedule instead.')
    remove_template(template)
    messages.success(request, f'“{template.title}” removed')
    return redirect('chores:dashboard')


@login_required
def away(request):
    """Add and list away periods."""
    form = AwayForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        period = add_away_period(form.cleaned_data['start'], form.cleaned_data['end'])
        messages.success(request, f'Away {formats.date_format(period.start, "D j M")} – {formats.date_format(period.end, "D j M")}')
        return redirect('chores:away')
    return render(request, 'chores/away.html', {
        'form': form,
        'periods': AwayPeriod.objects.filter(end__gte=timezone.localdate()),
        'today': timezone.localdate(),
    })


@login_required
@require_POST
def delete_away(request, pk):
    get_object_or_404(AwayPeriod, pk=pk).delete()
    messages.success(request, 'Away dates removed')
    return redirect('chores:away')


@login_required
def pickup_setup(request):
    """Ask whether bins share a schedule, then collect one shared schedule or one per bin."""
    data = request.POST if request.method == 'POST' else None
    existing = active_pickup_schedules()
    today = timezone.localdate()
    initial = {}
    if existing:
        initials = [ScheduleForm.initial_from(s, today) for s in existing.values()]
        initial = {
            'same_schedule': 'yes' if all(i == initials[0] for i in initials) else 'no',
            'has_compost': 'yes' if PickupSchedule.COMPOST in existing else 'no',
        }

    def schedule_form(prefix, stream=None):
        source = existing.get(stream) or next(iter(existing.values()), None)
        return ScheduleForm(data, prefix=prefix, initial=ScheduleForm.initial_from(source, today) if source else None)

    setup = PickupSetupForm(data, initial=initial)
    shared = schedule_form('shared')
    bins = {stream: schedule_form(stream, stream) for stream, _ in PickupSchedule.STREAM_CHOICES}

    if data is not None and setup.is_valid():
        streams = [PickupSchedule.GARBAGE, PickupSchedule.RECYCLING]
        if setup.cleaned_data['has_compost'] == 'yes':
            streams.append(PickupSchedule.COMPOST)
        if setup.cleaned_data['same_schedule'] == 'yes':
            if shared.is_valid():
                save_pickup_schedules({stream: shared.as_schedule() for stream in streams})
                return redirect('chores:dashboard')
        elif all(bins[stream].is_valid() for stream in streams):
            save_pickup_schedules({stream: bins[stream].as_schedule() for stream in streams})
            return redirect('chores:dashboard')

    return render(request, 'chores/pickup_setup.html', {
        'setup': setup,
        'shared': shared,
        'bins': [(label, stream, bins[stream]) for stream, label in PickupSchedule.STREAM_CHOICES],
    })


def superuser_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def _parse_month(value, today):
    try:
        year, month = (int(part) for part in value.split('-'))
        return date(year, month, 1)
    except (AttributeError, ValueError):
        return today.replace(day=1)


@login_required
def calendar_view(request):
    """Month grid of done, open, overdue and projected chores."""
    generate_due_chores()
    today = timezone.localdate()
    month = _parse_month(request.GET.get('month'), today)
    who = 'mine' if request.GET.get('who') == 'mine' else 'everyone'

    # Django's FIRST_DAY_OF_WEEK counts from Sunday=0; Python's calendar from Monday=0.
    first_weekday = (int(formats.get_format('FIRST_DAY_OF_WEEK')) - 1) % 7
    weeks = calendar.Calendar(first_weekday).monthdatescalendar(month.year, month.month)
    entries = calendar_entries(
        weeks[0][0], weeks[-1][-1], assignee=request.user if who == 'mine' else None, today=today
    )
    away_days = set()
    for period in AwayPeriod.objects.filter(start__lte=weeks[-1][-1], end__gte=weeks[0][0]):
        away_days.update(period.start + timedelta(days=i) for i in range((period.end - period.start).days + 1))
    prev_month = (month - timedelta(days=1)).replace(day=1)
    next_month = (month + timedelta(days=31)).replace(day=1)
    return render(request, 'chores/calendar.html', {
        'who': who,
        'month': month,
        'today': today,
        'weekday_names': [calendar.day_abbr[(first_weekday + i) % 7] for i in range(7)],
        'weeks': [[(day, entries.get(day, []), day in away_days) for day in week] for week in weeks],
        'prev_month': prev_month.strftime('%Y-%m'),
        'next_month': next_month.strftime('%Y-%m'),
        'this_month': today.strftime('%Y-%m'),
    })


@login_required
def new_task(request):
    form = TaskForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        template = create_task(**form.task_fields())
        messages.success(request, f'“{template.title}” created')
        return redirect('chores:dashboard')
    return render(request, 'chores/task_form.html', {'form': form})


@superuser_required
def new_user(request):
    form = NewUserForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        messages.success(request, f'User “{user.get_username()}” created')
        return redirect('chores:dashboard')
    return render(request, 'chores/user_form.html', {'form': form})
