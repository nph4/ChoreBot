from django.core.management.base import BaseCommand

from chores.services import generate_due_chores


class Command(BaseCommand):
    help = 'Create the next open chore for every active template that lacks one.'

    def handle(self, *args, **options):
        created = generate_due_chores()
        self.stdout.write(self.style.SUCCESS(f'Created {len(created)} chore(s).'))
