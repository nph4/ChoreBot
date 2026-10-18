from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Chore, ChoreTemplate, PickupSchedule
from .services import complete_chore, generate_due_chores, next_due_date, save_pickup_schedules


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


# 2026-10-20 is a Tuesday.
TUESDAY = date(2026, 10, 20)


class PickupScheduleDueDateTests(TestCase):
    def schedule(self, every_weeks=1, am_pickup=False):
        return PickupSchedule(every_weeks=every_weeks, next_pickup=TUESDAY, am_pickup=am_pickup)

    def test_pm_pickup_is_due_the_morning_of_pickup(self):
        self.assertEqual(self.schedule().next_due_date(TUESDAY - timedelta(days=3)), TUESDAY)

    def test_am_pickup_is_due_the_day_before(self):
        self.assertEqual(self.schedule(am_pickup=True).next_due_date(TUESDAY - timedelta(days=3)), TUESDAY - timedelta(days=1))

    def test_am_pickup_today_rolls_to_next_cycle(self):
        due = self.schedule(am_pickup=True).next_due_date(TUESDAY)
        self.assertEqual(due, TUESDAY + timedelta(days=6))

    def test_every_other_week_skips_off_weeks(self):
        self.assertEqual(self.schedule(every_weeks=2).next_due_date(TUESDAY + timedelta(days=1)), TUESDAY + timedelta(days=14))

    def test_anchor_in_the_past_still_finds_the_right_week(self):
        schedule = self.schedule(every_weeks=2)
        self.assertEqual(schedule.next_due_date(TUESDAY + timedelta(days=29)), TUESDAY + timedelta(days=42))


class SavePickupSchedulesTests(TestCase):
    def test_creates_one_chore_per_bin(self):
        save_pickup_schedules({
            PickupSchedule.GARBAGE: (2, TUESDAY, True),
            PickupSchedule.RECYCLING: (1, TUESDAY, True),
        })
        titles = set(Chore.objects.values_list('template__title', flat=True))
        self.assertEqual(titles, {'Take out garbage', 'Take out recycling'})

    def test_resave_replaces_open_chore_with_new_schedule(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, TUESDAY, False)})
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, TUESDAY + timedelta(days=2), False)})
        chore = Chore.objects.get(completed_at__isnull=True)
        self.assertEqual(chore.due_date.weekday(), 3)  # Thursday

    def test_dropping_a_bin_deactivates_it_and_readding_reuses_it(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, TUESDAY, False), PickupSchedule.COMPOST: (1, TUESDAY, False)})
        compost = PickupSchedule.objects.get(stream=PickupSchedule.COMPOST).template
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, TUESDAY, False)})
        compost.refresh_from_db()
        self.assertFalse(compost.active)
        self.assertFalse(compost.chores.filter(completed_at__isnull=True).exists())
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, TUESDAY, False), PickupSchedule.COMPOST: (1, TUESDAY, False)})
        self.assertEqual(ChoreTemplate.objects.filter(title='Take out compost/yard waste').count(), 1)

    def test_completing_early_does_not_reschedule_the_same_pickup(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, timezone.localdate() + timedelta(days=3), False)})
        chore = Chore.objects.get()
        complete_chore(chore, None)  # three days early
        next_chore = Chore.objects.get(completed_at__isnull=True)
        self.assertEqual(next_chore.due_date, chore.due_date + timedelta(days=7))
        self.assertEqual(next_due_date(chore.template, timezone.localdate()), next_chore.due_date)


class PickupSetupViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.url = reverse('chores:pickup_setup')

    def schedules(self):
        return {s.stream: (s.every_weeks, s.next_pickup, s.am_pickup) for s in PickupSchedule.objects.all()}

    def test_dashboard_prompts_until_configured(self):
        self.assertContains(self.client.get(reverse('chores:dashboard')), self.url)
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, TUESDAY, False)})
        self.assertNotContains(self.client.get(reverse('chores:dashboard')), 'Set up your pickup schedule')

    def test_same_schedule_applies_to_every_bin(self):
        response = self.client.post(self.url, {
            'same_schedule': 'yes', 'has_compost': 'yes',
            'shared-frequency': 'every_other_week', 'shared-next_pickup': '2026-10-20', 'shared-pickup_time': 'am',
        })
        self.assertRedirects(response, reverse('chores:dashboard'))
        self.assertEqual(self.schedules(), {
            stream: (2, TUESDAY, True) for stream in ['garbage', 'recycling', 'compost']
        })

    def test_portland_style_different_schedules(self):
        response = self.client.post(self.url, {
            'same_schedule': 'no', 'has_compost': 'yes',
            'garbage-frequency': 'every_other_week', 'garbage-next_pickup': '2026-10-27', 'garbage-pickup_time': 'am',
            'recycling-frequency': 'weekly', 'recycling-next_pickup': '2026-10-20', 'recycling-pickup_time': 'am',
            'compost-frequency': 'weekly', 'compost-next_pickup': '2026-10-20', 'compost-pickup_time': 'pm',
        })
        self.assertRedirects(response, reverse('chores:dashboard'))
        self.assertEqual(self.schedules(), {
            'garbage': (2, date(2026, 10, 27), True),
            'recycling': (1, TUESDAY, True),
            'compost': (1, TUESDAY, False),
        })

    def test_custom_requires_week_count(self):
        response = self.client.post(self.url, {
            'same_schedule': 'yes', 'has_compost': 'no',
            'shared-frequency': 'custom', 'shared-next_pickup': '2026-10-20', 'shared-pickup_time': 'pm',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PickupSchedule.objects.exists())

        self.client.post(self.url, {
            'same_schedule': 'yes', 'has_compost': 'no',
            'shared-frequency': 'custom', 'shared-every_weeks': '4', 'shared-next_pickup': '2026-10-20', 'shared-pickup_time': 'pm',
        })
        self.assertEqual(self.schedules(), {stream: (4, TUESDAY, False) for stream in ['garbage', 'recycling']})

    def test_form_is_prefilled_from_saved_schedule(self):
        save_pickup_schedules({
            PickupSchedule.GARBAGE: (2, TUESDAY, True),
            PickupSchedule.RECYCLING: (1, TUESDAY, True),
        })
        setup = self.client.get(self.url).context['setup']
        self.assertEqual(setup.initial, {'same_schedule': 'no', 'has_compost': 'no'})
