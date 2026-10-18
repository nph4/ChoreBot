# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

ChoreBot is a vibe-coded webapp with two goals: learning AI-assisted development, and keeping the owner's house tidy. It generates **SMART** chores (Specific, Measurable, Assignable, Realistic, Time-bound).

## Stack

Django 6.1 (Python 3.13 image) + PostgreSQL 17, run with Docker Compose.

## Commands

All development runs through Docker Compose. Copy `.env.example` to `.env` first (`.env` is git-ignored).

```sh
docker compose up --build                          # start db + web on http://localhost:8000
docker compose exec web python manage.py migrate
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_templates     # starter chore templates (no-op if any exist)
docker compose exec web python manage.py generate_chores    # create missing open chores (dashboard also does this)
docker compose exec web python manage.py test                      # all tests
docker compose exec web python manage.py test chores.tests.SomeTest.test_x   # single test
```

## Layout

- `config/` — Django project (settings, root URLs, WSGI/ASGI).
- `chores/` — the main app.

## How chores work

Chores are rule-based (no AI). A `ChoreTemplate` is the SMART definition: `title`/`area` (specific), `done_when` (measurable), `default_assignee` (assignable, any Django user — all users are one household), `estimated_minutes` (realistic), `every_days` (time-bound). A `Chore` is one dated occurrence of a template.

`chores/services.py` owns the scheduling rule: each active template has at most one open chore (enforced by the `one_open_chore_per_template` DB constraint). `generate_due_chores()` creates the missing ones — due today if the template was never completed, otherwise last completion date + `every_days`. It is idempotent and runs on every dashboard load and after `complete_chore()`, so no cron is required.

Bin pickups are the exception: a `PickupSchedule` (garbage / recycling / compost, `every_weeks` + an anchor `next_pickup` date + `am_pickup`) is linked one-to-one to its chore template and overrides `every_days`. Due date = pickup day, or the day before for AM pickups. After a completion the next due date is searched from the day after max(last due date, completion date), so early or late completions never repeat a pickup. `save_pickup_schedules()` replaces the setup from the `/pickup/` form; removed bins keep their schedule row but their template is deactivated. The setup form shows or hides its sections with CSS `:has()`, not JS, so `ScheduleForm.use_required_attribute` is False.

UI is server-rendered templates + htmx (loaded from unpkg, no build step). The Done button `hx-post`s and the view returns `chores/_chore_lists.html` to replace `#chore-lists`; without htmx it redirects. CSRF for htmx is sent via `hx-headers` on `<body>`. Theming uses the Nord palette as CSS variables under `:root[data-theme=dark|light]` in `base.html`. Dark is the default, and a light choice is stored in the browser's `localStorage`. Templates/chores are managed in the Django admin.

## Configuration

`config/settings.py` reads everything from environment variables (see `.env.example`). `DJANGO_SECRET_KEY` is required with no default, so `manage.py` fails outside Compose unless the `.env` vars are exported. `DEBUG` is on only when `DJANGO_DEBUG=1`. `TIME_ZONE` decides what "today" means for due dates.

The Compose `web` service runs `runserver` and bind-mounts the repo into `/app`, so code changes reload live. The Dockerfile's default `CMD` is gunicorn, intended for non-dev runs. Postgres data persists in the `pgdata` named volume. The container runs as root, so files it writes into the bind mount (e.g. from `makemigrations`) are owned by root. Re-own them with `cp f f.tmp && mv -f f.tmp f`.
