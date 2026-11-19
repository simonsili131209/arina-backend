"""ARINA API - FastAPI. Jalankan: uvicorn main:app --reload --port 8000"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
import os
import re
from typing import Optional
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
from seed import seed_if_empty
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


# ----------------------------------------------------------------- auth
class LoginIn(BaseModel):
    email: str
    password: str


class RegisterIn(BaseModel):
    name: str
    email: str
    password: str


class GoogleLoginIn(BaseModel):
    email: str
    name: Optional[str] = None


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Email atau password salah.")
    info = {"sub": str(user.id), "email": user.email, "name": user.name, "role": user.role}
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/register")
def register(body: RegisterIn, db: Session = Depends(get_db)):
    clean_email = body.email.strip().lower()
    clean_name = body.name.strip()
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
    info = {"sub": str(user.id), "email": user.email, "name": user.name, "role": user.role}
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
    info = {"sub": str(user.id), "email": user.email, "name": user.name, "role": user.role}
    return {"token": create_token(info), "user": info}


@app.post("/api/auth/guest")
def guest():
    guest_id = f"guest_{os.urandom(4).hex()}"
    info = {"sub": guest_id, "email": "", "name": "Tamu", "role": "guest"}
    return {"token": create_token(info), "user": info}


@app.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    return {"user": {k: user[k] for k in ("sub", "email", "name", "role")}}


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


# ----------------------------------------------------------------- penitikan koordinat masyarakat & validasi petugas
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


@app.get("/api/point-reports")
def list_point_reports(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    rows = db.query(PointReport).order_by(PointReport.created_at.desc()).all()
    if user.get("role") == "admin":
        visible = rows
    else:
        uid = str(user.get("sub"))
        visible = [r for r in rows if r.status == "Valid" or r.reporter_id == uid]
    return {"reports": [report_out(r) for r in visible]}


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


# ----------------------------------------------------------------- asisten AI ARINA (Fundamental Sampah, Riset Peneliti, & Pemantauan Sungai)
class AiChatIn(BaseModel):
    message: str


SENSITIVE_PATTERN = re.compile(
    r"\b(email|e-mail|sandi|password|pass|kata\s*sandi|login|masuk\s*akun|username|user\s*id|token|jwt|secret|kredensial|hack|bobol|nomor\s*hp|telepon|whatsapp|wa\s*petugas|alamat\s*rumah)\b",
    re.IGNORECASE,
)

DOMAIN_PATTERN = re.compile(
    r"\b(sampah|limbah|sungai|arina|titik|t1|t2|t3|t4|t5|karangantu|hulu|hilir|plastik|botol|kemasan|kertas|kardus|organik|anorganik|b3|kain|logam|kaca|styrofoam|mikroplastik|polimer|pet|hdpe|ldpe|pp|tumpuk|penumpukan|kepadatan|tinggi|sedang|rendah|kejadian|laporan|peta|deteksi|kamera|yolo|kategori|statistik|lapor|bersih|banjir|daur\s*ulang|3r|terurai|urai|degradasi|lama|tahun|artikel|jurnal|peneliti|penelitian|riset|studi|data|sumber|referensi|pdf|bukti|ilmiah|ekosistem|pencemaran|polusi|lingkungan|dampak|bahaya|akibat|fundamental|pengertian|jenis|halo|hai|hi|pagi|siang|sore|malam|terima\s*kasih|makasih|apa\s*itu|siapa\s*kamu|bantu|info|terbaru|kondisi|status|kenapa|mengapa|bagaimana)\b",
    re.IGNORECASE,
)


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


def local_arina_reply(msg: str, snap: dict) -> str:
    q = msg.strip().lower()

    # 1. Sapaan hangat & alami
    if re.fullmatch(r"(halo|hai|hi|hello|assalamualaikum|selamat\s*(pagi|siang|sore|malam)|p|tes|test)[!.? ]*", q):
        return (
            "Halo! Senang bisa ngobrol denganmu. Saya **ARINA**, teman diskusimu seputar ilmu lingkungan, "
            "karakteristik sampah & limbah, dampak penumpukannya di sungai, hingga referensi riset ilmiah di balik data yang kami tampilkan. "
            "Ada topik atau data penelitian yang sedang bikin kamu penasaran?"
        )

    if any(k in q for k in ("siapa kamu", "apa itu arina", "fungsi kamu", "bisa apa")):
        return (
            "Saya **ARINA** — asisten cerdas yang dirancang khusus mendampingi sistem pemantauan Sungai Karangantu. "
            "Kamu bisa mengajak saya berdiskusi tentang konsep fundamental sampah dan limbah, dampak ekologis penumpukan di aliran sungai, "
            "kondisi titik pemantauan terkini, maupun menelusuri **jurnal dan artikel PDF para peneliti** yang menjadi rujukan data waktu urai sampah."
        )

    # 2. Pertanyaan sumber data ilmiah, lama penguraian plastik, artikel & PDF peneliti
    if any(k in q for k in ("artikel", "jurnal", "peneliti", "penelitian", "riset", "studi", "dari mana", "sumber data", "pdf", "referensi", "bukti", "terurai", "berapa lama", "450", "tahun", "degradasi")):
        return (
            "Estimasi bahwa **botol plastik (PET) membutuhkan sekitar 450 tahun** dan kantong plastik (HDPE/LDPE) puluhan hingga ratusan tahun untuk terurai bukan sekadar perkiraan kasar, melainkan hasil pengukuran laju degradasi polimer (*Specific Surface Degradation Rate* / SSDR) di lingkungan perairan dan sedimen.\n\n"
            "**Mengapa botol plastik begitu sulit terurai di sungai?**\n"
            "• Rantai polimer *Polyethylene Terephthalate* (PET) memiliki ikatan ester aromatik yang sangat stabil dan kristalinitas tinggi sehingga enzim mikroba alami hampir tidak mampu memutusnya.\n"
            "• Saat botol tenggelam atau tertutup lumpur sungai, paparan sinar UV matahari dan oksigen terlarut menurun drastis, sehingga proses fotodegradasi melambat hingga puluhan kali lipat dibanding di daratan terbuka.\n\n"
            "**Referensi Artikel & PDF Para Peneliti:**\n"
            "1. **Chamas, A. et al. (2020)** — *Degradation Rates of Plastics in the Environment* (ACS Sustainable Chemistry & Engineering). Mengukur langsung laju peluruhan massa polimer PET, HDPE, LDPE, dan PP di lingkungan tanah maupun laut.\n"
            "   • [Baca Jurnal (DOI)](https://doi.org/10.1021/acssuschemeng.9b06635) | [Unduh PDF Peneliti](https://pubs.acs.org/doi/pdf/10.1021/acssuschemeng.9b06635)\n"
            "2. **Jambeck, J. R. et al. (2015)** — *Plastic waste inputs from land into the ocean* (Science). Studi rujukan global mengenai aliran sampah plastik dari wilayah pesisir dan daerah aliran sungai.\n"
            "   • [Baca Jurnal (Science)](https://doi.org/10.1126/science.1260352) | [Unduh PDF](https://www.science.org/doi/pdf/10.1126/science.1260352)\n"
            "3. **Lebreton, L. et al. (2017)** — *River plastic emissions to the world's oceans* (Nature Communications). Memodelkan transportasi sampah plastik dari hulu sungai menuju muara dan laut.\n"
            "   • [Baca & Unduh PDF Open-Access](https://www.nature.com/articles/ncomms15611.pdf)\n"
            "4. **UNEP (2021)** — *From Pollution to Solution: A Global Assessment of Marine Litter and Plastic Pollution*. Laporan komprehensif PBB mengenai dampak dan siklus degradasi sampah perairan.\n"
            "   • [Unduh Laporan PDF Resmi](https://wedocs.unep.org/bitstream/handle/20.500.11822/36963/POLSOL.pdf)"
        )

    # 3. Konsep fundamental sampah vs limbah & klasifikasi dasar
    if any(k in q for k in ("fundamental", "perbedaan sampah dan limbah", "apa itu limbah", "apa itu sampah", "pengertian", "organik", "anorganik", "b3", "mikroplastik")):
        return (
            "Secara fundamental dalam ilmu lingkungan, **sampah** dan **limbah** memiliki cakupan yang saling berkaitan namun berbeda fokus:\n\n"
            "• **Sampah (*Solid Waste*):** Sisa aktivitas harian manusia atau proses alam yang berbentuk padat (seperti botol PET, kemasan berlapis aluminium foil, sisa makanan, kayu, dan tekstil).\n"
            "• **Limbah (*Waste / Effluent*):** Buangan hasil proses produksi, industri, maupun domestik yang dapat berwujud cair, gas, maupun padat — sering kali membawa beban pencemar kimia atau biologis langsung ke badan air.\n\n"
            "**Pembagian Fundamental Sampah di Ekosistem Sungai:**\n"
            "1. **Organik (*Biodegradable*):** Mudah diurai mikroorganisme (sisa makanan, ranting, daun), tetapi jika menumpuk berlebihan akan memicu pembusukan anaerob dan menguras oksigen terlarut (*Dissolved Oxygen*).\n"
            "2. **Anorganik Persisten:** Sulit terurai secara biologis (plastik sekali pakai, styrofoam, kaca, logam). Di sungai, plastik tidak benar-benar hilang, melainkan terfragmentasi secara mekanis menjadi **mikroplastik (< 5 mm)**.\n"
            "3. **Bahan Berbahaya & Beracun (B3):** Baterai bekas, kemasan pestisida, atau oli yang melepaskan logam berat dan senyawa toksik ke rantai makanan perairan.\n\n"
            "Referensi riset produksi & nasib seluruh polimer plastik global:\n"
            "• **Geyer, R., Jambeck, J. R., & Law, K. L. (2017)** — *Production, use, and fate of all plastics ever made* ([Unduh PDF Science Advances](https://www.science.org/doi/pdf/10.1126/sciadv.1700782))"
        )

    # 4. Dampak penumpukan sampah di sungai
    if any(k in q for k in ("dampak", "akibat", "bahaya", "banjir", "ekosistem", "ikan", "pencemaran", "polusi", "kesehatan")):
        return (
            "Penumpukan sampah di badan sungai menimbulkan dampak berantai pada tiga aspek utama:\n\n"
            "1. **Hidrologis (Penyempitan Penampang Sungai):** Sumbatan sampah plastik dan ranting di pilar jembatan atau tikungan sungai menghambat debit aliran saat hujan deras, memicu luapan banjir lokal.\n"
            "2. **Ekologis & Kualitas Air:** Timbunan sampah menghalangi penetrasi cahaya matahari ke dasar perairan, menurunkan kadar oksigen terlarut (hipoksia), serta melepaskan zat aditif plastik (*phthalates*, *bisphenol-A*) yang mengganggu biota air dan ikan konsumsi.\n"
            "3. **Bioakumulasi Mikroplastik:** Fragmentasi sampah plastik membentuk partikel mikroplastik yang tertelan oleh plankton dan ikan muara, lalu berpindah kembali ke tubuh manusia melalui rantai pangan.\n\n"
            "Rujukan ilmiah fragmentasi & akumulasi sampah perairan:\n"
            "• **Barnes, D. K. A. et al. (2009)** — *Accumulation and fragmentation of plastic debris in global environments* ([Unduh PDF Royal Society](https://royalsocietypublishing.org/doi/pdf/10.1098/rstb.2008.0205))"
        )

    # 5. Cek titik pemantauan spesifik T1 - T5
    code_match = re.search(r"\bt([1-5])\b", q)
    if code_match:
        target_code = f"T{code_match.group(1)}"
        pt = next((p for p in snap["points"] if p["code"] == target_code), None)
        if pt:
            dom = ", ".join(pt["top_cats"]) if pt["top_cats"] else "belum ada dominasi kategori"
            return (
                f"Berdasarkan data pemantauan terbaru di **{pt['code']} ({pt['name']} · {pt['zone']})**, tingkat kepadatannya berada pada level **{pt['density']}** "
                f"dengan total **{pt['total']} objek** sampah terdeteksi. Komposisi paling dominan berasal dari {dom}."
            )

    # 6. Ringkasan kondisi sungai terkini
    pts_sorted = sorted(snap["points"], key=lambda x: x["total"], reverse=True)
    top_pt = pts_sorted[0] if pts_sorted else None
    latest_ev = snap["events"][0] if snap["events"] else None

    parts = []
    if top_pt:
        parts.append(
            f"Secara keseluruhan di Sungai Karangantu, titik dengan akumulasi tertinggi saat ini adalah **{top_pt['code']} ({top_pt['zone']})** "
            f"dengan **{top_pt['total']} objek** sampah (kepadatan **{top_pt['density']}**)."
        )
    if latest_ev:
        parts.append(
            f"Temuan terbaru yang masuk ke sistem adalah **{latest_ev['category']}** ({latest_ev['count']} objek) di **{latest_ev['point_code']} ({latest_ev['zone']})** "
            f"pada {latest_ev['time']} dengan status *{latest_ev['status']}*."
        )
    parts.append(
        "Kalau kamu ingin membedah sisi ilmiahnya — seperti bagaimana proses polimer terurai, dampak mikroplastik bagi biota sungai, atau meminta **tautan jurnal & PDF penelitian** — silakan tanyakan saja!"
    )
    return "\n\n".join(parts)


@app.post("/api/ai/chat")
def ai_chat(body: AiChatIn, db: Session = Depends(get_db), _: dict = Depends(require_user)):
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "Pertanyaan tidak boleh kosong.")
    if len(msg) > 800:
        raise HTTPException(400, "Pertanyaan terlalu panjang (maksimal 800 karakter).")

    # Guardrail 1: Tolak tegas pertanyaan seputar kredensial, email, sandi, atau data akun petugas
    if SENSITIVE_PATTERN.search(msg):
        return {
            "reply": "Maaf, saya tidak tahu dan tidak memiliki akses terhadap email, kata sandi, maupun data akun petugas atau pengguna. Saya hanya dapat berdiskusi seputar ilmu sampah, limbah, dampak lingkungan, dan pemantauan sungai ARINA.",
            "guardrailed": True,
        }

    # Guardrail 2: Jaga tetap di koridor ilmu sampah, limbah, lingkungan, & pemantauan ARINA
    if not DOMAIN_PATTERN.search(msg):
        return {
            "reply": "Topik itu berada di luar ruang lingkup saya. Yuk kita diskusikan hal-hal seputar sampah, limbah, dampak penumpukannya di sungai, atau referensi jurnal penelitian lingkungan!",
            "guardrailed": True,
        }

    snap = build_river_snapshot(db)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if api_key:
        try:
            sys_prompt = (
                "Kamu adalah ARINA, asisten AI berwawasan lingkungan dan ilmiah untuk website pemantauan Sungai Karangantu. "
                "Gaya bicaramu hangat, mengalir, cerdas, tidak kaku, dan edukatif. "
                "ATURAN WAJIB: "
                "1. Jelaskan konsep fundamental tentang sampah, limbah, degradasi material, dan dampak penumpukan sampah secara mendalam namun mudah dipahami. "
                "2. Jika pengguna menanyakan sumber data atau meminta artikel penelitian (misal lama botol plastik terurai), jelaskan mekanisme ilmiahnya dan sertakan referensi jurnal peneliti asli lengkap dengan format tautan Markdown [Judul/Sumber](https://...) menuju DOI atau PDF terbuka. "
                "3. DILARANG KERAS memberikan atau mengarang email, kata sandi, nomor telepon, atau data akun petugas/pengguna; jika ditanya jawab 'Maaf, saya tidak tahu.' "
                "4. Gunakan data pemantauan sungai terkini berikut jika pengguna menanyakan kondisi titik:\n"
                f"{json.dumps(snap, ensure_ascii=False)}"
            )
            payload = {
                "contents": [{"parts": [{"text": f"{sys_prompt}\n\nPertanyaan pengguna: {msg}"}]}],
                "generationConfig": {"temperature": 0.45, "maxOutputTokens": 700},
            }
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                text = raw["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text:
                    return {"reply": text, "guardrailed": False}
        except Exception:
            pass

    return {"reply": local_arina_reply(msg, snap), "guardrailed": False}


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