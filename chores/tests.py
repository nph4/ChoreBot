import json
from datetime import date, datetime, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AwayPeriod, Chore, ChoreTemplate, NotificationLog, PickupSchedule
from . import stats
from .notifications import send, send_due_notification
from .services import (
    add_away_period, calendar_entries, complete_chore, enable_template, generate_due_chores, next_due_date,
    remove_template, save_pickup_schedules, skip_chore, slide_to_lighter_day, spread_overdue, suggest_chores,
    update_template,
)


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
            for week in response.context['weeks'] for day, entries, _ in week for kind, template, _, _ in entries
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

    @override_settings(WHITENOISE_USE_FINDERS=True)
    def test_manifest_and_icons(self):
        response = self.client.get('/static/chores/manifest.webmanifest')
        self.assertEqual(response.status_code, 200)
        manifest = json.loads(b''.join(response.streaming_content))
        self.assertEqual((manifest['start_url'], manifest['display']), ('/', 'standalone'))
        for icon in manifest['icons']:
            self.assertEqual(self.client.get(f'/static/chores/{icon["src"]}').status_code, 200, icon['src'])
        self.assertIn('maskable', [icon.get('purpose') for icon in manifest['icons']])

    def test_pages_link_manifest_and_icons(self):
        response = self.client.get(reverse('login'))
        for path in ('chores/manifest.webmanifest', 'chores/icons/icon.svg', 'chores/icons/apple-touch-icon.png'):
            self.assertContains(response, f'/static/{path}')


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

    def test_nothing_sent_while_away(self):
        AwayPeriod.objects.create(start=self.day, end=self.day)
        self.assertIsNone(send_due_notification(self.at(8)))
        self.assertIsNotNone(send_due_notification(self.at(8, day=self.day + timedelta(days=1))))

    @override_settings(NOTIFY_URLS=[])
    def test_no_urls_means_no_notifications(self):
        self.assertIsNone(send_due_notification(self.at(8)))
        self.assertFalse(NotificationLog.objects.exists())

    @override_settings(SITE_URL='http://chores.test')
    def test_link_to_today_when_site_url_set(self):
        send_due_notification(self.at(8))
        _, body = self.send.call_args.args
        self.assertTrue(body.endswith('\n\n[Open ChoreBot](http://chores.test/)'))

    @override_settings(SITE_URL='')
    def test_no_link_without_site_url(self):
        send_due_notification(self.at(8))
        _, body = self.send.call_args.args
        self.assertNotIn('Open ChoreBot', body)

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


def close(chore, days_ago):
    """Mark a chore done as if it were done days_ago."""
    chore.completed_at = timezone.now() - timedelta(days=days_ago)
    chore.save(update_fields=['completed_at'])


class SkipAndMoveTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.template = make_template(title='Vacuum', every_days=7)
        self.chore = Chore.objects.create(template=self.template, due_date=self.today - timedelta(days=3))

    def open_chore(self, template=None):
        return (template or self.template).chores.get(completed_at__isnull=True)

    def test_skip_schedules_next_from_today_and_is_not_done(self):
        response = self.client.post(reverse('chores:skip', args=[self.chore.pk]), headers={'HX-Request': 'true'})
        self.assertTemplateUsed(response, 'chores/_chore_lists.html')
        self.chore.refresh_from_db()
        self.assertTrue(self.chore.skipped)
        self.assertEqual(self.chore.completed_by, self.user)
        self.assertEqual(self.open_chore().due_date, self.today + timedelta(days=7))

    def test_skip_pickup_moves_to_next_pickup(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, self.today + timedelta(days=2), False)})
        bins = PickupSchedule.objects.get().template
        skip_chore(self.open_chore(bins), self.user)
        self.assertEqual(self.open_chore(bins).due_date, self.today + timedelta(days=9))

    def test_one_off_cannot_be_skipped(self):
        make_template(title='Fix gate', every_days=None)
        [chore] = generate_due_chores()
        self.assertEqual(self.client.post(reverse('chores:skip', args=[chore.pk])).status_code, 400)
        self.assertNotContains(self.client.get(reverse('chores:dashboard')), reverse('chores:skip', args=[chore.pk]))
        self.assertFalse(Chore.objects.get(pk=chore.pk).is_done)

    def test_move(self):
        new_day = self.today + timedelta(days=4)
        self.client.post(reverse('chores:move', args=[self.chore.pk]), {'due_date': new_day.isoformat()})
        self.chore.refresh_from_db()
        self.assertEqual((self.chore.due_date, self.chore.moved_by_hand), (new_day, True))

    def test_move_needs_a_date(self):
        response = self.client.post(reverse('chores:move', args=[self.chore.pk]), {'due_date': 'soon'})
        self.assertEqual(response.status_code, 400)

    def test_calendar_shows_skipped(self):
        skip_chore(self.chore, self.user)
        entries = calendar_entries(self.today, self.today)
        self.assertIn('skipped', [kind for kind, *_ in entries[self.today]])


class EditTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.template = make_template(title='Vacuum', every_days=7)
        [first] = generate_due_chores()
        close(first, days_ago=3)
        [self.chore] = generate_due_chores()  # due in 4 days
        self.url = reverse('chores:edit_task', args=[self.template.pk])

    def post(self, **changes):
        data = {
            'title': 'Vacuum', 'area': 'Kitchen', 'done_when': 'Sink empty', 'estimated_minutes': 10,
            'repeat': 'weekly', 'start_date': self.chore.due_date.isoformat(), 'assignee': '',
        }
        return self.client.post(self.url, {**data, **changes})

    def test_form_is_filled_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context['form']['repeat'].value(), 'weekly')
        self.assertEqual(response.context['form']['start_date'].value(), self.chore.due_date)

    def test_more_often_reschedules_but_not_into_the_past(self):
        self.assertRedirects(self.post(repeat='daily'), reverse('chores:dashboard'))
        self.chore.refresh_from_db()
        self.assertEqual(self.chore.due_date, self.today)

    def test_less_often_reschedules_from_last_done(self):
        self.post(repeat='custom', every_days=14)
        self.chore.refresh_from_db()
        self.assertEqual(self.chore.due_date, self.today + timedelta(days=11))

    def test_frequency_change_keeps_date_moved_by_hand(self):
        self.chore.moved_by_hand = True
        self.chore.save()
        self.post(repeat='daily')
        self.chore.refresh_from_db()
        self.assertEqual(self.chore.due_date, self.today + timedelta(days=4))

    def test_new_due_date_moves_chore(self):
        self.post(start_date=(self.today + timedelta(days=1)).isoformat())
        self.chore.refresh_from_db()
        self.assertEqual((self.chore.due_date, self.chore.moved_by_hand), (self.today + timedelta(days=1), True))

    def test_new_assignee_takes_over_open_chore(self):
        self.post(assignee=self.user.pk, title='Vacuum upstairs')
        self.chore.refresh_from_db()
        self.template.refresh_from_db()
        self.assertEqual((self.chore.assignee, self.template.default_assignee), (self.user, self.user))
        self.assertEqual(self.template.title, 'Vacuum upstairs')

    def test_pickup_edit_leaves_schedule_alone(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, self.today + timedelta(days=2), False)})
        bins = PickupSchedule.objects.get().template
        url = reverse('chores:edit_task', args=[bins.pk])
        response = self.client.get(url)
        self.assertNotIn('repeat', response.context['form'].fields)
        self.assertNotContains(response, reverse('chores:remove_task', args=[bins.pk]))
        self.client.post(url, {'title': 'Garbage out', 'done_when': 'At the curb', 'estimated_minutes': 5})
        bins.refresh_from_db()
        self.assertEqual((bins.title, bins.every_days), ('Garbage out', 7))
        self.assertEqual(bins.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=2))
        self.assertEqual(self.client.post(reverse('chores:remove_task', args=[bins.pk])).status_code, 400)

    def test_remove_keeps_history(self):
        self.assertRedirects(self.client.post(reverse('chores:remove_task', args=[self.template.pk])), reverse('chores:chore_list'))
        self.template.refresh_from_db()
        self.assertFalse(self.template.active)
        self.assertEqual(self.template.chores.count(), 1)  # the done one
        self.assertFalse(self.template.chores.filter(completed_at__isnull=True).exists())

    def test_finished_one_off_that_now_repeats_comes_back(self):
        one_off = make_template(title='Fix gate', every_days=None)
        [chore] = generate_due_chores()
        close(chore, days_ago=2)
        update_template(one_off, title='Fix gate', area='', done_when='Latches', estimated_minutes=10,
                        assignee=None, every_days=30)
        self.assertEqual(one_off.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=28))


class AwayTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.start, self.end = self.today + timedelta(days=2), self.today + timedelta(days=8)

    def test_adding_away_moves_chores_and_skips_pickups(self):
        vacuum = make_template(title='Vacuum')
        Chore.objects.create(template=vacuum, due_date=self.today + timedelta(days=3))
        late = make_template(title='Laundry')
        Chore.objects.create(template=late, due_date=self.today - timedelta(days=1))
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, self.today + timedelta(days=4), False)})
        bins = PickupSchedule.objects.get().template

        add_away_period(self.start, self.end)
        self.assertEqual(vacuum.chores.get().due_date, self.end + timedelta(days=1))
        self.assertEqual(late.chores.get().due_date, self.today - timedelta(days=1))  # already overdue: stays
        self.assertEqual(bins.chores.get().due_date, self.today + timedelta(days=11))

    def test_new_due_dates_avoid_away(self):
        AwayPeriod.objects.create(start=self.start, end=self.end)
        template = make_template(title='Water plants', every_days=3)
        self.assertEqual(next_due_date(template, self.today), self.today)
        [chore] = generate_due_chores()
        complete_chore(chore, self.user)  # next would be today + 3, inside the away period
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, self.end + timedelta(days=1))

    def test_calendar_projections_and_shading(self):
        AwayPeriod.objects.create(start=self.start, end=self.end)
        make_template(title='Water plants', every_days=1)
        generate_due_chores()
        entries = calendar_entries(self.today, self.end + timedelta(days=2))
        days = sorted(day for day, items in entries.items() for kind, t, _, _ in items if t.title == 'Water plants')
        self.assertEqual(days, [self.today, self.today + timedelta(days=1), self.end + timedelta(days=1), self.end + timedelta(days=2)])
        response = self.client.get(reverse('chores:calendar'))
        away_days = {day for week in response.context['weeks'] for day, _, away in week if away}
        self.assertIn(self.start, away_days)

    def test_away_page_add_list_delete(self):
        response = self.client.post(reverse('chores:away'), {'start': self.start.isoformat(), 'end': self.end.isoformat()})
        self.assertRedirects(response, reverse('chores:away'))
        period = AwayPeriod.objects.get()
        self.assertContains(self.client.get(reverse('chores:away')), reverse('chores:delete_away', args=[period.pk]))
        self.client.post(reverse('chores:delete_away', args=[period.pk]))
        self.assertFalse(AwayPeriod.objects.exists())

    def test_away_form_rejects_backwards_or_past_dates(self):
        response = self.client.post(reverse('chores:away'), {'start': self.end.isoformat(), 'end': self.start.isoformat()})
        self.assertContains(response, 'Must be on or after the first day.')
        past = (self.today - timedelta(days=1)).isoformat()
        self.assertContains(self.client.post(reverse('chores:away'), {'start': past, 'end': past}), 'already over')
        self.assertFalse(AwayPeriod.objects.exists())

    def test_dashboard_banner_while_away(self):
        self.assertNotContains(self.client.get(reverse('chores:dashboard')), 'Away until')
        AwayPeriod.objects.create(start=self.today, end=self.end)
        self.assertContains(self.client.get(reverse('chores:dashboard')), 'Away until')


class ChoreListTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.url = reverse('chores:chore_list')

    def toggle(self, template, active, htmx=False):
        headers = {'HX-Request': 'true'} if htmx else {}
        return self.client.post(reverse('chores:set_active', args=[template.pk]), {'active': '1' if active else '0'}, headers=headers)

    def test_grouped_by_area_with_no_area_last(self):
        make_template(title='Mow lawn', area='Yard', every_days=7)
        make_template(title='Dishes', area='Kitchen', every_days=1)
        make_template(title='Call plumber', area='', every_days=None)
        response = self.client.get(self.url)
        areas = [(area, [t.title for t in items]) for area, items in response.context['areas']]
        self.assertEqual(areas, [('Kitchen', ['Dishes']), ('Yard', ['Mow lawn']), ('Other', ['Call plumber'])])
        self.assertContains(response, 'Weekly')
        self.assertContains(self.client.get(reverse('chores:dashboard')), self.url)

    def test_turn_off_then_on_schedules_from_today(self):
        mow = make_template(title='Mow lawn', area='Yard', every_days=7)
        [chore] = generate_due_chores()
        close(chore, days_ago=30)
        generate_due_chores()
        self.assertRedirects(self.toggle(mow, False), self.url)
        mow.refresh_from_db()
        self.assertFalse(mow.active)
        self.assertFalse(mow.chores.filter(completed_at__isnull=True).exists())
        self.toggle(mow, True)
        mow.refresh_from_db()
        self.assertTrue(mow.active)
        self.assertEqual(mow.chores.get(completed_at__isnull=True).due_date, self.today)  # not 23 days overdue

    def test_turn_on_keeps_future_date_and_respects_away(self):
        mow = make_template(title='Mow lawn', every_days=7)
        close(generate_due_chores()[0], days_ago=2)
        remove_template(mow)
        enable_template(mow)
        self.assertEqual(mow.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=5))

        remove_template(mow)
        AwayPeriod.objects.create(start=self.today - timedelta(days=1), end=self.today + timedelta(days=6))
        enable_template(mow)
        self.assertEqual(mow.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=7))

    def test_htmx_returns_just_the_row(self):
        mow = make_template(title='Mow lawn', every_days=7)
        response = self.toggle(mow, False, htmx=True)
        self.assertTemplateUsed(response, 'chores/_template_row.html')
        self.assertContains(response, 'aria-checked="false"')
        self.assertContains(response, f'id="template-{mow.pk}"')

    def test_pickups_and_finished_one_offs_have_no_switch(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, self.today + timedelta(days=2), False)})
        bins = PickupSchedule.objects.get().template
        gate = make_template(title='Fix gate', every_days=None)
        generate_due_chores()
        close(gate.chores.get(), days_ago=1)
        response = self.client.get(self.url)
        self.assertNotContains(response, reverse('chores:set_active', args=[bins.pk]))
        self.assertNotContains(response, reverse('chores:set_active', args=[gate.pk]))
        self.assertContains(response, 'Follows the pickup schedule')
        self.assertEqual(self.toggle(bins, False).status_code, 400)
        bins.refresh_from_db()
        self.assertTrue(bins.active)

    def test_turned_off_chore_can_still_be_edited(self):
        mow = make_template(title='Mow lawn', every_days=7)
        remove_template(mow)
        response = self.client.get(reverse('chores:edit_task', args=[mow.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Turn Off')


class UndoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()
        self.template = make_template(title='Vacuum', every_days=7, default_assignee=self.user)
        self.chore = Chore.objects.create(template=self.template, assignee=None, due_date=self.today - timedelta(days=1))

    def post(self, name, chore=None):
        return self.client.post(reverse(f'chores:{name}', args=[(chore or self.chore).pk]), headers={'HX-Request': 'true'})

    def test_done_shows_undo_toast_and_undo_restores(self):
        response = self.post('mark_done')
        self.assertContains(response, 'hx-swap-oob="true"')
        self.assertContains(response, '“Vacuum” done')
        self.assertContains(response, reverse('chores:undo', args=[self.chore.pk]))
        response = self.post('undo')
        self.assertTemplateUsed(response, 'chores/_chore_lists.html')
        self.assertNotContains(response, 'Undo')  # toast cleared
        self.chore.refresh_from_db()
        self.assertEqual((self.chore.completed_at, self.chore.completed_by, self.chore.due_date, self.chore.assignee),
                         (None, None, self.today - timedelta(days=1), None))
        self.assertEqual(self.template.chores.count(), 1)  # the next one it scheduled is gone

    def test_undo_skip(self):
        self.assertContains(self.post('skip'), '“Vacuum” skipped')
        self.post('undo')
        self.chore.refresh_from_db()
        self.assertFalse(self.chore.skipped or self.chore.is_done)

    def test_too_late(self):
        complete_chore(self.chore, self.user)
        Chore.objects.filter(pk=self.chore.pk).update(completed_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(self.post('undo').status_code, 400)
        self.assertEqual(self.template.chores.filter(completed_at__isnull=True).count(), 1)

    def test_only_latest_closed_chore(self):
        complete_chore(self.chore, self.user)
        complete_chore(self.template.chores.get(completed_at__isnull=True), self.user)
        self.assertEqual(self.post('undo').status_code, 400)
        self.assertEqual(self.post('undo', self.template.chores.filter(completed_at__isnull=False).latest('completed_at')).status_code, 200)

    def test_open_chore_and_get_are_refused(self):
        self.assertEqual(self.post('undo').status_code, 400)
        self.assertEqual(self.client.get(reverse('chores:undo', args=[self.chore.pk])).status_code, 405)

    def test_other_row_actions_clear_the_toast(self):
        response = self.client.post(reverse('chores:move', args=[self.chore.pk]), {'due_date': self.today.isoformat()},
                                    headers={'HX-Request': 'true'})
        self.assertContains(response, 'id="toasts"')
        self.assertNotContains(response, 'Undo')


def next_weekday(after, weekday):
    """First date strictly after `after` falling on weekday (Mon=0)."""
    return after + timedelta(days=(weekday - after.weekday() - 1) % 7 + 1)


class FixedDayTests(TestCase):
    monday = date(2026, 10, 19)

    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def fixed(self, weekdays, every_weeks=1, start_date=None, **kwargs):
        return make_template(title='Mow lawn', every_days=None, weekdays=weekdays, every_weeks=every_weeks,
                             start_date=start_date or self.today, **kwargs)

    def test_next_fixed_day(self):
        saturdays = self.fixed('5', start_date=self.monday)
        self.assertEqual(saturdays.next_fixed_day(self.monday), date(2026, 10, 24))
        self.assertEqual(saturdays.next_fixed_day(date(2026, 10, 24)), date(2026, 10, 31))
        mwf = self.fixed('024', start_date=self.monday)
        self.assertEqual(mwf.next_fixed_day(self.monday), date(2026, 10, 21))
        self.assertEqual(mwf.next_fixed_day(date(2026, 10, 23)), date(2026, 10, 26))

    def test_every_other_week_counts_from_start_week(self):
        sundays = self.fixed('6', every_weeks=2, start_date=date(2026, 10, 25))
        self.assertEqual(sundays.next_fixed_day(date(2026, 10, 20)), date(2026, 10, 25))
        self.assertEqual(sundays.next_fixed_day(date(2026, 10, 25)), date(2026, 11, 8))
        self.assertEqual(sundays.next_fixed_day(date(2026, 10, 11)), date(2026, 10, 25))  # before the start too

    def test_labels(self):
        self.assertEqual(self.fixed('5').repeat_label, 'Every Sat')
        self.assertEqual(self.fixed('024').repeat_label, 'Every Mon, Wed, Fri')
        self.assertEqual(self.fixed('6', every_weeks=2).repeat_label, 'Every 2 weeks on Sun')
        self.assertFalse(self.fixed('5').is_one_off)

    def test_first_due_on_or_after_start(self):
        self.fixed(str(self.today.weekday()))
        [chore] = generate_due_chores()
        self.assertEqual(chore.due_date, self.today)

    def test_late_or_early_completion_never_repeats_a_day(self):
        late = self.fixed(str((self.today - timedelta(days=1)).weekday()))
        Chore.objects.create(template=late, due_date=self.today - timedelta(days=1))
        complete_chore(late.chores.get(), self.user)  # a day late
        self.assertEqual(late.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=6))

        early = make_template(title='Early', every_days=None, weekdays=str((self.today + timedelta(days=2)).weekday()),
                              start_date=self.today)
        Chore.objects.create(template=early, due_date=self.today + timedelta(days=2))
        skip_chore(early.chores.get(), self.user)  # two days early
        self.assertEqual(early.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=9))

    def test_calendar_projections(self):
        self.fixed(str(self.today.weekday()))
        generate_due_chores()
        entries = calendar_entries(self.today, self.today + timedelta(days=14))
        days = sorted(day for day, items in entries.items() for _, t, _, _ in items if t.title == 'Mow lawn')
        self.assertEqual(days, [self.today, self.today + timedelta(days=7), self.today + timedelta(days=14)])

    def test_switching_on_goes_to_next_matching_day(self):
        template = self.fixed(str((self.today - timedelta(days=2)).weekday()))
        Chore.objects.create(template=template, due_date=self.today - timedelta(days=9),
                             completed_at=timezone.now() - timedelta(days=9))
        remove_template(template)
        enable_template(template)
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, self.today + timedelta(days=5))

    def test_create_through_form(self):
        response = self.client.post(reverse('chores:new_task'), {
            'title': 'Mow lawn', 'area': 'Yard', 'done_when': 'Grass cut', 'estimated_minutes': 45,
            'repeat': 'weekdays', 'weekdays': ['5'], 'every_weeks': 2, 'start_date': self.today.isoformat(),
        })
        self.assertRedirects(response, reverse('chores:dashboard'))
        template = ChoreTemplate.objects.get(title='Mow lawn')
        self.assertEqual((template.weekdays, template.every_weeks, template.every_days), ('5', 2, None))
        expected = self.today if self.today.weekday() == 5 else next_weekday(self.today, 5)
        self.assertEqual(template.chores.get().due_date, expected)

    def test_form_needs_a_day(self):
        response = self.client.post(reverse('chores:new_task'), {
            'title': 'Mow lawn', 'done_when': 'Grass cut', 'estimated_minutes': 45,
            'repeat': 'weekdays', 'every_weeks': 1, 'start_date': self.today.isoformat(),
        })
        self.assertContains(response, 'Pick at least one day.')
        self.assertFalse(ChoreTemplate.objects.exists())

    def test_edit_to_weekdays_reschedules_and_prefills(self):
        template = make_template(title='Vacuum', every_days=7)
        [first] = generate_due_chores()
        close(first, days_ago=3)
        [chore] = generate_due_chores()
        url = reverse('chores:edit_task', args=[template.pk])
        self.client.post(url, {
            'title': 'Vacuum', 'done_when': 'Floors clean', 'estimated_minutes': 30, 'repeat': 'weekdays',
            'weekdays': ['5'], 'every_weeks': 1, 'start_date': chore.due_date.isoformat(),
        })
        chore.refresh_from_db()
        self.assertEqual(chore.due_date, next_weekday(self.today, 5))
        form = self.client.get(url).context['form']
        self.assertEqual((form['repeat'].value(), form['weekdays'].value()), ('weekdays', ['5']))


def month_from(day, offset):
    """The month number `offset` months after day's month (1-12)."""
    return (day.month - 1 + offset) % 12 + 1


class SeasonTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def seasonal(self, start, end, **kwargs):
        return make_template(**{'title': 'Mow lawn', 'every_days': 7, 'season_start': start, 'season_end': end, **kwargs})

    def test_in_season_and_labels(self):
        summer = self.seasonal(4, 10)
        self.assertTrue(summer.in_season(date(2026, 10, 31)))
        self.assertFalse(summer.in_season(date(2026, 11, 1)))
        self.assertEqual(summer.season_label, 'Apr–Oct')
        self.assertEqual(summer.next_season_start(date(2026, 10, 20)), date(2027, 4, 1))
        winter = self.seasonal(11, 2)
        self.assertTrue(winter.in_season(date(2027, 1, 15)))
        self.assertTrue(winter.in_season(date(2026, 12, 1)))
        self.assertFalse(winter.in_season(date(2027, 3, 1)))
        self.assertEqual(winter.next_season_start(date(2026, 10, 20)), date(2026, 11, 1))
        self.assertEqual(self.seasonal(7, 7).season_label, 'Jul')
        self.assertTrue(make_template().in_season(date(2026, 1, 1)))

    def test_first_chore_waits_for_the_season(self):
        self.seasonal(4, 10)
        [chore] = generate_due_chores(date(2027, 2, 10))
        self.assertEqual(chore.due_date, date(2027, 4, 1))

    def test_fixed_days_land_on_a_chosen_day_in_season(self):
        make_template(title='Mow lawn', every_days=None, weekdays='5', start_date=date(2027, 2, 1),
                      season_start=4, season_end=10)
        [chore] = generate_due_chores(date(2027, 2, 10))
        self.assertEqual(chore.due_date, date(2027, 4, 3))  # the first Saturday in April

    def test_next_after_season_end_moves_to_next_season(self):
        template = self.seasonal(month_from(self.today, 0), month_from(self.today, 0), every_days=40)
        [chore] = generate_due_chores()
        complete_chore(chore, self.user)
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date,
                         date(self.today.year + 1, self.today.month, 1))

    def test_overdue_chore_is_dropped_when_season_ends(self):
        template = self.seasonal(4, 10)
        old = Chore.objects.create(template=template, due_date=date(2027, 10, 20))
        generate_due_chores(date(2027, 11, 2))
        self.assertFalse(Chore.objects.filter(pk=old.pk).exists())
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, date(2028, 4, 1))

    def test_overdue_chore_from_last_season_is_dropped_on_return(self):
        template = self.seasonal(4, 10)
        Chore.objects.create(template=template, due_date=date(2026, 10, 20),
                             completed_at=timezone.make_aware(datetime(2026, 10, 10, 12)))
        old = Chore.objects.create(template=template, due_date=date(2026, 10, 17))
        generate_due_chores(date(2027, 4, 2))  # nobody opened the app all winter
        self.assertFalse(Chore.objects.filter(pk=old.pk).exists())
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, date(2027, 4, 2))

    def test_overdue_chore_in_season_is_kept(self):
        template = self.seasonal(4, 10)
        chore = Chore.objects.create(template=template, due_date=date(2027, 6, 1))
        generate_due_chores(date(2027, 6, 9))
        self.assertEqual(template.chores.get(completed_at__isnull=True), chore)

    def test_season_and_away_dates_combine(self):
        template = self.seasonal(4, 10)
        AwayPeriod.objects.create(start=date(2027, 3, 30), end=date(2027, 4, 5))
        self.assertEqual(next_due_date(template, date(2027, 3, 25)), date(2027, 4, 6))
        AwayPeriod.objects.create(start=date(2027, 10, 28), end=date(2027, 11, 3))
        self.assertEqual(next_due_date(template, date(2027, 10, 28)), date(2028, 4, 1))

    def test_calendar_skips_off_season(self):
        template = self.seasonal(4, 10)
        Chore.objects.create(template=template, due_date=date(2027, 10, 25))
        entries = calendar_entries(date(2027, 10, 1), date(2027, 11, 30), today=date(2027, 10, 25))
        self.assertEqual(sorted(entries), [date(2027, 10, 25)])

    def test_season_change_reschedules_open_chore(self):
        template = make_template(title='Mow lawn', every_days=7)
        [chore] = generate_due_chores()
        next_month = month_from(self.today, 1)
        update_template(template, title='Mow lawn', area='Yard', done_when='Grass cut', estimated_minutes=30,
                        assignee=None, every_days=7, season_start=next_month, season_end=next_month)
        chore.refresh_from_db()
        self.assertEqual(chore.due_date, template.next_season_start(self.today))

    def test_create_through_form(self):
        response = self.client.post(reverse('chores:new_task'), {
            'title': 'Clear gutters', 'done_when': 'Gutters clear', 'estimated_minutes': 60, 'repeat': 'monthly',
            'season': 'seasonal', 'season_start': '11', 'season_end': '2', 'start_date': self.today.isoformat(),
        })
        self.assertRedirects(response, reverse('chores:dashboard'))
        template = ChoreTemplate.objects.get(title='Clear gutters')
        self.assertEqual((template.season_start, template.season_end), (11, 2))

    def test_all_year_through_form_clears_months(self):
        self.client.post(reverse('chores:new_task'), {
            'title': 'Dust', 'done_when': 'No dust', 'estimated_minutes': 10, 'repeat': 'weekly',
            'season': 'all', 'season_start': '4', 'season_end': '10', 'start_date': self.today.isoformat(),
        })
        template = ChoreTemplate.objects.get(title='Dust')
        self.assertFalse(template.is_seasonal)

    def test_form_needs_both_months(self):
        response = self.client.post(reverse('chores:new_task'), {
            'title': 'Mow lawn', 'done_when': 'Grass cut', 'estimated_minutes': 45, 'repeat': 'weekly',
            'season': 'seasonal', 'season_start': '4', 'season_end': '', 'start_date': self.today.isoformat(),
        })
        self.assertContains(response, 'Pick a month.')

    def test_chores_page_shows_season(self):
        next_month = month_from(self.today, 1)
        self.seasonal(next_month, next_month)
        name = ChoreTemplate.MONTH_NAMES[next_month - 1]
        response = self.client.get(reverse('chores:chore_list'))
        self.assertContains(response, f'{name} (back in {name})')


class SpreadTheLoadTests(TestCase):
    today = date(2027, 6, 1)

    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.client.force_login(self.user)

    def load(self, day, minutes, title='Other'):
        """An open chore of `minutes` due on `day`."""
        template = make_template(title=title, every_days=30, estimated_minutes=minutes)
        return Chore.objects.create(template=template, due_date=day)

    def done_on(self, template, day):
        Chore.objects.create(template=template, due_date=day,
                             completed_at=timezone.make_aware(datetime(day.year, day.month, day.day, 12)))

    def test_slide_goes_to_lightest_day_in_window(self):
        template = make_template(title='Mop', every_days=14)
        self.done_on(template, self.today)
        due = self.today + timedelta(days=14)
        self.load(due, 30)
        self.load(due + timedelta(days=1), 10, 'Other 2')
        self.load(due + timedelta(days=3), 0, 'Out of window')
        generate_due_chores(self.today)
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, due + timedelta(days=2))

    def test_slide_window_is_15_percent_of_interval(self):
        for every_days, slack in [(6, 0), (7, 1), (14, 2), (30, 4)]:
            template = make_template(title=f'Every {every_days}', every_days=every_days)
            due = self.today + timedelta(days=every_days)
            busy = [self.load(due + timedelta(days=i), 60, f'Busy {every_days} {i}') for i in range(slack)]
            self.assertEqual(slide_to_lighter_day(template, due), due + timedelta(days=slack))
            for chore in busy:
                chore.template.delete()

    def test_ties_keep_the_rule_date(self):
        template = make_template(title='Mop', every_days=14)
        due = self.today + timedelta(days=14)
        self.assertEqual(slide_to_lighter_day(template, due), due)

    def test_no_slide_for_first_chore_daily_and_fixed_days(self):
        self.load(self.today, 60)
        first = make_template(title='New', every_days=30)
        daily = make_template(title='Dishes', every_days=1)
        self.done_on(daily, self.today - timedelta(days=1))
        saturdays = make_template(title='Mow', every_days=None, weekdays='5', start_date=self.today)
        self.done_on(saturdays, self.today - timedelta(days=3))  # Sat 29 May
        generate_due_chores(self.today)
        self.assertEqual(first.chores.get().due_date, self.today)
        self.assertEqual(daily.chores.get(completed_at__isnull=True).due_date, self.today)
        self.assertEqual(saturdays.chores.get(completed_at__isnull=True).due_date, date(2027, 6, 5))

    def test_no_slide_for_pickups(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, date(2027, 6, 8), False)})
        template = PickupSchedule.objects.get().template
        template.chores.all().delete()
        self.done_on(template, self.today)
        self.load(date(2027, 6, 8), 60)
        generate_due_chores(self.today)
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, date(2027, 6, 8))

    def test_slide_skips_away_days(self):
        template = make_template(title='Mop', every_days=14)
        self.done_on(template, self.today)
        due = self.today + timedelta(days=14)
        self.load(due, 30)
        AwayPeriod.objects.create(start=due + timedelta(days=1), end=due + timedelta(days=1))
        self.load(due + timedelta(days=2), 20, 'Other 2')
        generate_due_chores(self.today)
        self.assertEqual(template.chores.get(completed_at__isnull=True).due_date, due + timedelta(days=2))

    def overdue(self, title, minutes, days_ago, every_days=7):
        template = make_template(title=title, every_days=every_days, estimated_minutes=minutes)
        return Chore.objects.create(template=template, due_date=self.today - timedelta(days=days_ago))

    def test_spread_fills_days_under_budget(self):
        a = self.overdue('A', 40, 5)
        b = self.overdue('B', 30, 3)
        c = self.overdue('C', 30, 2, every_days=30)
        dishes = self.overdue('Dishes', 10, 1, every_days=1)
        self.assertEqual(spread_overdue(60, self.today), (3, 2))
        for chore in (a, b, c, dishes):
            chore.refresh_from_db()
        tomorrow = self.today + timedelta(days=1)
        self.assertEqual([a.due_date, b.due_date, c.due_date], [self.today, tomorrow, tomorrow])
        self.assertEqual(dishes.due_date, self.today - timedelta(days=1))  # daily chores stay put

    def test_spread_counts_chores_already_due(self):
        self.load(self.today, 50)
        a = self.overdue('A', 20, 2)
        spread_overdue(60, self.today)
        a.refresh_from_db()
        self.assertEqual(a.due_date, self.today + timedelta(days=1))

    def test_spread_stays_within_interval_and_a_week(self):
        often = self.overdue('Every 2 days', 30, 1, every_days=2)
        self.load(self.today, 40)
        spread_overdue(10, self.today)  # no room anywhere: lightest day of its 2-day window
        often.refresh_from_db()
        self.assertEqual(often.due_date, self.today + timedelta(days=1))

        rare = self.overdue('Monthly', 30, 1, every_days=30)
        for i in range(7):
            self.load(self.today + timedelta(days=i), 100 - i, f'Busy {i}')
        spread_overdue(10, self.today)
        rare.refresh_from_db()
        self.assertEqual(rare.due_date, self.today + timedelta(days=6))

    def test_spread_skips_away_days_and_leaves_bins(self):
        save_pickup_schedules({PickupSchedule.GARBAGE: (1, date(2027, 6, 8), False)})
        bin_chore = PickupSchedule.objects.get().template.chores.get()
        bin_chore.due_date = self.today - timedelta(days=6)
        bin_chore.save()
        AwayPeriod.objects.create(start=self.today, end=self.today)
        a = self.overdue('A', 20, 2)
        spread_overdue(60, self.today)
        a.refresh_from_db()
        bin_chore.refresh_from_db()
        self.assertEqual(a.due_date, self.today + timedelta(days=1))
        self.assertEqual(bin_chore.due_date, self.today - timedelta(days=6))

    def test_suggest_takes_what_fits_in_order(self):
        chores = [self.overdue('A', 20, 3), self.overdue('B', 15, 2), self.overdue('C', 10, 1)]
        self.assertEqual(suggest_chores(chores, 30), [chores[0], chores[2]])

    def test_dashboard_suggests_for_mine(self):
        today = timezone.localdate()
        other = get_user_model().objects.create_user('sam', password='pw')
        mine = Chore.objects.create(template=make_template(title='Mine', estimated_minutes=10), due_date=today,
                                    assignee=self.user)
        Chore.objects.create(template=make_template(title='Theirs', estimated_minutes=10), due_date=today,
                             assignee=other)
        Chore.objects.create(template=make_template(title='Too long', estimated_minutes=40), due_date=today,
                             assignee=self.user)
        response = self.client.get(reverse('chores:dashboard'), {'who': 'mine', 'have': '30'})
        self.assertEqual(response.context['suggested'], [mine])
        self.assertContains(response, '1 chore · ~10 of 30 min')
        self.assertNotContains(response, 'Coming Up')

    def test_unknown_minutes_are_ignored(self):
        response = self.client.get(reverse('chores:dashboard'), {'have': '45'})
        self.assertIsNone(response.context['have'])

    def test_row_actions_keep_the_minutes(self):
        chore = Chore.objects.create(template=make_template(), due_date=timezone.localdate(), assignee=self.user)
        response = self.client.post(reverse('chores:mark_done', args=[chore.pk]) + '?who=mine&have=30')
        self.assertRedirects(response, reverse('chores:dashboard') + '?who=mine&have=30')
        response = self.client.post(reverse('chores:undo', args=[chore.pk]) + '?who=mine&have=30',
                                    HTTP_HX_REQUEST='true')
        self.assertContains(response, 'have=30')

    def test_spread_view(self):
        self.overdue('A', 20, 2).template.chores.update(due_date=timezone.localdate() - timedelta(days=2))
        response = self.client.post(reverse('chores:spread'), {'budget': 60}, HTTP_HX_REQUEST='true')
        self.assertContains(response, 'Spread 1 chore over 1 day')
        self.assertEqual(self.client.post(reverse('chores:spread'), {'budget': 0}).status_code, 400)


class StatsTests(TestCase):
    today = date(2027, 6, 30)

    def setUp(self):
        self.user = get_user_model().objects.create_user('alex', password='pw')
        self.sam = get_user_model().objects.create_user('sam', password='pw')
        self.client.force_login(self.user)
        self.template = make_template(title='Dishes', estimated_minutes=20)
        self.start = self.today - timedelta(days=27)

    def chore(self, due, done=None, by=None, skipped=False, template=None):
        completed_at = timezone.make_aware(datetime(done.year, done.month, done.day, 12)) if done else None
        return Chore.objects.create(template=template or self.template, due_date=due, completed_at=completed_at,
                                    completed_by=by, skipped=skipped)

    def test_outcomes(self):
        d = date(2027, 6, 20)
        self.chore(d, done=d)
        self.chore(d - timedelta(days=1), done=d, template=make_template(title='Late'))
        self.chore(d - timedelta(days=2), done=d, skipped=True, template=make_template(title='Skipped'))
        self.chore(d, template=make_template(title='Overdue'))
        self.chore(self.today, template=make_template(title='Due today'))  # not counted yet
        self.chore(self.start - timedelta(days=1), done=d, template=make_template(title='Before'))
        counts = stats.outcome_counts(self.start, self.today)
        self.assertEqual({k: counts[k] for k in ('on_time', 'late', 'skipped', 'overdue', 'total')},
                         {'on_time': 1, 'late': 1, 'skipped': 1, 'overdue': 1, 'total': 4})
        self.assertEqual(counts['on_time_pct'], 25)

    def test_household_streak(self):
        self.chore(self.start - timedelta(days=30), done=self.start - timedelta(days=30), template=make_template(title='First'))
        # Overdue at the end of 20 and 21 June, done on the 22nd.
        self.chore(date(2027, 6, 20), done=date(2027, 6, 22))
        self.chore(self.today, template=make_template(title='Due today'))  # today doesn't count yet
        current, best = stats.household_streak(self.start, self.today)
        self.assertEqual(current, 8)  # 22–29 June
        self.assertEqual(best, 17)  # 3–19 June (the range starts on the 3rd)

    def test_away_days_neither_count_nor_break_streak(self):
        self.chore(self.start - timedelta(days=30), done=self.start - timedelta(days=30), template=make_template(title='First'))
        self.chore(date(2027, 6, 20), done=date(2027, 6, 22))
        AwayPeriod.objects.create(start=date(2027, 6, 25), end=date(2027, 6, 26))
        self.assertEqual(stats.household_streak(self.start, self.today)[0], 6)
        self.chore(date(2027, 6, 25), done=date(2027, 6, 27), template=make_template(title='While away'))
        self.assertEqual(stats.household_streak(self.start, self.today)[0], 3)  # 27–29 June

    def test_streak_starts_with_the_first_chore(self):
        self.chore(date(2027, 6, 25), done=date(2027, 6, 25))
        self.assertEqual(stats.household_streak(self.start, self.today), (5, 5))  # 25–29 June
        Chore.objects.all().delete()
        self.assertEqual(stats.household_streak(self.start, self.today), (0, 0))

    def test_open_overdue_chore_breaks_streak(self):
        self.chore(date(2027, 6, 28))
        self.assertEqual(stats.household_streak(self.start, self.today)[0], 0)

    def test_weekly_minutes_by_person(self):
        self.chore(self.today, done=self.today, by=self.user)
        self.chore(self.start, done=self.start, by=self.sam, template=make_template(title='Old', estimated_minutes=5))
        self.chore(self.today, done=self.today, by=self.sam, skipped=True, template=make_template(title='Skip'))
        weeks = stats.weekly_minutes(self.start, self.today)
        self.assertEqual(len(weeks), 4)
        self.assertEqual((weeks[0].start, weeks[-1].end), (self.start, self.today))
        self.assertEqual([w.minutes for w in weeks], [5, 0, 0, 20])
        self.assertEqual(weeks[-1].by_person, {'alex': 20})

    def test_credit_goes_to_whoever_ticked_it(self):
        chore = self.chore(date(2027, 6, 20), done=date(2027, 6, 21), by=self.sam)
        chore.assignee = self.user
        chore.save()
        [row] = stats.by_person(self.start, self.today)
        self.assertEqual((row['name'], row['done'], row['minutes'], row['on_time_pct']), ('sam', 1, 20, 0))

    def test_by_chore_streak_and_days_late(self):
        self.chore(date(2027, 6, 5), done=date(2027, 6, 8))
        for day in (10, 15, 20):
            self.chore(date(2027, 6, day), done=date(2027, 6, day))
        on_time = make_template(title='Always on time')
        self.chore(date(2027, 6, 20), done=date(2027, 6, 19), template=on_time)
        dishes, good = stats.by_chore(self.start, self.today)
        self.assertEqual(dishes['template'], self.template)
        self.assertEqual((dishes['due'], dishes['done'], dishes['on_time_pct'], dishes['streak'], dishes['avg_late']),
                         (4, 4, 75, 3, 0.8))
        self.assertEqual((good['template'], good['streak'], good['avg_late']), (on_time, 1, 0.0))

    def test_page_renders_each_range(self):
        today = timezone.localdate()
        self.chore(today - timedelta(days=2), done=today - timedelta(days=1), by=self.user)
        for key in ('4w', '12w', 'year', 'bogus'):
            response = self.client.get(reverse('chores:stats'), {'range': key})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Minutes per Week')
        self.assertEqual(len(response.context['weeks']), 4)  # unknown range falls back to 4 weeks
        response = self.client.get(reverse('chores:stats'), {'range': 'year'})
        self.assertEqual(len(response.context['weeks']), 52)
        self.assertContains(response, '1 of 1 done · 0% on time')
