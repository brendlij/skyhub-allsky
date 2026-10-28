from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class ArchivedFile(Base):
    """A file that has been copied to the archive and read back intact.

    The row is the only thing that makes a local file deletable. A copy that was
    never verified has no row, so no cleanup can ever remove the last good copy.
    """

    __tablename__ = "archived_files"

    # Path relative to the data directory, e.g. "originals/pi5-hqcam/2026-10-03/night/x.jpg".
    rel_path: Mapped[str] = mapped_column(String(500), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
