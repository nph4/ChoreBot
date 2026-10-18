from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import AssignForm, PickupSetupForm, ScheduleForm
from .models import Chore, PickupSchedule
from .services import (
    active_pickup_schedules, assign_chore, complete_chore, generate_due_chores, save_pickup_schedules,
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
