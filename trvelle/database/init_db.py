# create_db.py

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Import all your models from the models.py file
from .models import Base, User, ChatSession, Message, ToolExecution, ResearcherAgent

def create_tables():
    """
    Connects to the PostgreSQL database and creates all tables defined in Base.metadata.
    """
    # --- Database Connection String ---


    DATABASE_URL = os.getenv("DB_URI")

    engine = create_engine(DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1))
    # Serialize versioned, additive migrations across API, worker and MCP startup.
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(710742901)"))
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"))
        Base.metadata.create_all(connection)
        if not connection.execute(text("SELECT 1 FROM schema_migrations WHERE version=1")).scalar():
            connection.execute(text("ALTER TABLE messages ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT false"))
            connection.execute(text("ALTER TABLE researcher_agents ADD COLUMN IF NOT EXISTS run_id UUID"))
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES(1)"))
    engine.dispose()

def delete_tables():
    """
    Deletes all tables defined in Base.metadata.
    Use with caution as this will remove all data in the tables.
    """
    DATABASE_URL = os.getenv("DB_URI")
    engine = create_engine(DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1))
    
    # Drop all tables
    Base.metadata.drop_all(engine)
    print("All tables have been deleted.")

if __name__ == "__main__":
    # delete_tables()
    create_tables()
