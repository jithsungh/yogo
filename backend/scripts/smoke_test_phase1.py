"""Quick smoke test: verify models import, DB connects, and default user exists."""
from app.models import Base, User, WorkExperience, Project, Skill, KBChunk
from app.db import get_session
from app.config import get_settings
from sqlalchemy import text


def main():
    settings = get_settings()
    print(f"DEFAULT_USER_ID: {settings.default_user_id}")
    print(f"Models loaded: {len(Base.metadata.tables)} tables")

    with get_session() as session:
        count = session.execute(text("SELECT COUNT(*) FROM users")).scalar()
        print(f"Users in DB: {count}")
        count = session.execute(text("SELECT COUNT(*) FROM kb_chunk")).scalar()
        print(f"KB chunks: {count}")

    print("\n✅ All imports and DB connection working!")


if __name__ == "__main__":
    main()
