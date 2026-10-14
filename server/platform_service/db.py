"""Database lifecycle and additive migrations. Entities live in models.py."""
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
# Re-export for integrations using the previous module path.
from .models import (Base, User, LoginSession, Node, ApiKey, Setting, ModelDefault,
                     Task, Attempt, Blob, Asset, MediaFile, now, uid)


class Database:
    def __init__(self, url):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            columns = await conn.run_sync(
                lambda sync: {c["name"] for c in inspect(sync).get_columns("api_keys")}
            )
            if "user_id" not in columns:
                await conn.execute(
                    text(
                        "ALTER TABLE api_keys ADD COLUMN user_id VARCHAR(36) REFERENCES users(id)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_api_keys_user_id ON api_keys(user_id)"
                    )
                )
            task_columns = await conn.run_sync(
                lambda sync: {c["name"] for c in inspect(sync).get_columns("media_tasks")}
            )
            if "raw_request" not in task_columns:
                await conn.execute(text("ALTER TABLE media_tasks ADD COLUMN raw_request JSON"))
                await conn.execute(text("UPDATE media_tasks SET raw_request = request_body"))
            setting_columns = await conn.run_sync(
                lambda sync: {
                    c["name"] for c in inspect(sync).get_columns("system_settings")
                }
            )
            if "idle_release_seconds" not in setting_columns:
                await conn.execute(
                    text(
                        "ALTER TABLE system_settings ADD COLUMN idle_release_seconds INTEGER DEFAULT 3600"
                    )
                )
        async with self.sessions() as db:
            if await db.get(Setting, 1) is None:
                db.add(Setting(id=1))
                await db.commit()
