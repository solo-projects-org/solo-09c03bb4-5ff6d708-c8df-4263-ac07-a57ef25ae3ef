"""SQLAlchemy 模型与数据库会话。"""

from __future__ import annotations

import os
from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DB_PATH = os.environ.get("FILM_DB_PATH", "/data/film_archive.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


class Roll(Base):
    __tablename__ = "rolls"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    first_frame = Column(Integer, nullable=False)
    last_frame = Column(Integer, nullable=False)

    scans = relationship(
        "Scan", back_populates="roll", cascade="all, delete-orphan"
    )
    locks = relationship(
        "Lock", back_populates="roll", cascade="all, delete-orphan"
    )
    manifest = relationship(
        "Manifest",
        back_populates="roll",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Scan(Base):
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True)
    roll_id = Column(Integer, ForeignKey("rolls.id"), nullable=False, index=True)
    filename = Column(String(512), nullable=False)
    offset = Column(Integer, nullable=False)

    roll = relationship("Roll", back_populates="scans")
    segments = relationship(
        "UsableSegment",
        back_populates="scan",
        cascade="all, delete-orphan",
    )


class UsableSegment(Base):
    __tablename__ = "usable_segments"

    id = Column(Integer, primary_key=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False, index=True)
    start_frame = Column(Integer, nullable=False)
    end_frame = Column(Integer, nullable=False)

    scan = relationship("Scan", back_populates="segments")


class Lock(Base):
    """已修复的同源帧段：重编时该区间必须保留指定文件与档内帧号。"""

    __tablename__ = "locks"

    id = Column(Integer, primary_key=True)
    roll_id = Column(Integer, ForeignKey("rolls.id"), nullable=False, index=True)
    start_frame = Column(Integer, nullable=False)
    end_frame = Column(Integer, nullable=False)
    filename = Column(String(512), nullable=False)

    roll = relationship("Roll", back_populates="locks")


class Manifest(Base):
    """最近一次成功编排的清单（整卷一份）。"""

    __tablename__ = "manifests"

    id = Column(Integer, primary_key=True)
    roll_id = Column(Integer, ForeignKey("rolls.id"), nullable=False, unique=True)
    switch_count = Column(Integer, nullable=False)
    runs_json = Column(Text, nullable=False)
    switches_json = Column(Text, nullable=False)

    roll = relationship("Roll", back_populates="manifest")


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    Base.metadata.create_all(engine)
