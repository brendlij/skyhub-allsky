from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()

settings.data_dir.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite:///{settings.database_path.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def create_db_tables():
    Base.metadata.create_all(bind=engine)
    ensure_lightweight_migrations()


def ensure_lightweight_migrations():
    inspector = inspect(engine)

    if not inspector.has_table("node_camera_settings"):
        return

    columns = {
        column["name"]
        for column in inspector.get_columns("node_camera_settings")
    }

    migrations = []

    if "capture_enabled" not in columns:
        migrations.append(
            "ALTER TABLE node_camera_settings "
            "ADD COLUMN capture_enabled BOOLEAN NOT NULL DEFAULT 0"
        )

    if "current_sequence_id" not in columns:
        migrations.append(
            "ALTER TABLE node_camera_settings "
            "ADD COLUMN current_sequence_id VARCHAR(100)"
        )

    if "day_interval_seconds" not in columns:
        migrations.append(
            "ALTER TABLE node_camera_settings "
            "ADD COLUMN day_interval_seconds INTEGER"
        )

    if "night_interval_seconds" not in columns:
        migrations.append(
            "ALTER TABLE node_camera_settings "
            "ADD COLUMN night_interval_seconds INTEGER"
        )

    added_columns = {
        "day_max_exposure_ms": "INTEGER DEFAULT 1000",
        "day_max_gain": "FLOAT DEFAULT 8.0",
        "night_max_exposure_ms": "INTEGER DEFAULT 30000",
        "night_max_gain": "FLOAT DEFAULT 16.0",
        "full_resolution": "BOOLEAN NOT NULL DEFAULT 1",
        "day_auto_white_balance": "BOOLEAN NOT NULL DEFAULT 1",
        "day_wb_red": "FLOAT NOT NULL DEFAULT 1.0",
        "day_wb_blue": "FLOAT NOT NULL DEFAULT 1.0",
        "day_saturation": "FLOAT NOT NULL DEFAULT 1.0",
        "day_hue": "FLOAT NOT NULL DEFAULT 0.0",
        "night_auto_white_balance": "BOOLEAN NOT NULL DEFAULT 0",
        "night_wb_red": "FLOAT NOT NULL DEFAULT 2.2",
        "night_wb_blue": "FLOAT NOT NULL DEFAULT 1.8",
        "night_saturation": "FLOAT NOT NULL DEFAULT 1.0",
        "night_hue": "FLOAT NOT NULL DEFAULT 0.0",
    }

    for column_name, column_type in added_columns.items():
        if column_name not in columns:
            migrations.append(
                f"ALTER TABLE node_camera_settings ADD COLUMN {column_name} {column_type}"
            )

    if not migrations:
        return

    with engine.begin() as connection:
        for migration in migrations:
            connection.execute(text(migration))


def get_db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from app.models import capture_storage_settings, node, node_camera_settings, node_capture_state, node_device_settings, node_environment, node_heater_state, node_overlay_settings  # noqa: F401
