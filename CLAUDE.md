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
docker compose exec web python manage.py test                      # all tests
docker compose exec web python manage.py test chores.tests.SomeTest.test_x   # single test
```

## Layout

- `config/` — Django project (settings, root URLs, WSGI/ASGI).
- `chores/` — the main app.

## Configuration

`config/settings.py` reads everything from environment variables (see `.env.example`). `DJANGO_SECRET_KEY` is required with no default, so `manage.py` fails outside Compose unless the `.env` vars are exported. `DEBUG` is on only when `DJANGO_DEBUG=1`.

The Compose `web` service runs `runserver` and bind-mounts the repo into `/app`, so code changes reload live. The Dockerfile's default `CMD` is gunicorn, intended for non-dev runs. Postgres data persists in the `pgdata` named volume.
