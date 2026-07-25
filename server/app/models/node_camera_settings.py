from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class NodeCameraSettings(Base):
    __tablename__ = "node_camera_settings"

    node_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    interval_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    day_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    night_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Only the node knows its sensor's native size, so full resolution is a flag
    # rather than a number: when set, width/height are not sent and the driver
    # captures at the sensor's full readout.
    full_resolution: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 4:3 matches the Pi sensors, so libcamera has no reason to centre-crop the sky.
    width: Mapped[int] = mapped_column(Integer, default=2028, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=1520, nullable=False)
    format: Mapped[str] = mapped_column(String(20), default="jpg", nullable=False)

    day_auto_exposure: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    day_exposure_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_auto_gain: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    day_gain: Mapped[float | None] = mapped_column(Float, nullable=True)

    night_auto_exposure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    night_exposure_ms: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    night_auto_gain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    night_gain: Mapped[float] = mapped_column(Float, default=8, nullable=False)

    # Colour. wb_red/wb_blue are libcamera ColourGains, green being the implicit
    # reference at 1.0. Auto white balance is left on for day and off for night:
    # grey-world has no valid assumption under a night sky and swings between
    # green and magenta frame to frame.
    day_auto_white_balance: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    day_wb_red: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    day_wb_blue: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    day_saturation: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    day_hue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    night_auto_white_balance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    night_wb_red: Mapped[float] = mapped_column(Float, default=2.2, nullable=False)
    night_wb_blue: Mapped[float] = mapped_column(Float, default=1.8, nullable=False)
    night_saturation: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    night_hue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    capture_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_sequence_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
