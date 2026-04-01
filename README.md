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
