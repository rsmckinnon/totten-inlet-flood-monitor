# Render setup

Create a Python Web Service connected to this repository.

- Branch: main
- Root directory: leave blank
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Use a paid instance if attaching a persistent disk.
- For the current cache path, mount the disk at `/opt/render/project/src/.cache`.

Copy the settings from your local `.env` into Render's Environment settings.
Do not upload `.env` to GitHub. `.env.example` shows the supported settings,
with sample values, not your configured property settings.

This is the current working application. Shared response caching, automatic
old-file cleanup, and more resilient handling of forecast-service outages
remain recommended improvements before wider public use. NOAA downloads can
make the first request slow; the cache will refill on the host automatically.

Validate the hosted dashboard against the local one before sharing its URL.
The public risk tile and API expose the configured flooding threshold.

## Local tests

`python -m unittest discover -s tests -v`
