from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Chore
from .services import complete_chore, generate_due_chores

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
    }


@login_required
def dashboard(request):
    generate_due_chores()
    return render(request, 'chores/dashboard.html', _list_context(request))


@login_required
@require_POST
def mark_done(request, pk):
    chore = get_object_or_404(Chore, pk=pk)
    complete_chore(chore, request.user)
    if request.headers.get('HX-Request'):
        return render(request, 'chores/_chore_lists.html', _list_context(request))
    who = 'mine' if request.GET.get('who') == 'mine' else 'everyone'
    return redirect(f"{reverse('chores:dashboard')}?who={who}")
