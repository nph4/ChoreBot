# ChoreBot

## Purpose

ChoreBot is a vibe-coded webapp with two purposes: learning AI skills, and cleaning my messy house.

The goal is to generate SMART (Specific, Measurable, Assignable, Realistic, Time-bound) chores to help me keep my house tidy.

## Features

- **SMART chore templates.** Each recurring chore defines:
  - what to do and where (specific)
  - what "done" looks like (measurable)
  - who it's for (assignable)
  - roughly how long it takes (realistic)
  - how often it repeats (time-bound)

  Chores are generated from these templates by rules, with no AI.
- **Today dashboard.** Lists Overdue, Due today and Coming up (next 7 days), with an Everyone/Mine filter. Marking a chore done schedules the next one straight away.
- **Calendar.** A month view, opened from the main menu (☰ → Calendar). It shows:
  - open chores, with overdue ones in red
  - chores already done, on the day they were done
  - **projected repeats**, outlined: future occurrences worked out from each chore's repeat rule

  Projections assume each chore is done on time. Pickup projections follow the pickup schedule. Everyone/Mine works here too, and on a phone the month turns into a list of only the days that have chores.
- **Custom tasks.** ☰ → New Task… adds your own chore. It has:
  - title, area and what "done" looks like
  - estimated minutes
  - a first due date
  - who to assign it to
  - how it repeats: **Once** (one-off), **Daily**, **Weekly**, **Monthly** (every 30 days) or **Custom** (every N days)

  Repeats count from when the last one was done. A one-off disappears once it's done.
- **Garbage, recycling and compost pickup.** Many cities don't collect every bin every week. Portland, OR, for example, collects recycling and compost weekly and garbage every other week. The pickup setup page asks:
  1. Are all bins picked up on the same schedule and day? If yes, you enter one schedule for all of them. If no, you enter a schedule for each bin.
  2. Do you also have compost/yard waste pickup?
  3. For each schedule: how often (weekly, every other week, or every N weeks), the next pickup day, and whether pickup is AM or PM. AM pickups are due the day before. PM pickups are due the morning of pickup.

  The dashboard prompts for this setup until it's done.
- **Household members.** Any active user account can be assigned chores. Superusers can add members from ☰ → New User…; switch on **Administrator** to let that person create users and use the admin site too. Each chore on the dashboard has an **Assign** button (the person icon): pick a person (or Unassigned) and tick **Also future ones** to make them the default for that chore's later occurrences too, or leave it unticked to change only this one. The **Mine** filter shows chores assigned to you; **Everyone** shows all.
- **GNOME-style interface.** Follows Adwaita / GNOME design patterns:
  - a header bar with a main menu
  - toasts confirming actions
  - grouped, rounded lists of rows
  - a round check button to complete a chore
  - popovers for assigning chores
  - pill-shaped primary buttons
  - a phone-friendly layout
- **Light and dark style.** Uses the [Nord](https://www.nordtheme.com/) palette. Dark is the default. Switch in the main menu (Dark Style), and each browser remembers its own choice.
- **Admin.** Chore templates, chores and pickup schedules are managed in the Django admin at `/admin/`.

## Architecture

- Django 6.1 app (Python 3.13), with server-rendered templates and [htmx](https://htmx.org/). htmx comes from unpkg. The [Inter](https://rsms.me/inter/) font (v4.1, SIL Open Font License) is bundled in `chores/static/chores/fonts/`. There's no frontend build step.
- PostgreSQL 17 database.
- Both run in Docker containers via Docker Compose.

| Path | Contents |
|---|---|
| `config/` | Django project: settings and root URLs |
| `chores/` | The app: models, scheduling rules (`services.py`), views, templates, management commands, tests |

## Getting started

```sh
cp .env.example .env        # then set DJANGO_SECRET_KEY, POSTGRES_PASSWORD, TIME_ZONE
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_templates     # optional starter chores
```

Open http://localhost:8000, log in, and set up your pickup schedule.

## Commands

```sh
docker compose exec web python manage.py test chores                                   # all tests
docker compose exec web python manage.py test chores.tests.MarkDoneTests.test_get_is_not_allowed   # one test
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py seed_templates     # no-op if any templates exist
docker compose exec web python manage.py generate_chores    # create missing chores (the dashboard also does this)
```

## Configuration

Settings come from environment variables in `.env` (see `.env.example`):

| Variable | Meaning |
|---|---|
| `DJANGO_SECRET_KEY` | Required |
| `DJANGO_DEBUG` | `1` turns debug on; it's off by default |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated host names |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` | Database connection |
| `TIME_ZONE` | e.g. `America/Los_Angeles`. Decides what "today" is for due dates. |

In development, Compose runs Django's development server with the repo mounted into the container, so code changes reload live. The image's default command is gunicorn. Static files (the bundled font) are served by Django only when `DJANGO_DEBUG=1`; a non-debug deployment needs a static file server such as WhiteNoise.
