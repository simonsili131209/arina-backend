"""ARINA API - FastAPI. Jalankan: uvicorn main:app --reload --port 8000"""
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
import time
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

import config
from auth import create_token, hash_password, require_admin, require_user, verify_password
from categories import CATEGORIES, CATEGORY_NAMES, PLASTIC_NAMES
from database import Base, SessionLocal, engine as db_engine, get_db
from models import Event, MonthlyStat, Point, PointCategoryStat, PointReport, STATUS_FLOW, User
from seed import STAFF_NICKNAMES, seed_if_empty
from video import engine as video_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(db_engine)
    with db_engine.begin() as conn:
        try:
            conn.exec_driver_sql("ALTER TABLE point_reports ADD COLUMN photo_data TEXT")
        except Exception:
            pass
    with SessionLocal() as db:
        seed_if_empty(db)
    yield
    video_engine.stop()


app = FastAPI(title="ARINA API - Pemantauan Sungai", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


# ----------------------------------------------------------------- helpers
def point_label(p: Point) -> str:
    return f"{p.code} · {p.zone.lower()}" if p.code in ("T1", "T5") else p.code


def default_nickname(email: str, name: str) -> str:
    clean_email = (email or "").strip().lower()
    if clean_email in STAFF_NICKNAMES:
        return STAFF_NICKNAMES[clean_email]
    clean_name = (name or "").strip()
    if not clean_name or clean_name == "Tamu":
        return ""
    before_dot = clean_name.split("·")[0].strip()
    if before_dot.lower().startswith("petugas"):
        return before_dot
    return before_dot.split()[0]


def event_out(e: Event, p: Point) -> dict:
    snap_url = None
    if e.snapshot:
        snap_url = e.snapshot if e.snapshot.startswith("data:") else f"/static/snapshots/{e.snapshot}"
    return {
        "id": e.id, "point_code": e.point_code, "point_name": p.name, "zone": p.zone,
        "lat": p.lat, "lng": p.lng, "dms": p.dms,
        "occurred_at": e.occurred_at.isoformat(), "category": e.category, "count": e.count,
        "density": e.density, "confidence": e.confidence, "status": e.status,
        "snapshot_url": snap_url,
        "note": e.note,
    }


def report_out(r: PointReport) -> dict:
    return {
        "id": r.id,
        "reporter_id": r.reporter_id,
        "reporter_name": r.reporter_name,
        "location_name": r.location_name,
        "lat": r.lat,
        "lng": r.lng,
        "density": r.density,
        "description": r.description,
        "photo_data": r.photo_data,
        "status": r.status,
        "created_at": r.created_at.isoformat(),
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "reviewed_by": r.reviewed_by,
        "review_note": r.review_note,
    }


def category_matrix(db: Session) -> dict:
    m = {p.code: {n: 0 for n in CATEGORY_NAMES} for p in db.query(Point).all()}
    for r in db.query(PointCategoryStat).all():
        m[r.point_code][r.category] = r.count
    return m


# ----------------------------------------------------------------- verifikasi email & token pendaftaran
def _verify_secret() -> bytes:
    sec = getattr(config, "SECRET_KEY", None) or os.getenv("SECRET_KEY") or "arina-email-verify-2026"
    return sec.encode("utf-8")


def sign_verification_payload(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(_verify_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def read_verification_payload(token: str) -> dict:
    try:
        b64, sig = token.strip().split(".", 1)
    except ValueError:
        raise HTTPException(400, "Tautan atau token verifikasi tidak valid.")
    expected = hmac.new(_verify_secret(), b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(400, "Tanda tangan token verifikasi tidak sah.")
    pad = "=" * (-len(b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(b64 + pad).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Data verifikasi rusak.")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(400, "Tautan & kode verifikasi sudah kedaluwarsa (lebih dari 15 menit). Silakan kirim ulang.")
    return payload


def send_arina_verification_email(to_email: str, nickname: str, full_name: str, otp_code: str, verify_link: str) -> bool:
    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip().replace(" ", "")
    smtp_host = (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT") or "465")

    if not smtp_user or not smtp_pass:
        return False

    spaced_otp = f"{otp_code[:3]} {otp_code[3:]}"
    html_body = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background-color:#050816;font-family:'Segoe UI',Roboto,Arial,sans-serif;color:#e2e8f0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#050816;padding:32px 16px;">
        <tr>
          <td align="center">
            <table width="100%" style="max-width:520px;background:linear-gradient(180deg,#0b132b 0%,#0f172a 100%);border:1px solid #1e3a5f;border-radius:20px;overflow:hidden;">
              <tr>
                <td style="padding:28px 32px 16px;text-align:center;border-bottom:1px solid rgba(56,189,248,0.18);">
                  <div style="display:inline-block;padding:6px 14px;border-radius:999px;background:rgba(42,160,255,0.15);border:1px solid rgba(56,189,248,0.35);color:#7dd3fc;font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;">
                    ARINA · Pemantauan Sungai
                  </div>
                  <h1 style="margin:14px 0 4px;font-size:24px;color:#ffffff;">Verifikasi Akun Baru</h1>
                  <p style="margin:0;font-size:13px;color:#94a3b8;">Sungai Karangantu · Sistem Pemantauan Lingkungan</p>
                </td>
              </tr>
              <tr>
                <td style="padding:28px 32px;">
                  <p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#f1f5f9;">
                    Halo <strong style="color:#38bdf8;">{nickname}</strong> ({full_name}),
                  </p>
                  <p style="margin:0 0 22px;font-size:14px;line-height:1.65;color:#cbd5e1;">
                    Terima kasih sudah mendaftar di <strong>ARINA</strong>. Klik tombol di bawah ini untuk memverifikasi email dan langsung masuk ke platform pemantauan sungai:
                  </p>

                  <div style="text-align:center;margin:26px 0;">
                    <a href="{verify_link}" style="display:inline-block;padding:14px 28px;background:linear-gradient(135deg,#2AA0FF 0%,#4f46e5 100%);color:#ffffff;text-decoration:none;font-weight:700;font-size:14px;border-radius:12px;box-shadow:0 8px 20px rgba(42,160,255,0.35);">
                      Verifikasi &amp; Masuk ke ARINA
                    </a>
                  </div>

                  <div style="margin:24px 0;padding:18px;background:rgba(15,23,42,0.85);border:1px dashed rgba(56,189,248,0.4);border-radius:14px;text-align:center;">
                    <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px;">
                      Atau masukkan kode verifikasi 6 digit
                    </div>
                    <div style="font-size:28px;font-weight:800;letter-spacing:6px;color:#38bdf8;font-family:monospace;">
                      {spaced_otp}
                    </div>
                  </div>

                  <p style="margin:0;font-size:12px;line-height:1.6;color:#64748b;">
                    Tautan dan kode ini berlaku selama <strong>15 menit</strong>. Jika kamu tidak merasa membuat akun ARINA, abaikan email ini.
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{otp_code}] Verifikasi Akun ARINA Pemantauan Sungai"
    msg["From"] = f"ARINA Pemantauan Sungai <{smtp_user}>"
    msg["To"] = to_email
    msg.attach(MIMEText(f"Halo {nickname}, klik link ini untuk verifikasi akun ARINA: {verify_link} atau masukkan kode: {otp_code}", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12, context=ctx) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())
    return True


# ----------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: str
    password: str


class RegisterIn(BaseModel):
    name: str
    email: str
    password: str
    nickname: Optional[str] = None
    frontend_url: Optional[str] = None


class VerifyRegisterIn(BaseModel):
    verify_token: str
    otp_code: Optional[str] = None


class GoogleLoginIn(BaseModel):
    email: str
    name: Optional[str] = None


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email atau password salah.")
    info = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "nickname": default_nickname(user.email, user.name),
        "role": user.role,
    }
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/register-request")
def register_request(body: RegisterIn, db: Session = Depends(get_db)):
    clean_email = body.email.strip().lower()
    clean_name = body.name.strip()
    clean_nick = (body.nickname or "").strip() or default_nickname(clean_email, clean_name)

    if not clean_name or not clean_email or "@" not in clean_email or len(body.password) < 6:
        raise HTTPException(400, "Nama panggilan, nama lengkap, email valid, dan sandi minimal 6 karakter wajib diisi.")
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(400, "Email sudah terdaftar. Silakan masuk melalui tab Masuk Akun.")

    otp_code = f"{secrets.randbelow(900000) + 100000}"
    payload = {
        "email": clean_email,
        "name": clean_name,
        "nickname": clean_nick,
        "password_hash": hash_password(body.password),
        "otp": otp_code,
        "exp": int(time.time()) + 900,
    }
    v_token = sign_verification_payload(payload)

    base_front = (
        (body.frontend_url or "").strip().rstrip("/")
        or (os.getenv("FRONTEND_URL") or "").strip().rstrip("/")
        or "https://arina-monitoring.vercel.app"
    )
    verify_link = f"{base_front}/?verify_token={urllib.parse.quote(v_token)}"

    try:
        sent = send_arina_verification_email(clean_email, clean_nick, clean_name, otp_code, verify_link)
    except Exception as ex:
        raise HTTPException(500, f"Gagal mengirim email verifikasi: {type(ex).__name__}. Periksa konfigurasi SMTP.")

    return {
        "status": "verification_sent",
        "email": clean_email,
        "nickname": clean_nick,
        "verify_token": v_token,
        "email_dispatched": sent,
        "dev_hint": None if sent else {"otp_code": otp_code, "verify_link": verify_link},
    }


@app.post("/api/auth/verify-registration")
def verify_registration(body: VerifyRegisterIn, db: Session = Depends(get_db)):
    payload = read_verification_payload(body.verify_token)

    if body.otp_code is not None:
        cleaned_otp = re.sub(r"\s+", "", body.otp_code.strip())
        if cleaned_otp != payload.get("otp"):
            raise HTTPException(400, "Kode verifikasi 6 digit tidak cocok. Periksa kembali email kamu.")

    clean_email = payload["email"]
    clean_name = payload["name"]
    clean_nick = payload.get("nickname") or default_nickname(clean_email, clean_name)

    user = db.query(User).filter(User.email == clean_email).first()
    if not user:
        user = User(
            email=clean_email,
            name=clean_name,
            password_hash=payload["password_hash"],
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    info = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "nickname": clean_nick,
        "role": user.role,
    }
    return {"token": create_token(info), "user": info, "nickname": clean_nick}


@app.post("/api/auth/register")
def register(body: RegisterIn, db: Session = Depends(get_db)):
    clean_email = body.email.strip().lower()
    clean_name = body.name.strip()
    clean_nick = (body.nickname or "").strip() or default_nickname(clean_email, clean_name)
    if not clean_name or not clean_email or len(body.password) < 6:
        raise HTTPException(400, "Nama, email, dan sandi minimal 6 karakter wajib diisi.")
    if db.query(User).filter(User.email == clean_email).first():
        raise HTTPException(400, "Email sudah terdaftar. Silakan masuk melalui tab Login.")
    user = User(
        email=clean_email,
        name=clean_name,
        password_hash=hash_password(body.password),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    info = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "nickname": clean_nick,
        "role": user.role,
    }
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/google")
def login_google(body: GoogleLoginIn, db: Session = Depends(get_db)):
    clean_email = body.email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(400, "Masukkan alamat email Google yang valid.")
    user = db.query(User).filter(User.email == clean_email).first()
    if not user:
        display_name = (body.name or clean_email.split("@")[0]).strip()
        user = User(
            email=clean_email,
            name=display_name,
            password_hash=hash_password(os.urandom(16).hex()),
            role="user",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    info = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "nickname": default_nickname(user.email, user.name),
        "role": user.role,
    }
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/guest")
def guest():
    guest_id = f"guest_{os.urandom(4).hex()}"
    info = {"sub": guest_id, "email": "", "name": "Tamu", "nickname": "", "role": "guest"}
    return {"token": create_token(info), "user": info}


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    out = {k: user[k] for k in ("sub", "email", "name", "role") if k in user}
    out["nickname"] = user.get("nickname") or default_nickname(out.get("email", ""), out.get("name", ""))
    return {"user": out}


# ----------------------------------------------------------------- peta & titik
@app.get("/api/points")
def points(db: Session = Depends(get_db), _: dict = Depends(require_user)):
    matrix = category_matrix(db)
    pts = db.query(Point).order_by(Point.code).all()
    out = []
    for p in pts:
        cats = sorted(matrix[p.code].items(), key=lambda kv: kv[1], reverse=True)
        out.append({
            "code": p.code, "name": p.name, "zone": p.zone, "label": point_label(p),
            "lat": p.lat, "lng": p.lng, "dms": p.dms, "density": p.density,
            "total": sum(matrix[p.code].values()),
            "dominant": [{"category": c, "count": n} for c, n in cats[:3] if n > 0],
        })
    week_ago = datetime.now() - timedelta(days=7)
    events = db.query(Event).all()
    return {
        "center": [-6.0355, 106.1631], "zoom": 15, "points": out,
        "summary": {
            "points": len(pts),
            "events_this_week": sum(1 for e in events if e.occurred_at >= week_ago),
            "pending": sum(1 for e in events if e.status == STATUS_FLOW[0]),
        },
    }


# ----------------------------------------------------------------- penitikan koordinat masyarakat, validasi & rotasi foto DB
PHOTO_MAX_AGE_DAYS = 7
PHOTO_STORAGE_LIMIT_BYTES = 40 * 1024 * 1024  # 40 MB ambang aman foto di database


class PointReportIn(BaseModel):
    location_name: str
    lat: float
    lng: float
    density: str = "Sedang"
    description: Optional[str] = None
    photo_data: Optional[str] = None


class PointReportReviewIn(BaseModel):
    status: str
    density: Optional[str] = None
    review_note: Optional[str] = None
    location_name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    description: Optional[str] = None
    reporter_id: Optional[str] = None
    reporter_name: Optional[str] = None
    created_at: Optional[str] = None
    photo_data: Optional[str] = None


class CleanupPhotosIn(BaseModel):
    mode: str = "older_than_7d"  # "older_than_7d" | "all_reviewed"


def cleanup_old_report_photos(
    db: Session,
    max_age_days: int = PHOTO_MAX_AGE_DAYS,
    max_total_bytes: int = PHOTO_STORAGE_LIMIT_BYTES,
    force_reviewed: bool = False,
) -> dict:
    """Hapus foto laporan > 7 hari (minggu lalu) atau saat database foto mulai penuh.
    Titik koordinat & riwayat laporan tetap tersimpan di database."""
    cutoff = datetime.now() - timedelta(days=max_age_days)
    rows = (
        db.query(PointReport)
        .filter(PointReport.photo_data.isnot(None))
        .order_by(PointReport.created_at.asc())
        .all()
    )

    deleted_count = 0
    freed_bytes = 0

    for r in rows:
        if not r.photo_data:
            continue
        is_expired = r.created_at is not None and r.created_at < cutoff
        is_reviewed_purge = force_reviewed and r.status in ("Valid", "Ditolak")
        if is_expired or is_reviewed_purge:
            freed_bytes += len(r.photo_data)
            r.photo_data = None
            deleted_count += 1

    remaining = [r for r in rows if r.photo_data]
    total_bytes = sum(len(r.photo_data) for r in remaining)

    # Jika total foto di database masih melewati ambang batas, hapus dari yang paling lama
    if total_bytes > max_total_bytes:
        remaining.sort(key=lambda r: (0 if r.status != "Menunggu Peninjauan" else 1, r.created_at or datetime.min))
        for r in remaining:
            if total_bytes <= max_total_bytes:
                break
            if r.photo_data:
                sz = len(r.photo_data)
                r.photo_data = None
                total_bytes -= sz
                freed_bytes += sz
                deleted_count += 1

    if deleted_count > 0:
        db.commit()

    active_photos = [r for r in rows if r.photo_data]
    active_bytes = sum(len(r.photo_data) for r in active_photos)
    return {
        "deleted_count": deleted_count,
        "freed_bytes": freed_bytes,
        "photo_count": len(active_photos),
        "used_bytes": active_bytes,
        "limit_bytes": max_total_bytes,
        "retention_days": max_age_days,
    }


def compute_photo_storage_stats(db: Session) -> dict:
    cutoff = datetime.now() - timedelta(days=PHOTO_MAX_AGE_DAYS)
    rows = db.query(PointReport).filter(PointReport.photo_data.isnot(None)).all()
    used_bytes = sum(len(r.photo_data) for r in rows if r.photo_data)
    expired_count = sum(1 for r in rows if r.photo_data and r.created_at and r.created_at < cutoff)
    reviewed_with_photo = sum(1 for r in rows if r.photo_data and r.status in ("Valid", "Ditolak"))
    return {
        "photo_count": len(rows),
        "used_bytes": used_bytes,
        "used_mb": round(used_bytes / (1024 * 1024), 2),
        "limit_mb": round(PHOTO_STORAGE_LIMIT_BYTES / (1024 * 1024), 1),
        "usage_pct": min(100, round((used_bytes / PHOTO_STORAGE_LIMIT_BYTES) * 100, 1)) if PHOTO_STORAGE_LIMIT_BYTES else 0,
        "expired_count": expired_count,
        "reviewed_with_photo": reviewed_with_photo,
        "retention_days": PHOTO_MAX_AGE_DAYS,
    }


@app.get("/api/point-reports")
def list_point_reports(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    cleanup_old_report_photos(db)
    rows = db.query(PointReport).order_by(PointReport.created_at.desc()).all()
    if user.get("role") == "admin":
        visible = rows
    else:
        uid = str(user.get("sub"))
        visible = [r for r in rows if r.status == "Valid" or r.reporter_id == uid]
    return {"reports": [report_out(r) for r in visible]}


@app.get("/api/point-reports/storage")
def point_report_storage(db: Session = Depends(get_db), _: dict = Depends(require_admin)):
    cleanup_old_report_photos(db)
    return compute_photo_storage_stats(db)


@app.post("/api/point-reports/cleanup-photos")
def manual_cleanup_photos(
    body: CleanupPhotosIn,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    force_reviewed = body.mode == "all_reviewed"
    res = cleanup_old_report_photos(db, force_reviewed=force_reviewed)
    stats = compute_photo_storage_stats(db)
    return {
        "deleted_count": res["deleted_count"],
        "freed_mb": round(res["freed_bytes"] / (1024 * 1024), 2),
        **stats,
    }


@app.post("/api/point-reports")
def create_point_report(body: PointReportIn, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    loc = body.location_name.strip()
    if not loc:
        raise HTTPException(400, "Nama lokasi atau patokan sekitar sungai wajib diisi.")
    if not (-90 <= body.lat <= 90 and -180 <= body.lng <= 180):
        raise HTTPException(400, "Koordinat latitude/longitude tidak valid.")
    if body.photo_data and len(body.photo_data) > 2_000_000:
        raise HTTPException(400, "Ukuran foto terlalu besar. Silakan pilih foto lain.")
    dens = body.density if body.density in ("Tinggi", "Sedang", "Rendah") else "Sedang"

    rep = PointReport(
        reporter_id=str(user.get("sub")),
        reporter_name=user.get("name") or "Warga",
        location_name=loc,
        lat=round(body.lat, 6),
        lng=round(body.lng, 6),
        density=dens,
        description=(body.description or "").strip() or None,
        photo_data=body.photo_data or None,
        status="Menunggu Peninjauan",
        created_at=datetime.now(),
    )
    db.add(rep)
    db.commit()
    db.refresh(rep)

    # Jalankan pembersihan otomatis foto > 7 hari atau jika kapasitas database mulai penuh
    cleanup_old_report_photos(db)
    db.refresh(rep)
    return report_out(rep)


@app.patch("/api/point-reports/{report_id}/review")
def review_point_report(report_id: int, body: PointReportReviewIn, db: Session = Depends(get_db),
                        admin: dict = Depends(require_admin)):
    if body.status not in ("Valid", "Ditolak"):
        raise HTTPException(400, "Keputusan peninjauan harus 'Valid' atau 'Ditolak'.")

    rep = db.get(PointReport, report_id)
    if not rep:
        if body.location_name is not None and body.lat is not None and body.lng is not None:
            try:
                created_dt = datetime.fromisoformat(body.created_at) if body.created_at else datetime.now()
            except Exception:
                created_dt = datetime.now()
            rep = PointReport(
                id=report_id,
                reporter_id=body.reporter_id or "warga",
                reporter_name=body.reporter_name or "Warga",
                location_name=body.location_name,
                lat=round(body.lat, 6),
                lng=round(body.lng, 6),
                density=body.density if body.density in ("Tinggi", "Sedang", "Rendah") else "Sedang",
                description=body.description,
                photo_data=body.photo_data,
                status="Menunggu Peninjauan",
                created_at=created_dt,
            )
            db.add(rep)
            db.flush()
        else:
            raise HTTPException(404, "Laporan titik koordinat tidak ditemukan.")

    if body.density in ("Tinggi", "Sedang", "Rendah"):
        rep.density = body.density
    rep.status = body.status
    rep.reviewed_at = datetime.now()
    rep.reviewed_by = admin.get("name") or "Petugas ARINA"
    rep.review_note = (body.review_note or "").strip() or None
    db.commit()
    db.refresh(rep)
    return report_out(rep)


@app.get("/api/categories")
def categories(_: dict = Depends(require_user)):
    return {"categories": CATEGORIES}


# ----------------------------------------------------------------- log kejadian (khusus petugas)
@app.get("/api/events")
def list_events(point: str = "Semua", density: str = "Semua", category: str = "Semua",
                status: str = "Semua", db: Session = Depends(get_db), _: dict = Depends(require_admin)):
    q = db.query(Event)
    if point != "Semua":
        q = q.filter(Event.point_code == point)
    if density != "Semua":
        q = q.filter(Event.density == density)
    if category != "Semua":
        q = q.filter(Event.category == category)
    if status != "Semua":
        q = q.filter(Event.status == status)
    pts = {p.code: p for p in db.query(Point).all()}
    rows = q.order_by(Event.occurred_at.desc()).all()
    per_point = {c: 0 for c in pts}
    for e in db.query(Event).all():
        per_point[e.point_code] += 1
    return {"events": [event_out(e, pts[e.point_code]) for e in rows],
            "total": len(rows), "per_point": per_point, "statuses": STATUS_FLOW}


@app.get("/api/events/{event_id}")
def get_event(event_id: int, db: Session = Depends(get_db), _: dict = Depends(require_admin)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Kejadian tidak ditemukan.")
    return event_out(e, db.get(Point, e.point_code))


class StatusIn(BaseModel):
    status: str


@app.patch("/api/events/{event_id}/status")
def update_status(event_id: int, body: StatusIn, db: Session = Depends(get_db),
                  _: dict = Depends(require_admin)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Kejadian tidak ditemukan.")
    if body.status not in STATUS_FLOW:
        raise HTTPException(400, f"Status harus salah satu dari: {', '.join(STATUS_FLOW)}")
    if STATUS_FLOW.index(body.status) != STATUS_FLOW.index(e.status) + 1:
        raise HTTPException(400, f"Status hanya dapat dinaikkan bertahap: {' → '.join(STATUS_FLOW)}")
    e.status = body.status
    db.commit()
    return event_out(e, db.get(Point, e.point_code))


# ----------------------------------------------------------------- asisten AI ARINA (Fast-Path + Bunglon Gaya Bahasa)
class ChatTurn(BaseModel):
    role: str
    text: str


class AiChatIn(BaseModel):
    message: str
    history: list[ChatTurn] = []
    nickname: Optional[str] = None


SENSITIVE_PATTERN = re.compile(
    r"\b(email|e-mail|sandi|password|pass|kata\s*sandi|login|masuk\s*akun|username|user\s*id|token|jwt|secret|kredensial|hack|bobol|nomor\s*hp|telepon|whatsapp|wa\s*petugas|alamat\s*rumah)\b",
    re.IGNORECASE,
)

GREETING_ONLY_TOKENS = {
    "hai", "halo", "hallo", "hello", "hi", "hey", "hei", "helo", "p", "ping",
    "pagi", "siang", "sore", "malam", "selamat",
    "assalamualaikum", "assalamu'alaikum", "salam",
    "yo", "yoo", "yow", "oy", "oi", "woy", "woi", "cuy", "bro", "sis", "bestie", "min",
    "arina", "juga", "dong", "nih", "ya", "kak", "bang", "mas", "mba", "mbak",
}

CASUAL_CHAT_TOKENS = GREETING_ONLY_TOKENS | {
    "tes", "test", "apa", "kabar", "kabarmu", "kabarnya", "gimana", "gmn", "baik", "sehat",
    "wkwk", "wkwkwk", "hehe", "haha", "mantap", "oke", "ok", "siap",
    "makasih", "terima", "kasih", "thanks", "thx", "gk", "ga", "gak", "jadi",
}

GAUL_HINTS = {
    "yo", "yoo", "yow", "oy", "oi", "woy", "woi", "cuy", "bro", "bestie", "wkwk",
    "wkwkwk", "gmn", "gimana", "nih", "dong", "gw", "gue", "lu", "lo",
    "gokil", "spill", "relate", "parah", "bgt", "banget", "sih", "gk", "ga", "gak",
}

_CACHED_WORKING_MODEL: Optional[str] = None


def fast_smalltalk_reply(msg: str, nickname: Optional[str]) -> Optional[str]:
    cleaned = re.sub(r"[^\w\s']", " ", msg.lower()).strip()
    words = [w for w in cleaned.split() if w]
    if not words or len(words) > 5:
        return None
    if not all(w in CASUAL_CHAT_TOKENS for w in words):
        return None

    nick = (nickname or "").strip()
    is_gaul = any(w in GAUL_HINTS for w in words) or cleaned in ("hai juga", "halo juga", "hallo juga", "p", "tes", "test")

    if any(w in ("makasih", "terima", "kasih", "thanks", "thx") for w in words):
        if is_gaul:
            return secrets.choice([
                "Sama-sama! Kalau ada yang mau diobrolin lagi soal sungai atau sampah, gas langsung tanya aja ya.",
                "Siap, santai aja! Kabarin kapan aja kalau butuh info atau data titik sungai.",
            ])
        return "Sama-sama! Senang bisa membantu. Silakan tanyakan apa saja seputar pemantauan sungai dan pengelolaan sampah."

    if any(w in ("kabar", "kabarmu", "kabarnya", "gimana", "gmn") for w in words):
        if is_gaul:
            return secrets.choice([
                "Aman terkendali! Sistem pantau sungai lagi on fire nih. Gimana, ada yang mau di-spill atau ditanyain?",
                "Kabar baik banget! Lagi standby mantau kondisi sungai nih. Mau bahas apa kita hari ini?",
            ])
        return "Kabar baik! Sistem pemantauan ARINA siap menemani diskusi. Ada topik sampah atau kondisi sungai yang ingin dibahas?"

    if any(w in ("jadi", "wkwk", "wkwkwk", "hehe", "haha", "oke", "ok", "siap", "mantap") for w in words):
        if is_gaul:
            return secrets.choice([
                "Wkwk siap, santai aja! Kalau tiba-tiba kepikiran mau bahas soal sampah atau sungai, langsung panggil aja ya.",
                "Oke sip! Standby terus di sini kok kalau sewaktu-waktu butuh data atau mau diskusi.",
            ])
        return "Baik, siap! Silakan sampaikan kapan saja jika ada pertanyaan atau topik lingkungan yang ingin dibahas."

    call = f" {nick}" if nick else ""
    if is_gaul:
        return secrets.choice([
            f"Hai juga{call}! Gimana nih, mau ngobrolin kondisi Sungai Karangantu, dampak sampah plastik, atau lagi nyari referensi riset?",
            f"Oi{call}! Santai aja di sini, mau bahas soal sampah, limbah, atau kondisi sungai mana nih?",
            f"Hallo{call}! Siap nemenin diskusi nih—mau nanya hal serius soal lingkungan atau ngobrol santai soal sampah sungai, gas aja!",
        ])

    return secrets.choice([
        f"Hai juga{call}! Senang menyapa kamu. Ada hal seputar sampah, limbah, atau kondisi Sungai Karangantu yang ingin kita diskusikan?",
        f"Halo{call}! Saya ARINA, siap membantu. Topik lingkungan atau data titik sungai mana yang ingin kamu bahas?",
    ])


def get_gemini_key() -> str:
    raw = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return raw.strip().strip('"').strip("'").strip()


def discover_available_models(api_key: str, ctx: ssl.SSLContext) -> list[str]:
    found: list[str] = []
    try:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models") or []:
                methods = m.get("supportedGenerationMethods") or []
                name = m.get("name") or ""
                if "generateContent" in methods and "gemini" in name.lower():
                    clean_name = name.replace("models/", "")
                    if clean_name not in found:
                        found.append(clean_name)
    except Exception:
        pass
    found.sort(key=lambda x: (0 if "2.0-flash" in x else 1, 0 if "flash" in x else 2))
    return found[:4]


def build_river_snapshot(db: Session) -> dict:
    pts = db.query(Point).order_by(Point.code).all()
    matrix = category_matrix(db)
    point_summaries = []
    for p in pts:
        cats = sorted(matrix[p.code].items(), key=lambda kv: kv[1], reverse=True)
        top_cats = [f"{c} ({n})" for c, n in cats[:3] if n > 0]
        point_summaries.append({
            "code": p.code,
            "name": p.name,
            "zone": p.zone,
            "density": p.density,
            "total": sum(matrix[p.code].values()),
            "top_cats": top_cats,
        })

    pts_map = {p.code: p for p in pts}
    latest_events = db.query(Event).order_by(Event.occurred_at.desc()).limit(5).all()
    event_list = [
        {
            "point_code": e.point_code,
            "zone": pts_map[e.point_code].zone if e.point_code in pts_map else e.point_code,
            "category": e.category,
            "count": e.count,
            "density": e.density,
            "status": e.status,
            "time": e.occurred_at.strftime("%d %b %Y %H:%M"),
        }
        for e in latest_events
    ]
    return {
        "points": point_summaries,
        "events": event_list,
        "pending_events": db.query(Event).filter(Event.status == STATUS_FLOW[0]).count(),
    }


def call_gemini_llm(
    msg: str,
    history: list[ChatTurn],
    snap: dict,
    api_key: str,
    nickname: Optional[str],
) -> tuple[Optional[str], str]:
    global _CACHED_WORKING_MODEL

    call_name = (nickname or "").strip()
    name_rule = (
        f"- ATURAN PENYEBUTAN NAMA: Nama pengguna adalah '{call_name}', tetapi JANGAN PERNAH menyebut nama atau gelar tersebut saat menjawab pertanyaan, berdiskusi, atau menanggapi obrolan lanjutan karena terasa kaku dan formal. Penyebutan nama HANYA boleh dipakai jika pengguna mengirim sapaan pembuka seperti 'hai', 'oi', 'hallo', atau 'woi'."
        if call_name else
        "- ATURAN PENYEBUTAN NAMA: Jangan menyebut nama atau gelar pengguna di dalam percakapan."
    )

    persona_prompt = (
        "Kamu adalah ARINA, asisten AI berwawasan luas, cerdas, luwes, dan adaptif pada platform pemantauan Sungai Karangantu.\n"
        f"{name_rule}\n"
        "KEMAMPUAN ADAPTASI GAYA BAHASA (BUNGLON KOMUNIKASI):\n"
        "- Amati gaya ketikan pengguna pada pesan terakhir dan riwayat chat!\n"
        "- Jika pengguna memakai bahasa gaul / santai / kekinian (seperti 'gw/lu', 'cuy', 'bro', 'bestie', 'spill', 'gokil', 'parah banget', 'relate', 'wkwk', 'btw', singkatan santai), sesuaikan gaya bicaramu agar ikut santai, akrab, ekspresif, dan kekinian seperti teman ngobrol sepantaran yang asyik (tetap sopan tanpa kata kasar)—dan pastikan penjelasan ilmiah serta datanya tetap tajam dan akurat.\n"
        "- Jika pengguna memakai bahasa Indonesia yang sopan, semi-formal, atau akademis (misalnya menanyakan jurnal penelitian atau analisis teknis), gunakan bahasa yang rapi, jelas, dan profesional.\n"
        "- Tanggap terhadap typo, singkatan, maupun maksud tersirat, dan jawab langsung ke inti tanpa bertele-tele.\n"
        "- Kamu menguasai seluruh topik persampahan, limbah domestik & industri, limbah B3, kimia polimer, mikroplastik, dampak kesehatan & ekologis, pengelolaan sungai di Banten (Ciujung, Cisadane, Cibanten, Cidurian, Karangantu) maupun wilayah lain, serta riset ilmiah.\n"
        "- Jika pengguna meminta sumber data ilmiah atau artikel/jurnal peneliti, jelaskan mekanisme ilmiahnya dan sertakan tautan Markdown [Judul/Sumber](https://...) menuju DOI atau file PDF terbuka.\n"
        "- DILARANG KERAS memberikan, menebak, atau mengarang email, kata sandi, nomor telepon, atau data akun petugas/pengguna. Jika ditanya, jawab singkat: 'Maaf, saya tidak tahu.'\n"
        "- Jika pengguna bertanya di luar topik lingkungan/sampah/sungai/sapaan wajar, arahkan kembali secara hangat sesuai gaya bahasa mereka.\n"
        f"- Data pemantauan Sungai Karangantu terkini (gunakan hanya jika relevan): {json.dumps(snap, ensure_ascii=False)}"
    )

    contents = [
        {
            "role": "user",
            "parts": [{"text": f"[INSTRUKSI SISTEM ARINA]\n{persona_prompt}\n\nMulai percakapan dengan pengguna."}],
        },
        {
            "role": "model",
            "parts": [{"text": "Siap! Saya ARINA, siap berdiskusi secara luwes tanpa menyebut nama pengguna di luar sapaan awal."}],
        },
    ]

    for turn in (history or [])[-8:]:
        role = "model" if turn.role == "assistant" else "user"
        clean_txt = (turn.text or "").strip()
        if clean_txt:
            contents.append({"role": role, "parts": [{"text": clean_txt}]})

    contents.append({"role": "user", "parts": [{"text": msg}]})

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.72,
            "maxOutputTokens": 900,
        },
    }
    encoded_body = json.dumps(payload).encode("utf-8")
    ctx = ssl.create_default_context()

    initial_models = []
    if _CACHED_WORKING_MODEL:
        initial_models.append(_CACHED_WORKING_MODEL)
    initial_models.extend([
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ])

    tried = set()
    last_err = ""

    def try_models(model_list: list[str]) -> tuple[Optional[str], str]:
        global _CACHED_WORKING_MODEL
        nonlocal last_err
        for model_name in model_list:
            if model_name in tried:
                continue
            tried.add(model_name)
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                req = urllib.request.Request(
                    url,
                    data=encoded_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=7, context=ctx) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                    cands = raw.get("candidates") or []
                    if cands:
                        parts = cands[0].get("content", {}).get("parts") or []
                        text = "".join(p.get("text", "") for p in parts).strip()
                        if text:
                            _CACHED_WORKING_MODEL = model_name
                            return text, model_name
            except urllib.error.HTTPError as he:
                err_body = ""
                try:
                    err_body = he.read().decode("utf-8")[:140]
                except Exception:
                    pass
                last_err = f"HTTP {he.code} ({model_name})"
                if he.code in (400, 403) and "API_KEY_INVALID" in err_body:
                    return None, ""
            except Exception as ex:
                last_err = type(ex).__name__
        return None, ""

    reply, ok_model = try_models(initial_models)
    if reply:
        return reply, ok_model

    discovered = discover_available_models(api_key, ctx)
    if discovered:
        reply, ok_model = try_models(discovered)
        if reply:
            return reply, ok_model

    return None, last_err or "unavailable"


def local_arina_reply(msg: str, snap: dict) -> str:
    q = msg.strip().lower()

    if any(k in q for k in ("banten", "ciujung", "cisadane", "cibanten", "cidurian", "selain karangantu")):
        return (
            "Selain **Sungai Karangantu**, beberapa sungai besar di wilayah **Banten** yang sering mengalami tekanan penumpukan sampah dan limbah antara lain:\n\n"
            "1. **Sungai Cisadane (Tangerang Raya):** Menanggung beban sampah domestik padat penduduk dan limbah industri sebelum bermuara ke Laut Jawa.\n"
            "2. **Sungai Ciujung (Serang – Lebak):** Rawan akumulasi sampah rumah tangga dan limbah industri di bagian tengah hingga hilir.\n"
            "3. **Sungai Cibanten (Kota Serang):** Melintasi pusat permukiman dan pasar sehingga rawan penyempitan aliran akibat plastik kemasan.\n"
            "4. **Sungai Cidurian (Tangerang – Serang):** Sering membawa sampah kiriman dari hulu saat debit hujan meningkat."
        )

    if any(k in q for k in ("berbahaya", "berbaya", "bahaya", "racun", "toksik", "mematikan", "paling parah")):
        return (
            "Jika dilihat dari ancaman terhadap kesehatan manusia dan ekosistem sungai, **sampah paling berbahaya** terbagi dalam 3 kelompok utama:\n\n"
            "1. **Limbah B3 & Elektronik (*E-waste*):** Baterai bekas, lampu neon, kaleng pestisida, dan oli melepaskan logam berat (**merkuri, timbal, kadmium**) yang merusak saraf dan mencemari air.\n"
            "2. **Sampah Medis & Infeksius:** Jarum suntik, masker bekas, dan limbah farmasi yang membawa patogen menular.\n"
            "3. **Plastik PVC & Berlapis (*Multilayer*):** Terurai ratusan tahun menjadi **mikroplastik** dan melepaskan gas karsinogenik **dioksin** bila dibakar."
        )

    if any(k in q for k in ("artikel", "jurnal", "peneliti", "penelitian", "riset", "studi", "dari mana", "sumber data", "pdf", "referensi", "bukti", "terurai", "berapa lama", "450", "tahun")):
        return (
            "Estimasi bahwa **botol plastik (PET) membutuhkan sekitar 450 tahun** untuk terurai berasal dari pengukuran laju degradasi permukaan polimer (*Specific Surface Degradation Rate*) di lingkungan perairan dan sedimen:\n\n"
            "1. **Chamas, A. et al. (2020)** — *Degradation Rates of Plastics in the Environment* ([Baca Jurnal](https://doi.org/10.1021/acssuschemeng.9b06635) | [Unduh PDF](https://pubs.acs.org/doi/pdf/10.1021/acssuschemeng.9b06635))\n"
            "2. **Jambeck, J. R. et al. (2015)** — *Plastic waste inputs from land into the ocean* ([Unduh PDF Science](https://www.science.org/doi/pdf/10.1126/science.1260352))\n"
            "3. **Lebreton, L. et al. (2017)** — *River plastic emissions to the world's oceans* ([Unduh PDF Nature](https://www.nature.com/articles/ncomms15611.pdf))"
        )

    pts_sorted = sorted(snap["points"], key=lambda x: x["total"], reverse=True)
    top_pt = pts_sorted[0] if pts_sorted else None
    return (
        f"Di Sungai Karangantu saat ini, titik terpadat berada di **{top_pt['code']} ({top_pt['zone']})** ({top_pt['total']} objek). "
        "Tanyakan apa saja seputar sampah, sungai di Banten, dampaknya bagi kesehatan, atau minta referensi PDF penelitian!"
    )


@app.get("/api/ai/status")
def ai_status(_: dict = Depends(require_user)):
    key = get_gemini_key()
    return {
        "llm_configured": bool(key),
        "model": _CACHED_WORKING_MODEL or ("auto" if key else "local"),
    }


@app.post("/api/ai/chat")
def ai_chat(body: AiChatIn, db: Session = Depends(get_db), _: dict = Depends(require_user)):
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "Pertanyaan tidak boleh kosong.")
    if len(msg) > 1000:
        raise HTTPException(400, "Pertanyaan terlalu panjang (maksimal 1000 karakter).")

    if SENSITIVE_PATTERN.search(msg):
        return {
            "reply": "Maaf, saya tidak tahu dan tidak memiliki akses terhadap email, kata sandi, maupun data akun petugas atau pengguna. Saya hanya dapat berdiskusi seputar ilmu sampah, limbah, dampak lingkungan, dan pemantauan sungai ARINA.",
            "engine": "guardrail",
        }

    quick = fast_smalltalk_reply(msg, body.nickname)
    if quick:
        return {"reply": quick, "engine": "instant-adaptive"}

    snap = build_river_snapshot(db)
    api_key = get_gemini_key()

    if api_key:
        llm_reply, detail = call_gemini_llm(msg, body.history, snap, api_key, body.nickname)
        if llm_reply:
            return {"reply": llm_reply, "engine": detail}
        return {"reply": local_arina_reply(msg, snap), "engine": f"local ({detail})"}

    return {"reply": local_arina_reply(msg, snap), "engine": "local (no_key)"}


@app.get("/api/alerts/latest")
def latest_alerts(db: Session = Depends(get_db), _: dict = Depends(require_user)):
    pts = {p.code: p for p in db.query(Point).all()}
    alerts = []

    for e in db.query(Event).order_by(Event.occurred_at.desc()).limit(5).all():
        p = pts.get(e.point_code)
        zone = p.zone if p else e.point_code
        alerts.append({
            "id": f"ev-{e.id}",
            "title": f"Deteksi Sampah Baru · {e.point_code} ({zone})",
            "body": f"Terdeteksi {e.category} ({e.count} objek) · Kepadatan {e.density}.",
            "density": e.density,
            "at": e.occurred_at.isoformat(),
        })

    for r in (
        db.query(PointReport)
        .filter(PointReport.status == "Valid")
        .order_by(PointReport.reviewed_at.desc())
        .limit(3)
        .all()
    ):
        dt = r.reviewed_at or r.created_at
        alerts.append({
            "id": f"rep-{r.id}",
            "title": f"Titik Sampah Tervalidasi · {r.location_name}",
            "body": f"Kepadatan {r.density}. {r.description or 'Lokasi telah diverifikasi petugas ARINA.'}",
            "density": r.density,
            "at": dt.isoformat(),
        })

    alerts.sort(key=lambda x: x["at"], reverse=True)
    return {"alerts": alerts[:6]}


# ----------------------------------------------------------------- statistik
@app.get("/api/stats")
def stats(point: str = "Semua", db: Session = Depends(get_db), _: dict = Depends(require_user)):
    pts = db.query(Point).order_by(Point.code).all()
    matrix = category_matrix(db)
    selected = [p.code for p in pts] if point == "Semua" else [point]
    if any(c not in matrix for c in selected):
        raise HTTPException(404, "Titik tidak ditemukan.")

    composition = {n: sum(matrix[c][n] for c in selected) for n in CATEGORY_NAMES}
    total = sum(composition.values())
    top_name, top_count = max(composition.items(), key=lambda kv: kv[1])
    plastic = sum(v for k, v in composition.items() if k in PLASTIC_NAMES)

    totals = {p.code: sum(matrix[p.code].values()) for p in pts}
    densest = max(selected, key=lambda c: totals[c])

    ev_q = db.query(Event)
    if point != "Semua":
        ev_q = ev_q.filter(Event.point_code == point)
    evs = ev_q.all()
    resolved = round(100 * sum(1 for e in evs if e.status == STATUS_FLOW[2]) / len(evs)) if evs else 0

    recap = []
    for p in pts:
        cats = matrix[p.code]
        recap.append({
            "code": p.code, "label": point_label(p), "total": totals[p.code],
            "dominant": max(cats.items(), key=lambda kv: kv[1])[0],
            "vs_prev_pct": round((totals[p.code] - p.prev_total) / p.prev_total * 100) if p.prev_total else 0,
        })
    monthly = [{"month": m.label, "plastic": m.plastic, "non_plastic": m.non_plastic,
                "total": m.plastic + m.non_plastic}
               for m in db.query(MonthlyStat).order_by(MonthlyStat.order).all()]
    return {
        "period": "Oktober 2026", "filter": point, "total": total,
        "top_category": {"name": top_name, "count": top_count},
        "densest_point": {"code": densest, "total": totals[densest]},
        "resolved_pct": resolved,
        "plastic_pct": round(100 * plastic / total) if total else 0,
        "by_point": [{"code": p.code, "label": point_label(p), "total": totals[p.code],
                      "categories": matrix[p.code]} for p in pts],
        "composition": [{"category": k, "count": v} for k, v in composition.items()],
        "monthly": monthly, "recap": recap,
    }


# ----------------------------------------------------------------- kamera (khusus petugas)
@app.get("/video_feed")
def video_feed(_: dict = Depends(require_admin)):
    """MJPEG stream. Pakai: <img src="/video_feed?token=..."> (khusus petugas)."""
    return StreamingResponse(video_engine.mjpeg(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/detections/current")
def detections_current(_: dict = Depends(require_admin)):
    video_engine.touch()
    return video_engine.current_detections()


@app.post("/api/camera/stop")
def camera_stop(_: dict = Depends(require_admin)):
    video_engine.stop_camera()
    return {"status": "stopped", **video_engine.status()}


@app.post("/api/camera/start")
def camera_start(_: dict = Depends(require_admin)):
    video_engine.start_camera()
    return {"status": "started", **video_engine.status()}


@app.get("/api/health")
def health():
    st = video_engine.status()
    return {"status": "ok", "use_yolo_model": st["mode"] == "yolov8", **st}