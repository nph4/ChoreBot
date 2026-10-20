import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from chores.notifications import _parse_time, send, send_due_notification

CHECK_EVERY_SECONDS = 60


class Command(BaseCommand):
    help = 'Send the morning digest or evening nudge if one is due and not yet sent today.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true', help=f'Keep checking every {CHECK_EVERY_SECONDS} seconds.')
        parser.add_argument('--test', action='store_true', help='Send a test message to every NOTIFY_URLS destination now.')

    def handle(self, *args, **options):
        for name in ('NOTIFY_MORNING_AT', 'NOTIFY_EVENING_AT'):
            try:
                _parse_time(getattr(settings, name))
            except ValueError:
                raise CommandError(f'{name} must be a time like 08:00, or empty.')
        if not settings.NOTIFY_URLS:
            if options['test']:
                raise CommandError('NOTIFY_URLS is empty.')
            self.stdout.write('NOTIFY_URLS is empty; nothing will be sent.')
        if options['test']:
            if not send('ChoreBot test', 'Notifications are working.'):
                raise CommandError('At least one destination failed; see the warnings above.')
            self.stdout.write(self.style.SUCCESS('Test message sent.'))
            return
        while True:
            self.check_once()
            if not options['loop']:
                return
            time.sleep(CHECK_EVERY_SECONDS)

    def check_once(self):
        close_old_connections()  # the loop outlives any single DB connection
        try:
            log = send_due_notification()
        except Exception as exc:  # keep the loop alive; the next check retries
            self.stderr.write(f'Notification check failed: {exc!r}')
            return
        if log is None:
            return
        if not log.chore_count:
            self.stdout.write(f'{log}: nothing due, not sent.')
        elif log.delivered:
            self.stdout.write(self.style.SUCCESS(f'{log}: sent ({log.chore_count} chores).'))
        else:
            self.stderr.write(f'{log}: at least one destination failed; not retrying today.')
