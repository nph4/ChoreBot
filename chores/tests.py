from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Chore, ChoreTemplate
from .services import complete_chore, generate_due_chores


def make_template(**kwargs):
    defaults = {'title': 'Dishes', 'area': 'Kitchen', 'done_when': 'Sink empty', 'every_days': 3}
    return ChoreTemplate.objects.create(**{**defaults, **kwargs})


class GenerateDueChoresTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def test_first_chore_is_due_today(self):
        template = make_template()
        [chore] = generate_due_chores(self.today)
        self.assertEqual(chore.template, template)
        self.assertEqual(chore.due_date, self.today)

    def test_next_chore_is_due_every_days_after_last_completion(self):
        template = make_template(every_days=3)
        [chore] = generate_due_chores(self.today)
        complete_chore(chore, None)
        next_chore = Chore.objects.get(template=template, completed_at__isnull=True)
        self.assertEqual(next_chore.due_date, self.today + timedelta(days=3))

    def test_repeated_runs_do_not_duplicate(self):
        make_template()
        generate_due_chores(self.today)
        self.assertEqual(generate_due_chores(self.today), [])
        self.assertEqual(Chore.objects.count(), 1)

    def test_inactive_templates_are_skipped(self):
        make_template(active=False)
        self.assertEqual(generate_due_chores(self.today), [])

    def test_assignee_comes_from_template(self):
        user = get_user_model().objects.create_user('alex')
        make_template(default_assignee=user)
        [chore] = generate_due_chores(self.today)
        self.assertEqual(chore.assignee, user)


class DashboardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('chores:dashboard'))
        self.assertEqual(response.status_code, 302)

    def test_chores_are_bucketed_by_due_date(self):
        late = Chore.objects.create(template=make_template(title='Late'), due_date=self.today - timedelta(days=2))
        now = Chore.objects.create(template=make_template(title='Now'), due_date=self.today)
        soon = Chore.objects.create(template=make_template(title='Soon'), due_date=self.today + timedelta(days=3))
        Chore.objects.create(template=make_template(title='Far'), due_date=self.today + timedelta(days=30))

        response = self.client.get(reverse('chores:dashboard'))

        self.assertEqual(response.context['overdue'], [late])
        self.assertEqual(response.context['due_today'], [now])
        self.assertEqual(response.context['upcoming'], [soon])

    def test_mine_filter(self):
        other = get_user_model().objects.create_user('sam')
        mine = Chore.objects.create(template=make_template(title='Mine'), assignee=self.user, due_date=self.today)
        Chore.objects.create(template=make_template(title='Theirs'), assignee=other, due_date=self.today)

        response = self.client.get(reverse('chores:dashboard'), {'who': 'mine'})

        self.assertEqual(response.context['due_today'], [mine])


class MarkDoneTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.template = make_template(every_days=7)
        [self.chore] = generate_due_chores()

    def test_htmx_request_returns_updated_lists(self):
        response = self.client.post(reverse('chores:mark_done', args=[self.chore.pk]), headers={'HX-Request': 'true'})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'chores/_chore_lists.html')
        self.assertTemplateNotUsed(response, 'chores/base.html')
        self.chore.refresh_from_db()
        self.assertEqual(self.chore.completed_by, self.user)
        self.assertEqual(response.context['upcoming'][0].due_date, timezone.localdate() + timedelta(days=7))

    def test_plain_post_redirects_to_dashboard(self):
        response = self.client.post(reverse('chores:mark_done', args=[self.chore.pk]))

        self.assertRedirects(response, reverse('chores:dashboard') + '?who=everyone')
        self.chore.refresh_from_db()
        self.assertTrue(self.chore.is_done)

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse('chores:mark_done', args=[self.chore.pk]))
        self.assertEqual(response.status_code, 405)
