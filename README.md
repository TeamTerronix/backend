# SLIOT Backend

FastAPI backend for temperature monitoring, predictions, auth, and scheduled jobs.

## Prerequisites

- Python 3.10+ (3.11 recommended)
- Git
- Optional: PostgreSQL (if you do not want to use local SQLite)

## 1. Clone the Repository

```bash
git clone https://github.com/TeamTerronix/backend.git
cd backend
```

## 2. Create and Activate a Virtual Environment

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Windows (CMD)

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

### macOS/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Install Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Configure Environment Variables

Create a `.env` file from the sample:

```bash
cp .env.example .env
```

If `cp` is not available on Windows CMD, use:

```cmd
copy .env.example .env
```

Update `.env` values as needed:

- `DATABASE_URL`
- `SECRET_KEY`
- `ALGORITHM`
- `ACCESS_TOKEN_EXPIRE_MINUTES`

On a server (EC2), set `DATABASE_URL` to your **PostgreSQL / RDS** URL before running scripts or `uvicorn`, or tools will default to **local SQLite** (`./sliot.db`) and you will not see data in RDS.

### Database options

- SQLite (default, easiest local setup):
  - `DATABASE_URL=sqlite:///./sliot.db`
- PostgreSQL (recommended for shared/dev/prod):
  - `DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/sliot`

## 5. Create Database Tables

Run once after setting `DATABASE_URL`:

```bash
python create_tables.py
```

## 5b. (Schema updates) Run migrations scripts when needed

If you pull changes that add new columns (example: `network_group_id`), run:

```bash
python migrate_add_network_group_id.py
```

## 5a. (Optional) Seed the Database with Test Data

If you want test users/sensors/readings/predictions in your PostgreSQL DB for dashboard testing:

```bash
python seed_data.py
```

## 5c. Prototype sensor + user (ESP32 / field devices)

Registers one dashboard user, one network, and one **approved** sensor so devices can `POST /data`.

```bash
python provision_prototype.py --sensor-uid "1"
```

Defaults: `prototype@sliot.local` / `proto123`. If login fails for an **existing** user, reset the password:

```bash
python provision_prototype.py --reset-password --user-password proto123
```

`provision_prototype.py` loads `backend/.env` **before** connecting, so `DATABASE_URL` in `.env` applies to provisioning.

## 5d. Sensor ingest (`POST /data`)

Devices send JSON to **`POST /data`** (no JWT). The body must include `sensor_uid` and `temperature`. `timestamp` is **optional**; if omitted, the server uses current UTC (hour-bucketed for deduplication).

Example:

```bash
curl -sS -X POST "http://127.0.0.1:8000/data" \
  -H "Content-Type: application/json" \
  -d '{"sensor_uid":"1","temperature":29.0}'
```

`sensor_uid` must match a **registered and approved** sensor (see `provision_prototype.py`). OpenAPI: `/docs` → `POST /data`.

ESP32: `reciever_single_temp.ino` POSTs to `/data` with sensor id + temperature only; time is stored on the server.

## 6. Run the API

```bash
uvicorn main:app --reload --env-file .env
```

Server will start at:

- `http://127.0.0.1:8000`
- Swagger docs: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Notes

- Background scheduler jobs start automatically with the app (see `scheduler.py`).
- `.env` is ignored by Git through `.gitignore`.
- Some endpoints load CSV/model assets from a sibling `../model` directory. Make sure that folder exists if you use those endpoints.

## Quick Health Check

After starting the server:

```bash
curl http://127.0.0.1:8000/
```

Expected response includes `status: "ok"`.
