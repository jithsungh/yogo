"""Run once: python -m scripts.create_default_user
Prints a UUID - paste it into .env as DEFAULT_USER_ID.
"""
import uuid

from sqlalchemy import text

from app.db import get_session
from app.config import get_settings


def main():
    settings = get_settings()
    with get_session() as session:
        user_id = uuid.uuid4()
        session.execute(
            text("INSERT INTO users (id, email, password_hash, full_name) VALUES (:id, :email, 'not-used-yet', :name)"),
            {"id": user_id, "email": settings.app_secret_key[:8] + "@local", "name": "Me"},
        )
        session.commit()
    print(f"Created default user: {user_id}")
    print("Add this to .env as DEFAULT_USER_ID")


if __name__ == "__main__":
    main()
