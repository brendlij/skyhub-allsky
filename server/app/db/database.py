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

    migrate_derived_products(inspector)
    migrate_processing_sessions(inspector)

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


def add_missing_columns(table: str, inspector, wanted: dict[str, str]) -> None:
    """Add columns a newer model expects to a table that predates them.

    SQLite can add a column but not change or drop one, which is exactly the
    subset this project needs: every schema change so far has been additive, and
    keeping it that way is what lets an install upgrade without a migration tool.
    """
    if not inspector.has_table(table):
        return

    existing = {column["name"] for column in inspector.get_columns(table)}
    statements = [
        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
        for name, definition in wanted.items()
        if name not in existing
    ]

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def migrate_derived_products(inspector) -> None:
    """Products gained a session link, a category, variants and a version."""
    add_missing_columns(
        "derived_products",
        inspector,
        {
            "session_key": "VARCHAR(200)",
            "category": "VARCHAR(50) NOT NULL DEFAULT 'analysis'",
            "preview_path": "VARCHAR(500)",
            "web_path": "VARCHAR(500)",
            "version": "INTEGER NOT NULL DEFAULT 1",
        },
    )


def migrate_processing_sessions(inspector) -> None:
    """Sessions gained a kind, a label and per-processor progress."""
    add_missing_columns(
        "processing_sessions",
        inspector,
        {
            "session_kind": "VARCHAR(30) NOT NULL DEFAULT 'solar'",
            "label": "VARCHAR(120)",
            "progress": "JSON",
        },
    )


def get_db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from app.models import admin_account, admin_session, capture_storage_settings, derived_product, node, node_camera_settings, node_capture_state, node_device_settings, node_environment, node_heater_state, node_overlay_settings, overlay_preset, processing_session, processing_settings, retention_policy, site_settings, trusted_device  # noqa: F401
