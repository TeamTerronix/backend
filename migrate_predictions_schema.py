"""
migrate_predictions_schema.py
=============================
Migrate predictions table to add new bleaching risk assessment columns.

Run this ONCE to add columns:
    cd backend
    python migrate_predictions_schema.py

This script:
1. Backs up the database (if SQLite)
2. Adds new columns: risk_score, anomaly, days_stressed, warming_rate
3. Sets default values for existing predictions (NULL)
"""

import os
import shutil
from datetime import datetime

from sqlalchemy import text
from database import engine, DATABASE_URL, Base
from models import Prediction

def backup_database():
    """Create a backup of SQLite database before migration."""
    if DATABASE_URL.startswith("sqlite"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        if os.path.exists(db_path):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{db_path}.backup.{timestamp}"
            shutil.copy2(db_path, backup_path)
            print(f"✓ Database backed up to: {backup_path}")
            return backup_path
    return None


def add_columns_postgres():
    """Add columns for PostgreSQL."""
    with engine.connect() as conn:
        statements = [
            "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS risk_score FLOAT;",
            "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS anomaly FLOAT;",
            "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS days_stressed INTEGER;",
            "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS warming_rate FLOAT;",
        ]
        
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                print(f"✓ {stmt.strip()}")
            except Exception as e:
                print(f"⚠ {stmt.strip()} → {str(e)}")
        
        conn.commit()


def add_columns_sqlite():
    """Add columns for SQLite."""
    with engine.connect() as conn:
        statements = [
            "ALTER TABLE predictions ADD COLUMN risk_score FLOAT DEFAULT NULL;",
            "ALTER TABLE predictions ADD COLUMN anomaly FLOAT DEFAULT NULL;",
            "ALTER TABLE predictions ADD COLUMN days_stressed INTEGER DEFAULT NULL;",
            "ALTER TABLE predictions ADD COLUMN warming_rate FLOAT DEFAULT NULL;",
        ]
        
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                print(f"✓ {stmt.strip()}")
            except Exception as e:
                # Column might already exist
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    print(f"ℹ Column already exists: {stmt.split('ADD COLUMN')[1].split()[0]}")
                else:
                    print(f"⚠ Error: {str(e)}")
        
        conn.commit()


def verify_schema():
    """Verify columns were added."""
    print("\n📋 Verifying new schema...")
    with engine.connect() as conn:
        if DATABASE_URL.startswith("sqlite"):
            result = conn.execute(text("PRAGMA table_info(predictions);")).fetchall()
            columns = {row[1]: row[2] for row in result}
        else:  # PostgreSQL
            result = conn.execute(text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'predictions';"
            )).fetchall()
            columns = {row[0]: row[1] for row in result}
        
        # Check new columns
        new_cols = ['risk_score', 'anomaly', 'days_stressed', 'warming_rate']
        for col in new_cols:
            if col in columns:
                print(f"  ✓ {col}: {columns[col]}")
            else:
                print(f"  ✗ {col}: MISSING")
        
        return all(col in columns for col in new_cols)


def main():
    print("=" * 60)
    print("SLIOT Predictions Schema Migration")
    print("=" * 60)
    print(f"\nDatabase: {DATABASE_URL}\n")
    
    # Backup
    backup_database()
    
    # Determine database type and add columns
    print("\n📝 Adding new columns...")
    if DATABASE_URL.startswith("postgresql"):
        add_columns_postgres()
    else:
        add_columns_sqlite()
    
    # Verify
    if verify_schema():
        print("\n✅ Migration completed successfully!")
        print("\nNew columns added to 'predictions' table:")
        print("  - risk_score (FLOAT): 0-1 continuous bleaching risk")
        print("  - anomaly (FLOAT): °C above baseline temperature")
        print("  - days_stressed (INTEGER): consecutive warm days")
        print("  - warming_rate (FLOAT): °C/day warming speed")
    else:
        print("\n⚠ Migration completed with warnings. Check above.")
    
    print("\n💡 Next steps:")
    print("  1. Restart the backend: python -m uvicorn main:app")
    print("  2. Restart the scheduler (it will populate new columns)")
    print("=" * 60)


if __name__ == "__main__":
    main()
