"""
create_tables.py
================
Run this ONCE to create all database tables defined in models.py.

Usage:
    cd backend
    python create_tables.py
"""

from database import engine, DATABASE_URL
from models import Base

def main():
    print(f"Connecting to: {DATABASE_URL}")
    Base.metadata.create_all(bind=engine)
    print("Tables created successfully:")
    for table_name in Base.metadata.tables:
        print(f"  - {table_name}")

if __name__ == "__main__":
    main()
