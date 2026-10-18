from django.core.management.base import BaseCommand

from chores.models import ChoreTemplate

# (area, title, done_when, estimated_minutes, every_days)
STARTER_TEMPLATES = [
    ('Kitchen', 'Do the dishes', 'Sink empty; dishes washed or in the dishwasher', 15, 1),
    ('Kitchen', 'Wipe counters and stovetop', 'Counters clear of clutter and wiped; stovetop wiped', 10, 2),
    ('Kitchen', 'Clean out the fridge', 'Expired food tossed; shelves wiped', 20, 14),
    ('Bathroom', 'Clean the toilet', 'Bowl scrubbed; seat and base wiped', 10, 7),
    ('Bathroom', 'Clean sink and mirror', 'Sink scrubbed; mirror streak-free', 10, 7),
    ('Bathroom', 'Clean shower/tub', 'Walls and tub scrubbed; drain cleared', 20, 14),
    ('Bedroom', 'Change bed sheets', 'Fresh sheets on; used sheets in the laundry', 15, 7),
    ('Living room', 'Declutter surfaces', 'Tables and couch clear of stray items', 10, 3),
    ('Whole house', 'Vacuum floors', 'All carpets and rugs vacuumed', 30, 7),
    ('Whole house', 'Mop hard floors', 'Kitchen and bathroom floors mopped', 20, 14),
    ('Laundry', 'Do a load of laundry', 'One load washed, dried, folded, and put away', 20, 3),
]


class Command(BaseCommand):
    help = 'Load a starter set of SMART chore templates (skipped if any templates exist).'

    def handle(self, *args, **options):
        if ChoreTemplate.objects.exists():
            self.stdout.write('Templates already exist; nothing seeded.')
            return
        ChoreTemplate.objects.bulk_create(
            ChoreTemplate(area=area, title=title, done_when=done_when,
                          estimated_minutes=minutes, every_days=every_days)
            for area, title, done_when, minutes, every_days in STARTER_TEMPLATES
        )
        self.stdout.write(self.style.SUCCESS(f'Seeded {len(STARTER_TEMPLATES)} templates.'))
