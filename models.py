from datetime import datetime
from sqlalchemy import ForeignKey, String, Float, Integer, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from database import Base

STATUS_FLOW = ["Belum ditangani", "Ditangani", "Selesai"]


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="admin")


class Point(Base):
    __tablename__ = "points"
    code: Mapped[str] = mapped_column(String(4), primary_key=True)   # T1..T5
    name: Mapped[str] = mapped_column(String(40))                    # "Titik 1"
    zone: Mapped[str] = mapped_column(String(40))                    # "Hulu"
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    dms: Mapped[str] = mapped_column(String(60))
    density: Mapped[str] = mapped_column(String(10))                 # Tinggi/Sedang/Rendah
    prev_total: Mapped[int] = mapped_column(Integer, default=0)      # total September (untuk "vs Sep")


class PointCategoryStat(Base):
    """Jumlah deteksi per titik per kategori (periode Oktober 2026)."""
    __tablename__ = "point_category_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    point_code: Mapped[str] = mapped_column(ForeignKey("points.code"))
    category: Mapped[str] = mapped_column(String(40))
    count: Mapped[int] = mapped_column(Integer, default=0)


class MonthlyStat(Base):
    __tablename__ = "monthly_stats"
    id: Mapped[int] = mapped_column(primary_key=True)
    order: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(10))
    plastic: Mapped[int] = mapped_column(Integer)
    non_plastic: Mapped[int] = mapped_column(Integer)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    point_code: Mapped[str] = mapped_column(ForeignKey("points.code"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    category: Mapped[str] = mapped_column(String(40))
    count: Mapped[int] = mapped_column(Integer)
    density: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[float] = mapped_column(Float)                 # persen
    status: Mapped[str] = mapped_column(String(20), default=STATUS_FLOW[0])
    snapshot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
