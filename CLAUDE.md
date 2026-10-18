# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**`README.md` is the source of truth** for purpose, features, architecture, setup, commands and configuration. Read it first. When a change alters any of those, update the README in the same change. This file holds only working notes: implementation details and gotchas that the README doesn't need.

## Scheduling internals (`chores/services.py`)

- Each active template has at most one open chore, meaning `completed_at` is null. This is enforced by the `one_open_chore_per_template` DB constraint.
- `generate_due_chores()` fills in the missing chores. It's idempotent and runs on every dashboard load and after `complete_chore()`, so there's no cron.
- **Ordinary templates:** a chore is due today if the template has never been completed. Otherwise it's due on the last completion date + `every_days`.
- **Pickup templates:** a template with a linked `PickupSchedule` (one-to-one) ignores `every_days`. `next_pickup` is only an anchor date for the cycle.
  - The next due date is searched from the day after max(last due date, completion date), so an early or late completion never repeats a pickup.
  - `save_pickup_schedules()` replaces the whole setup and deletes open chores so they regenerate.
  - A removed bin keeps its schedule row, but its template is deactivated so re-adding the bin reuses it. Treat "configured bins" as schedules whose template is active (`active_pickup_schedules()`).

## UI

- **Done button:** it `hx-post`s, and the view returns `chores/_chore_lists.html` to replace `#chore-lists`. Without htmx it redirects instead. htmx sends the CSRF token through `hx-headers` on `<body>`.
- **`/pickup/` form:** sections are shown or hidden with CSS `:has()`, with no JS. So hidden inputs must not carry `required`, which is why `ScheduleForm.use_required_attribute = False`. Inactive sections still post data. The view validates only the forms that the answers select.
- **Theme:** the Nord colors are CSS variables under `:root[data-theme=dark|light]` in `base.html`. A script in `<head>` applies the saved choice before first paint.

## Environment gotchas

- `DJANGO_SECRET_KEY` has no default, so `manage.py` outside Compose needs the `.env` vars exported.
- The container runs as root. Files it writes into the bind mount, such as from `makemigrations`, end up owned by root. Re-own them with `cp f f.tmp && mv -f f.tmp f`.
- The remote is SSH (`git@github.com:nph4/ChoreBot.git`). HTTPS pushes can't prompt for credentials here.
