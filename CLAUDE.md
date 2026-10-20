# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**`README.md` is the source of truth** for purpose, features, architecture, setup, commands and configuration. Read it first. When a change alters any of those, update the README in the same change. This file holds only working notes: implementation details and gotchas that the README doesn't need.

## Scheduling internals (`chores/services.py`)

- Each active template has at most one open chore, meaning `completed_at` is null. This is enforced by the `one_open_chore_per_template` DB constraint.
- `generate_due_chores()` fills in the missing chores. It's idempotent and runs on every dashboard load and after `complete_chore()`, so there's no cron.
- **Ordinary templates:** a chore is due today (or on `start_date`, if that's later) if the template has never been completed. Otherwise it's due on the last completion date + `every_days`.
- **One-off templates:** `every_days` is null. Once the template has a completed chore, `next_due_date()` returns None, and `generate_due_chores()` skips templates whose next due date is None.
- **Skipped chores** are closed like done ones (`completed_at` and `completed_by` set) with `skipped=True`, so every rule that counts from the last completion also counts from a skip. Check `skipped` wherever done and skipped must look different (the calendar).
- **Away periods:** `next_due_date()` passes its rule's date through `shift_out_of_away()`. A pickup moves to the first pickup after the period; anything else moves to the day after it ends. `add_away_period()` applies the same rule once to open chores due in the range from today on. Deleting a period changes nothing else.
- **`moved_by_hand`** is set by `move_chore()`. `update_template()` checks it before rescheduling on a frequency change, and nothing else does: away periods still move such chores.
- **Calendar:** `calendar_entries()` doesn't store anything. For each open chore it projects repeats forward from max(due date, today), and for pickups it steps `next_due_date()`. Completed chores appear on their local completion date. Projections go through `shift_out_of_away()` too. Keep projection logic consistent with `next_due_date()` when either changes.
- **Pickup templates:** a template with a linked `PickupSchedule` (one-to-one) ignores `every_days`. `next_pickup` is only an anchor date for the cycle.
  - The next due date is searched from the day after max(last due date, completion date), so an early or late completion never repeats a pickup.
  - `save_pickup_schedules()` replaces the whole setup and deletes open chores so they regenerate.
  - A removed bin keeps its schedule row, but its template is deactivated so re-adding the bin reuses it. Treat "configured bins" as schedules whose template is active (`active_pickup_schedules()`).

## Notifications (`chores/notifications.py`)

- `send_due_notification()` claims the (kind, date) `NotificationLog` row before sending, and keeps it even if nothing was due or a destination failed. That's what prevents duplicates and retry storms, so don't add retries without per-destination tracking.
- It calls `generate_due_chores()` itself, since nobody may have loaded the dashboard yet that day.
- Tests patch `chores.notifications.send`. To try real sends without an outside service, point `NOTIFY_URLS` at `json://<host>:<port>` and run a small HTTP server on the Compose network to print what arrives.
- With `DEBUG` off, the test runner prints WhiteNoise's "No directory at: /app/staticfiles/" warning under Compose, because the bind mount hides the image's collected files. It's harmless.

## UI

- **Row actions (Done, Assign):** each `hx-post`s with `?who=` preserved, and `_lists_response()` in `views.py` returns `chores/_chore_lists.html` to replace `#chore-lists`, so the Mine/Everyone filter re-applies. Without htmx it redirects instead. `assign_chore()` changes only the chore unless `include_future`, which also sets the template's `default_assignee`. htmx sends the CSRF token through `hx-headers` on `<body>`.
- **`/pickup/` form:** sections are shown or hidden with CSS `:has()`, with no JS. So hidden inputs must not carry `required`, which is why `ScheduleForm.use_required_attribute = False`. Inactive sections still post data. The view validates only the forms that the answers select.
- **Form pages:** render fields with `chores/_form_rows.html` inside a `.boxed-list`. It picks the row layout from `field.widget_type`: radio choices become linked toggles, checkboxes become switches, and text and password inputs stack under their label. Each row gets a `field-<name>` class for page-level `:has()` show/hide rules.
- **Row ⋮ menu (Skip, Move, Edit):** Skip and Move work like Done and Assign (htmx, then `_lists_response()`). The Edit page reuses `task_form.html` through `EditTaskForm`, which drops the repeat and due fields for pickup chores. Remove Chore is a separate form below the main one, because forms can't be nested.
- **Chores page switches** are submit buttons with `role="switch"` and `aria-checked` inside a form that posts the new state, so they work without JS. With htmx, `set_active` returns `chores/_template_row.html` to replace just that row. `enable_template()` (switch on) schedules from today rather than from the last completion, unlike `generate_due_chores()`.
- **Feedback:** use `messages.success()` for confirmations. `base.html` renders messages as toasts.
- **Permissions:** `superuser_required` in `views.py` returns 403 to non-superusers. Hide matching menu items with `user.is_superuser`.
- **Theme:** the Nord colors are CSS variables under `:root[data-theme=dark|light]` in `base.html`. A script in `<head>` applies the saved choice before first paint.
- **Design language:** all styles live in `base.html` and copy libadwaita widget classes. Reuse them in new templates instead of adding one-off styles.
  - Page structure: `.headerbar`, `.banner`, `.clamp`.
  - Lists: `.group` > `.heading` + `.boxed-list` > `.row` (with `.row-content`, `.row-subtitle` and `.suffix`).
  - Buttons: `.button` with modifiers `.flat`, `.suggested`, `.pill` and `.icon-only`, plus `.linked` > `.toggle` for segmented choices.
  - Popovers and menus: `details.popover-menu` > `.popover`, with `.menu-item` entries.
  - Labels follow GNOME's capitalization rules: headings and buttons use header capitalization, everything else uses sentence case.
  - Popovers are `<details>` elements. A script in `base.html` closes them on an outside click or Escape.
- **Previewing pages without the browser extension:** render a view through `RequestFactory(HTTP_HOST='localhost')` in `manage.py shell`, save the HTML, then run `google-chrome --headless=new --screenshot`.

## Environment gotchas

- `DJANGO_SECRET_KEY` has no default, so `manage.py` outside Compose needs the `.env` vars exported.
- The container runs as root. Files it writes into the bind mount, such as from `makemigrations`, end up owned by root. Re-own them with `cp f f.tmp && mv -f f.tmp f`.
- Django's static file finder only picks up app `static/` folders that existed when the server started. After adding one, restart with `docker compose restart web`.
- Static storage depends on `DJANGO_DEBUG` at settings load. With 1 (Compose dev) it's plain storage. With 0 it's WhiteNoise's manifest storage, where `{% static %}` raises an error for any file missing from the `collectstatic` output, so a new static file needs an image rebuild. The bind mount hides the image's `staticfiles/`, which is why `StaticFilesTests` turns on `WHITENOISE_USE_FINDERS`.
- To check production behaviour, run the built image directly (it uses gunicorn) on the Compose network: `docker run --rm --network chore-bot_default --env-file .env -e DJANGO_DEBUG=0 -p 127.0.0.1:8001:8000 chore-bot-web`.
- The remote is SSH (`git@github.com:nph4/ChoreBot.git`). HTTPS pushes can't prompt for credentials here.
