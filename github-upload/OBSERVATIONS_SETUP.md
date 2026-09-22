# Local Tide Observations — one-time setup

The feature is disabled until a durable PostgreSQL database and write key are configured. No observations are ever stored in Render's ephemeral filesystem or forecast cache. No Render plan/billing change is required by the code, and none has been made.

## Smallest appropriate storage

Use one small database on an existing durable PostgreSQL service, or provision an external managed PostgreSQL database with non-expiring storage under your chosen provider/account. Only one table and index are needed. Do not use Render's 30-day Free Postgres database for permanent records. Choosing/provisioning a new account or paid database remains the owner's action; this update does not purchase anything.

## Exact setup

1. Obtain a PostgreSQL connection URL for that database. Use the provider's TLS-enabled connection string (for external connections, retain `sslmode=require` or preferably the provider's certificate-verified setting). Keep it secret.
2. Run `001_local_tide_observations.sql` once in the database provider's SQL editor, or with `psql "$OBSERVATIONS_DATABASE_URL" -v ON_ERROR_STOP=1 -f 001_local_tide_observations.sql`. The script is safe to rerun and does not delete existing records.
3. In Render → **totten-inlet-flood-monitor** → **Environment** → **Edit**, add:
   - `OBSERVATIONS_DATABASE_URL`: the full connection URL.
   - `OBSERVATIONS_WRITE_KEY`: a long random secret you choose and keep in your password manager (at least 32 random characters recommended).
4. Choose **Save, rebuild, and deploy** (or save and deploy the latest commit). Keep the existing compute plan, build/start commands and property threshold unchanged.
5. Open the dashboard, expand **Local Tide Observations**, and confirm **Persistent observation storage is connected**. If unavailable, check the URL, network access, table migration and database permissions. Do not enter your first reading until connected.
6. Enter your actual observation time, signed inches, optional note and access key. Confirm **Observation saved to persistent storage**, then refresh the page and verify the reading appears. A submission is only acknowledged after its database transaction commits.

The date/time defaults to the device's local time on first expansion; the displayed time-zone label makes this explicit. The API stores timezone-aware observation timestamps and a separate database-generated submission timestamp. The fixed datum is property flood-entry threshold = 0 inches. The existing forecast's 17.5-ft MLLW setting is independent and unchanged.

## Retention and access

Raw values are append-only through the API; there are no edit/delete endpoints, retention cutoff, or forecast-derived values. Up to 10 recent records appear in the tile (API supports 1–100); older records remain in the database. UUID submission IDs make unchanged retries idempotent. The API validates finite readings from −1200 to +1200 inches, up to four decimal places, offset-bearing timestamps no more than five minutes ahead, and notes up to 2000 characters.

The dashboard and recent observation list are public, including notes. Saving requires the write key; it is never embedded in the page or saved in browser storage. Configure the database application role with only SELECT and INSERT on the table after applying the migration, if your provider supports separate roles. Store backups/export records with your database provider: application retention cannot protect against deleting or allowing the database account to expire. For corrections, append a new reading and reference the earlier observation in the note.

## Deployment and testing

Render follows `main` from rsmckinnon/totten-inlet-flood-monitor. The application lives under `github-upload/`. Install its requirements and run `python -m unittest discover -s tests -v` there. Database setup is deliberately explicit; the web app does not perform schema changes on startup. Connections are short-lived with five-second connection/query timeouts and run in FastAPI's thread pool, leaving forecast processing and its memory cache unchanged.
