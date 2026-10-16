# GRAND Explorer

The **Global Repository for Agricultural N2O Data (GRAND) Explorer** is a web platform for
exploring, inspecting and exporting harmonized soil nitrous oxide (N2O) flux measurements and
their covariates (soil, climate, crop and management).

Live site: [grandexplorer.org](https://grandexplorer.org) (currently in private preview)

## What it does

- **Filter and query** the database by instrument, crop, management and covariate availability.
- **Explore on a map** — every site plots on an interactive world map with its experiment and
  treatment details.
- **Preview datasets** — graphs of flux, temperature, moisture and fertilizer events.
- **Build a data cart** and download a zip with one clean CSV (every row carries its source
  citation), a README and a manifest of which values are modeled rather than measured.
- **Contribute data** — a guided upload workbench takes a manuscript and/or data file
  (CSV/XLSX), maps columns onto the harmonized schema, and sends submissions to staff curation
  before they are promoted into the live database.

## Data

GRAND brings together temporally resolved N2O flux measurements from several public sources —
GRACEnet, Ag Data Commons, the NANORP N2O Network and the Global N2O Database — plus roughly 45
additional peer-reviewed publications. Every record keeps its original source citation, which is
included with each download.

The database contents are **not** part of this repository, which contains only the platform code.

## Tech stack

- Python 3.12, Django 5.2 LTS, MySQL 8.0
- django-allauth (contributor accounts), django-sql-explorer (staff SQL console)
- Optional: Anthropic API for manuscript fact-extraction in the upload workbench (off by default)
- Production: Docker image served by Gunicorn + WhiteNoise, deployed on Railway
  ([`railway.json`](railway.json))

## Running locally

The development setup is a VS Code Dev Container with two services: `app` (Django) and `db`
(MySQL 8.0). You need Docker and VS Code with the Dev Containers extension.

**1. Create your `.env` file** in the repository root by copying the template, then replace the
placeholder passwords and secret key:

```bash
cp .env.example .env
```

`.env` is read by both containers and is gitignored — never commit it.

**2. Open the folder in VS Code** and choose **Reopen in Container**.

**3. Inside the `app` container** (VS Code's terminal, `/workspaces/fluxapp`):

```bash
python manage.py migrate
python manage.py createcachetable
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

The site is then at <http://localhost:8001>.

### Running the tests

Inside the `app` container:

```bash
python manage.py test n2odb
```

Django creates a separate test database, so the MySQL user needs privileges to create
databases. CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same suite
against MariaDB, plus `check --deploy` and a migration drift check.

## Configuration

All settings are read from environment variables (via `python-decouple`) in
[`fluxapp/settings.py`](fluxapp/settings.py). The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | — (required) | Django secret key |
| `DATABASE_URL` | — (required) | Main database connection |
| `DEBUG` | `False` | Development mode |
| `ALLOWED_HOSTS` | `localhost` | Comma-separated host names |
| `CSRF_TRUSTED_ORIGINS` | empty | Required in production |
| `READONLY_DATABASE_URL` | empty | SELECT-only user for the SQL Explorer console |
| `EMAIL_BACKEND` and `EMAIL_*`, `ANYMAIL_BREVO_API_KEY` | console backend | Outgoing mail (signup verification, contact form) |
| `CONTACT_EMAIL`, `DEFAULT_FROM_EMAIL` | placeholders | Contact form recipient and sender |
| `WORKBENCH_LLM_ENABLED`, `ANTHROPIC_API_KEY` | off | Manuscript pre-fill in the upload workbench |
| `WORKBENCH_ENRICH_ENABLED` | off | Abstract lookup by DOI |
| `USE_SHARED_CACHE` | off | Database cache, needed for rate limiting across workers |
| `SITE_PASSWORD`, `SITE_USERNAME` | empty | Optional site-wide password gate (preview deployments) |

With `DEBUG=False` the app refuses to start unless HTTPS and host settings are configured. See
[`docs/DEPLOY_CHECKLIST.md`](docs/DEPLOY_CHECKLIST.md) for the full production setup.

## Repository layout

| Path | Contents |
|---|---|
| `fluxapp/` | Django project settings and URLs |
| `n2odb/` | The application: models, views, upload workbench, templates, tests |
| `n2odb/management/commands/` | Geography backfill and flux analysis commands |
| `docs/` | Deployment checklist and design notes |
| `sample-data/` | Synthetic files for testing the upload workbench |

## Citing

If you use GRAND Explorer, please cite both the platform and the original data sources (every
downloaded row includes its source citation):

> Ackett, R. & Saha, D. (2026). The Global Repository for Agricultural N2O Data (GRAND)
> Explorer. Available at grandexplorer.org.

A DOI will be added with the first archived release.

## License

Copyright (C) 2026 Ryan Ackett

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. See [`LICENSE`](LICENSE).

In short: you may use, modify and share this code, but if you run a modified version — including
as a website — you must make your source code available under the same license.

### Third-party components

The AGPL covers this project's own code only. Third-party components keep their own licenses:

- **[Highcharts](https://www.highcharts.com/)** (charts and maps, loaded from CDN) is **not open
  source**. It is free for non-commercial use (personal, academic, non-profit); commercial use
  requires a [Highcharts license](https://shop.highcharts.com/). If you deploy a fork commercially,
  license Highcharts or swap it out.
- Loaded from CDN under permissive licenses: [Bootstrap](https://getbootstrap.com/) (MIT),
  [jQuery](https://jquery.com/) (MIT), [Font Awesome Free](https://fontawesome.com/license/free)
  (icons CC BY 4.0, fonts SIL OFL 1.1, code MIT), [Choices.js](https://github.com/Choices-js/Choices)
  (MIT), [driver.js](https://driverjs.com/) (MIT), [Grid.js](https://gridjs.io/) (MIT),
  [Plotly.js](https://plotly.com/javascript/) (MIT) and [proj4js](http://proj4js.org/) (MIT).
