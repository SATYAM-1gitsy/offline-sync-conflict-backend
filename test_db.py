
from sqlalchemy import text
from database import engine

with engine.connect() as connection:
    result = connection.execute(text("SELECT version()"))
    print(result.scalar())

print("PostgreSQL connection successful!")