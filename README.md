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
- **Today dashboard.** Lists Overdue, Due today and Coming up (next 7 days), with an Everyone/Mine filter. Marking a chore done schedules the next one straight away. A toast with **Undo** follows Done and Skip for 10 seconds; undoing reopens the chore as it was and drops the next one it scheduled. The server accepts an undo for up to 10 minutes after the chore was closed, and only for that chore's most recent occurrence.
- **Calendar.** A month view, opened from the main menu (☰ → Calendar). It shows:
  - open chores, with overdue ones in red
  - chores already done or skipped, on the day they were done or skipped
  - **projected repeats**, outlined: future occurrences worked out from each chore's repeat rule

  Projections assume each chore is done on time. Pickup projections follow the pickup schedule. Everyone/Mine works here too, and on a phone the month turns into a list of only the days that have chores.
- **Custom tasks.** ☰ → New Task… adds your own chore. It has:
  - title, area and what "done" looks like
  - estimated minutes
  - a first due date
  - who to assign it to
  - how it repeats: **Once** (one-off), **Daily**, **Weekly**, **Monthly** (every 30 days), **Custom** (every N days) or **Weekdays** (on chosen days of the week, every week or every N weeks, e.g. every Saturday, or every other Sunday)

  Repeats count from when the last one was done, except Weekdays: those are due on the next chosen day after the later of the due date and the day it was done, so doing one late or early never makes it due again straight away. With every N weeks, the first due date decides which weeks count (weeks start on Monday). A one-off disappears once it's done.
- **Seasonal chores.** Set **Season** to **Seasonal** and pick a range of months, such as Apr–Oct for mowing or Nov–Feb for clearing gutters (a range can run past December). Outside its season a chore isn't scheduled: a due date that would fall outside the season moves to the first day of the next season (for Weekdays chores, the first chosen day from then), and a chore still open when its season ends is dropped, so it doesn't sit in Overdue all winter. The Chores page shows the season, and "back in Apr" while it's out of season. The on/off switch still works as before; a chore switched off stays off whatever the season.
- **Chores list.** ☰ → Chores lists every chore by area, with how often it repeats and an on/off switch. Switch off what doesn't apply to your home (no lawn, no stairs…); its history is kept. Switching a chore back on schedules it from today, so it never comes back overdue. Tap a chore to edit it, or use **New Task** to add your own. Bin chores are switched on and off on the pickup schedule instead.
- **Changing chores.** Each chore on the dashboard has a ⋮ menu:
  - **Skip This One** closes it without marking it done. The next one is due the repeat interval after today, or on the next pickup for bins. One-off chores can't be skipped.
  - **Move To** sets this one's due date. Only this occurrence moves.
  - **Edit Chore…** changes the title, area, "done when", minutes, repeat, season, next due date and default assignee. Changing how often it repeats or its season reschedules the open chore from the last time it was done, but never into the past, and leaves a date you moved by hand alone. Bin chores take their dates from the pickup schedule instead. **Turn Off** does the same as the switch on the Chores page.
- **Away dates.** ☰ → Away records days nobody is home. While away, no notifications are sent, bin pickups that fall in the range are skipped, and other chores due in it move to the day you get back. Chores that were already overdue stay as they are, and removing away dates doesn't move chores back. The calendar shades away days and the dashboard shows a banner.
- **Garbage, recycling and compost pickup.** Many cities don't collect every bin every week. Portland, OR, for example, collects recycling and compost weekly and garbage every other week. The pickup setup page asks:
  1. Are all bins picked up on the same schedule and day? If yes, you enter one schedule for all of them. If no, you enter a schedule for each bin.
  2. Do you also have compost/yard waste pickup?
  3. For each schedule: how often (weekly, every other week, or every N weeks), the next pickup day, and whether pickup is AM or PM. AM pickups are due the day before. PM pickups are due the morning of pickup.

  The dashboard prompts for this setup until it's done.
- **Household members.** Any active user account can be assigned chores. Superusers can add members from ☰ → New User…; switch on **Administrator** to let that person create users and use the admin site too. Each chore on the dashboard has an **Assign** button (the person icon): pick a person (or Unassigned) and tick **Also future ones** to make them the default for that chore's later occurrences too, or leave it unticked to change only this one. The **Mine** filter shows chores assigned to you; **Everyone** shows all.
- **Notifications.** A morning digest lists chores that are overdue or due today. An evening nudge lists whichever of them are still not done; AM-pickup bins are due the day before pickup, so they show up in that evening's nudge. Messages go to every destination in `NOTIFY_URLS`, for the whole household, and are skipped when nothing is due or you're away. Discord, Telegram, Signal and email all work through [Apprise](https://github.com/caronc/apprise); see [Notifications](#notifications).
- **GNOME-style interface.** Follows Adwaita / GNOME design patterns:
  - a header bar with a main menu
  - toasts confirming actions
  - grouped, rounded lists of rows
  - a round check button to complete a chore
  - popovers for assigning chores
  - pill-shaped primary buttons
  - a phone-friendly layout
- **Installable.** ChoreBot has an app icon and a web app manifest, so Chrome and Edge offer to install it, and it opens in its own window. Browsers only allow this over HTTPS or on `localhost`. To install it on a phone, serve it over HTTPS; on iPhone use Share → Add to Home Screen. There's no offline mode: it needs the server.
- **Light and dark style.** Uses the [Nord](https://www.nordtheme.com/) palette. Dark is the default. Switch in the main menu (Dark Style), and each browser remembers its own choice.
- **Admin.** Chore templates, chores and pickup schedules are managed in the Django admin at `/admin/`.

## Architecture

- Django 6.1 app (Python 3.13), with server-rendered templates and [htmx](https://htmx.org/), run by gunicorn with WhiteNoise for static files. htmx comes from unpkg. GNOME's [Adwaita Sans](https://gitlab.gnome.org/GNOME/adwaita-fonts) font (51.0, SIL Open Font License) is bundled in `chores/static/chores/fonts/`, converted to WOFF2. There's no frontend build step.
- PostgreSQL 17 database.
- A `notifier` service, from the same image, runs `manage.py send_notifications --loop` to send notifications on time.
- All of these run in Docker containers via Docker Compose.

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
docker compose exec web python manage.py send_notifications --test   # send a test message to NOTIFY_URLS now
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
| `NOTIFY_URLS` | Where notifications go: one or more Apprise URLs, separated by spaces. Empty sends nothing. |
| `SITE_URL` | Address you open ChoreBot at, e.g. `http://localhost:8000`. Notifications end with a link to the Today page there; empty means no link. |
| `NOTIFY_MORNING_AT`, `NOTIFY_EVENING_AT` | Local send times, default `08:00` and `19:00`. Leave one empty to turn that message off. |

In development, Compose runs Django's development server with the repo mounted into the container, so code changes reload live. The image's default command is gunicorn, and with `DJANGO_DEBUG=0` static files (the bundled font, the admin's CSS) are served by [WhiteNoise](https://whitenoise.readthedocs.io/). The Dockerfile runs `collectstatic` at build time, which makes compressed copies with hashed file names that browsers can cache indefinitely, so rebuild the image after changing anything in `static/`.

## Notifications

Put one Apprise URL per destination in `NOTIFY_URLS`, separated by spaces, then recreate the notifier so it rereads `.env` (`docker compose up -d --force-recreate notifier`) and check with `send_notifications --test`.

| Service | URL | Where the parts come from |
|---|---|---|
| Discord | `https://discord.com/api/webhooks/<id>/<token>` | Channel settings → Integrations → Webhooks → Copy Webhook URL. `discord://<id>/<token>` also works. |
| Telegram | `tgram://<bot_token>/<chat_id>` | Create a bot with @BotFather, send it a message, then find your chat ID at `https://api.telegram.org/bot<bot_token>/getUpdates`. |
| Email | `mailto://<user>:<password>@gmail.com?to=<address>` | Gmail needs an [app password](https://myaccount.google.com/apppasswords). Other providers: see Apprise's [email docs](https://github.com/caronc/apprise/wiki/Notify_email). |
| Signal | `signal://<host>:8080/<+from>/<+to>` | Signal has no official API: this needs a separately run [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) container with its own registered (or linked) phone number. It isn't part of this Compose setup. |

Escape characters with special meaning in URLs, such as `@` or `/` in a password, as `%40` or `%2F`.

Each message is sent at most once a day. If the notifier is down at the morning time, the digest goes out when it comes back, unless the evening time has already passed. A message that fails at some destination isn't retried; the failure appears in `docker compose logs notifier`. Sent messages are listed in the admin under Notification logs.
