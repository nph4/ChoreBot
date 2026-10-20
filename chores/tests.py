from datetime import date, datetime, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Chore, ChoreTemplate, NotificationLog, PickupSchedule
from .notifications import send, send_due_notification
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


class AssignTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.other = get_user_model().objects.create_user('sam')
        self.client.force_login(self.user)
        self.template = make_template(every_days=7)
        [self.chore] = generate_due_chores()

    def assign(self, assignee, include_future=False, **query):
        data = {'assignee': assignee.pk if assignee else ''}
        if include_future:
            data['include_future'] = 'on'
        url = reverse('chores:assign', args=[self.chore.pk])
        return self.client.post(url, data, query_params=query, headers={'HX-Request': 'true'})

    def test_assign_just_this_occurrence(self):
        self.assign(self.other)
        self.chore.refresh_from_db()
        self.template.refresh_from_db()
        self.assertEqual(self.chore.assignee, self.other)
        self.assertIsNone(self.template.default_assignee)

        complete_chore(self.chore, self.user)
        self.assertIsNone(Chore.objects.get(completed_at__isnull=True).assignee)

    def test_assign_this_and_future_ones(self):
        self.assign(self.other, include_future=True)
        self.template.refresh_from_db()
        self.assertEqual(self.template.default_assignee, self.other)

        complete_chore(Chore.objects.get(pk=self.chore.pk), self.user)
        self.assertEqual(Chore.objects.get(completed_at__isnull=True).assignee, self.other)

    def test_unassign(self):
        self.assign(self.user)
        self.assign(None)
        self.chore.refresh_from_db()
        self.assertIsNone(self.chore.assignee)

    def test_mine_filter_reflects_assignment(self):
        self.assertEqual(self.assign(self.other, who='mine').context['due_today'], [])
        self.assertEqual(self.assign(self.user, who='mine').context['due_today'], [self.chore])

    def test_unknown_member_is_rejected(self):
        url = reverse('chores:assign', args=[self.chore.pk])
        self.assertEqual(self.client.post(url, {'assignee': '999999'}).status_code, 400)

    def test_dashboard_lists_members_to_assign(self):
        response = self.client.get(reverse('chores:dashboard'))
        self.assertContains(response, f'<option value="{self.other.pk}">sam</option>', html=True)


class CustomTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def post_task(self, **overrides):
        data = {
            'title': 'Clean gutters', 'area': 'Outside', 'done_when': 'Gutters clear',
            'estimated_minutes': '45', 'repeat': 'once', 'start_date': self.today.isoformat(), 'assignee': '',
        }
        data.update(overrides)
        return self.client.post(reverse('chores:new_task'), data)

    def test_one_off_is_created_and_not_repeated(self):
        response = self.post_task()
        self.assertRedirects(response, reverse('chores:dashboard'))
        template = ChoreTemplate.objects.get(title='Clean gutters')
        self.assertTrue(template.is_one_off)
        chore = Chore.objects.get(template=template)
        complete_chore(chore, self.user)
        generate_due_chores()
        self.assertFalse(template.chores.filter(completed_at__isnull=True).exists())

    def test_repeat_presets(self):
        for repeat, days in [('daily', 1), ('weekly', 7), ('monthly', 30)]:
            self.post_task(title=repeat, repeat=repeat)
            self.assertEqual(ChoreTemplate.objects.get(title=repeat).every_days, days)

    def test_custom_repeat_requires_day_count(self):
        response = self.post_task(repeat='custom')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ChoreTemplate.objects.exists())
        self.post_task(repeat='custom', every_days='10')
        self.assertEqual(ChoreTemplate.objects.get().every_days, 10)

    def test_future_due_date_and_assignee(self):
        due = self.today + timedelta(days=5)
        self.post_task(start_date=due.isoformat(), assignee=str(self.user.pk))
        chore = Chore.objects.get()
        self.assertEqual(chore.due_date, due)
        self.assertEqual(chore.assignee, self.user)

    def test_success_toast(self):
        response = self.post_task()
        self.assertContains(self.client.get(response.url), 'Clean gutters” created')


class CalendarTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def entries(self, **params):
        response = self.client.get(reverse('chores:calendar'), params)
        self.assertEqual(response.status_code, 200)
        return {
            (day, kind, template.title)
            for week in response.context['weeks'] for day, entries in week for kind, template, _, _ in entries
        }

    def test_open_done_and_projected(self):
        template = make_template(title='Water plants', every_days=3)
        [chore] = generate_due_chores()
        complete_chore(chore, self.user)
        entries = self.entries()
        self.assertIn((self.today, 'done', 'Water plants'), entries)
        self.assertIn((self.today + timedelta(days=3), 'open', 'Water plants'), entries)
        self.assertIn((self.today + timedelta(days=6), 'projected', 'Water plants'), entries)

    def test_one_off_has_no_projections(self):
        make_template(title='Fix fence', every_days=None)
        generate_due_chores()
        kinds = [kind for _, kind, title in self.entries() if title == 'Fix fence']
        self.assertEqual(kinds, ['open'])

    def test_pickup_projections_follow_schedule(self):
        anchor = self.today + timedelta(days=2)
        save_pickup_schedules({PickupSchedule.GARBAGE: (2, anchor, False)})
        month = anchor.strftime('%Y-%m')
        days = sorted(day for day, kind, title in self.entries(month=month) if title == 'Take out garbage')
        self.assertEqual(days[0], anchor)
        self.assertTrue(all((b - a).days == 14 for a, b in zip(days, days[1:])))

    def test_mine_filter(self):
        other = get_user_model().objects.create_user('sam')
        make_template(title='Mine', default_assignee=self.user)
        make_template(title='Theirs', default_assignee=other)
        generate_due_chores()
        titles = {title for _, _, title in self.entries(who='mine')}
        self.assertEqual(titles, {'Mine'})

    def test_month_navigation_and_bad_input(self):
        response = self.client.get(reverse('chores:calendar'), {'month': '2027-02'})
        self.assertEqual(response.context['month'], date(2027, 2, 1))
        self.assertEqual(response.context['prev_month'], '2027-01')
        self.assertEqual(response.context['next_month'], '2027-03')
        response = self.client.get(reverse('chores:calendar'), {'month': 'nonsense'})
        self.assertEqual(response.context['month'], self.today.replace(day=1))


class NewUserTests(TestCase):
    def setUp(self):
        self.url = reverse('chores:new_user')
        self.admin = get_user_model().objects.create_superuser('admin', password='pw')
        self.member = get_user_model().objects.create_user('alex', password='pw')

    def test_non_superuser_is_forbidden_and_menu_hides_it(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertNotContains(self.client.get(reverse('chores:dashboard')), self.url)

    def test_superuser_creates_member(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('chores:dashboard')), self.url)
        response = self.client.post(self.url, {
            'username': 'sam', 'password1': 'a-long-test-passphrase-42', 'password2': 'a-long-test-passphrase-42',
        })
        self.assertRedirects(response, reverse('chores:dashboard'))
        sam = get_user_model().objects.get(username='sam')
        self.assertTrue(sam.check_password('a-long-test-passphrase-42'))
        self.assertFalse(sam.is_superuser)

    def test_superuser_creates_admin(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {
            'username': 'pat', 'password1': 'a-long-test-passphrase-42', 'password2': 'a-long-test-passphrase-42',
            'is_admin': 'on',
        })
        pat = get_user_model().objects.get(username='pat')
        self.assertTrue(pat.is_superuser and pat.is_staff)

    def test_password_mismatch(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {'username': 'sam', 'password1': 'a-long-test-passphrase-42', 'password2': 'different-passphrase-99'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username='sam').exists())


class StaticFilesTests(TestCase):
    # Under Compose the bind mount hides the image's collectstatic output, so let WhiteNoise
    # find files in app static/ folders the way the hashed copies would be found in production.
    @override_settings(WHITENOISE_USE_FINDERS=True)
    def test_font_is_served_with_debug_off(self):
        # The test runner forces DEBUG off, so this is WhiteNoise serving it, not Django's dev view.
        self.assertFalse(settings.DEBUG)
        response = self.client.get('/static/chores/fonts/AdwaitaSans-Regular.woff2')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'font/woff2')


@override_settings(NOTIFY_URLS=['json://localhost'], NOTIFY_MORNING_AT='08:00', NOTIFY_EVENING_AT='19:00')
class NotificationTests(TestCase):
    day = date(2026, 3, 10)

    def at(self, hour, minute=0, day=None):
        return timezone.make_aware(datetime.combine(day or self.day, datetime.min.time()).replace(hour=hour, minute=minute))

    def setUp(self):
        patcher = mock.patch('chores.notifications.send', return_value=True)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.dishes = make_template(title='Dishes', default_assignee=self.user)
        self.vacuum = make_template(title='Vacuum', area='', estimated_minutes=20)

    def test_nothing_before_morning_time(self):
        self.assertIsNone(send_due_notification(self.at(7, 59)))
        self.assertFalse(NotificationLog.objects.exists())

    def test_morning_digest_sent_once_and_generates_chores(self):
        log = send_due_notification(self.at(8))
        self.assertEqual((log.kind, log.chore_count, log.delivered), (NotificationLog.MORNING, 2, True))
        title, body = self.send.call_args.args
        self.assertEqual(title, 'ChoreBot: 2 chores today')
        self.assertIn('**Due today**', body)
        self.assertIn('- Dishes (Kitchen, ~10 min) · alex', body)
        self.assertIn('- Vacuum (~20 min) · unassigned', body)
        self.assertNotIn('Overdue', body)
        self.assertIsNone(send_due_notification(self.at(9)))
        self.send.assert_called_once()

    def test_overdue_listed_with_due_date_and_upcoming_left_out(self):
        Chore.objects.create(template=self.dishes, assignee=self.user, due_date=self.day - timedelta(days=2))
        Chore.objects.create(template=self.vacuum, due_date=self.day + timedelta(days=1))
        send_due_notification(self.at(8))
        title, body = self.send.call_args.args
        self.assertEqual(title, 'ChoreBot: 1 chore today')
        self.assertIn('**Overdue**\n- Dishes (Kitchen, ~10 min) · alex · due Sun 8 Mar', body)
        self.assertNotIn('Vacuum', body)

    def test_late_start_catches_up_morning_but_not_after_evening_time(self):
        self.assertEqual(send_due_notification(self.at(10)).kind, NotificationLog.MORNING)
        next_day = self.day + timedelta(days=1)
        self.assertEqual(send_due_notification(self.at(19, 30, next_day)).kind, NotificationLog.EVENING)
        self.assertFalse(NotificationLog.objects.filter(kind=NotificationLog.MORNING, date=next_day).exists())

    def test_evening_nudge_lists_only_chores_still_open(self):
        send_due_notification(self.at(8))
        complete_chore(Chore.objects.get(template=self.dishes), self.user)
        log = send_due_notification(self.at(19))
        self.assertEqual((log.kind, log.chore_count), (NotificationLog.EVENING, 1))
        title, body = self.send.call_args.args
        self.assertEqual(title, 'ChoreBot: 1 chore still to do')
        self.assertNotIn('Dishes', body)

    def test_nothing_due_is_logged_but_not_sent(self):
        ChoreTemplate.objects.update(active=False)
        log = send_due_notification(self.at(8))
        self.assertEqual(log.chore_count, 0)
        self.send.assert_not_called()

    def test_failed_send_is_not_retried(self):
        self.send.return_value = False
        self.assertFalse(send_due_notification(self.at(8)).delivered)
        self.assertIsNone(send_due_notification(self.at(8, 1)))
        self.send.assert_called_once()

    @override_settings(NOTIFY_URLS=[])
    def test_no_urls_means_no_notifications(self):
        self.assertIsNone(send_due_notification(self.at(8)))
        self.assertFalse(NotificationLog.objects.exists())

    @override_settings(NOTIFY_EVENING_AT='')
    def test_empty_time_turns_that_message_off(self):
        self.assertEqual(send_due_notification(self.at(23)).kind, NotificationLog.MORNING)


class AppriseSendTests(TestCase):
    @override_settings(NOTIFY_URLS=['json://localhost'])
    def test_sends_markdown_to_destinations(self):
        with mock.patch('apprise.Apprise.notify', return_value=True) as notify:
            self.assertTrue(send('Title', '**Body**'))
        self.assertEqual(notify.call_args.kwargs['body_format'], 'markdown')

    @override_settings(NOTIFY_URLS=['json://localhost', 'not-a-service://x'])
    def test_unrecognised_url_counts_as_failure(self):
        with mock.patch('apprise.Apprise.notify', return_value=True), self.assertLogs('chores.notifications', 'WARNING'):
            self.assertFalse(send('Title', 'Body'))
