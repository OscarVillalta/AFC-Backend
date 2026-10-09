# AGENTS.md

## Cursor Cloud specific instructions

This repo is a **Flask + SQLAlchemy inventory-management REST API** (Python). There is no
frontend, no test suite, and no linter configured. The single service is the backend API
defined under `backend/` with ORM models in `database/`.

### Python version (important)
- The project targets **Python 3.14** (see the original `.venv/pyvenv.cfg`, which points at
  `C:\Python314`). The Cloud VM has both `python3.12` and `python3.14`; **always use
  `python3.14`** for the venv (`.venv/bin/...` is created from 3.14 by the update script).
- Do **not** run the app under Python 3.12. `backend/app/api/qb_xml_parser.py` uses
  `defusedxml.ElementTree.Element` as a type annotation. Python 3.14 lazily evaluates
  annotations (PEP 649), so this never fails; Python 3.12 evaluates them eagerly and raises
  `AttributeError: module 'defusedxml.ElementTree' has no attribute 'Element'` at import,
  breaking `create_app()`. This is an environment (Python version) issue, not a code bug.

### Database
- The app reads the DB connection from the `DATABASE_URL` env var (`database/__init__.py`).
  The committed root `.env` sets `DATABASE_URL` to a **shared Neon cloud Postgres** DB.
- For local development the VM runs a **local Postgres** (db `afc_inventory`, user `postgres`,
  password `pass`). To use it without editing `.env`, export `DATABASE_URL` before running —
  `load_dotenv()` uses `override=False`, so an exported value wins over `.env`:
  `export DATABASE_URL="postgresql+psycopg2://postgres:pass@localhost:5432/afc_inventory"`
  Prefer the local DB so you don't mutate the shared cloud DB.
- Start the local Postgres cluster each VM session (not done by the update script):
  `sudo pg_ctlcluster 16 main start`
- **Migrations cannot bootstrap an empty DB.** The first Alembic migration
  (`666853b4f3a8_start`, `down_revision=None`) already references pre-existing `suppliers` /
  `air_filters` tables, so `alembic upgrade head` fails from scratch. To build a fresh local
  schema, create it from the ORM models (the source of truth) and then stamp Alembic:
  `python -c "import database, database.models; from database import Base, engine; Base.metadata.create_all(engine)"`
  then `cd database && alembic stamp head`. The Neon cloud DB already has the full schema.

### Seeding
- `backend/create_admin.py` seeds RBAC permissions, an `Admin` role, and admin user
  `admin@afc.com` / `AFC1110`. Note: it calls `app.run()` at **module level**, so after
  seeding it starts a blocking dev server on port 5000 — stop it (Ctrl+C) once you see
  `Database RBAC seeding complete`.

### Running the dev server
- Proper dev entrypoint (debug + reloader): `cd backend && python inventory_api.py`
  (binds `0.0.0.0:5000`). All routes are under the `/api` prefix. Requires `JWT_SECRET_KEY`
  (provided by the root `.env`).
- Quick smoke check: `curl -X POST localhost:5000/api/login -H 'Content-Type: application/json' -d '{"email":"admin@afc.com","password":"AFC1110"}'`

### Lint / test / build
- No automated tests and no lint config exist. A reasonable smoke check is
  `python -m compileall -q backend database`.
