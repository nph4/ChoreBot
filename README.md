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
- **Garbage, recycling and compost pickup.** Many cities don't collect every bin every week. Portland, OR, for example, collects recycling and compost weekly and garbage every other week. The pickup setup page asks:
  1. Are all bins picked up on the same schedule and day? If yes, you enter one schedule for all of them. If no, you enter a schedule for each bin.
  2. Do you also have compost/yard waste pickup?
  3. For each schedule: how often (weekly, every other week, or every N weeks), the next pickup day, and whether pickup is AM or PM. AM pickups are due the day before. PM pickups are due the morning of pickup.

  The dashboard prompts for this setup until it's done.
- **Household members.** Any user account can be assigned chores.
- **Light and dark mode.** Uses the [Nord](https://www.nordtheme.com/) palette. Dark is the default, and each browser remembers its own choice.
- **Admin.** Chore templates, chores and pickup schedules are managed in the Django admin at `/admin/`.

## Architecture

- Django 6.1 app (Python 3.13), with server-rendered templates and [htmx](https://htmx.org/).
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

In development, Compose runs Django's development server with the repo mounted into the container, so code changes reload live. The image's default command is gunicorn.
