from sqlalchemy import Text
from sqlalchemy.orm import mapped_column, Mapped

from app.db.base import Base

class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
