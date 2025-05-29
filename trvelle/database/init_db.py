# create_db.py

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import all your models from the models.py file
from .models import Base, User, ChatSession, Message, ToolCall, ToolResponse, FinalItinerary, ResearcherAgent

def create_tables():
    """
    Connects to the PostgreSQL database and creates all tables defined in Base.metadata.
    """
    # --- Database Connection String ---


    DATABASE_URL = os.getenv("DB_URI")

    print(f"Attempting to connect to: {DATABASE_URL.split('@')[1]}") # Print without sensitive password

    try:
        # Create an SQLAlchemy engine
        engine = create_engine(DATABASE_URL)

        # Create all tables defined in your Base
        # This will check the database and create tables if they don't exist.
        # It will NOT alter existing tables. For migrations, use Alembic.
        Base.metadata.create_all(engine)

        # print(f"Successfully created tables in database '{DB_NAME}' on '{DB_HOST}'.")

    except Exception as e:
        print(f"Error creating tables: {e}")
        print("Please ensure PostgreSQL is running and the database/user exist and are accessible.")
        print("Also check your DATABASE_URL connection string.")

if __name__ == "__main__":
    create_tables()