"""Autentikasi ringan: token bertanda tangan HMAC (tanpa dependensi tambahan)."""
import base64, hashlib, hmac, json, os, time
from typing import Optional
from fastapi import Depends, Header, HTTPException, Query
from config import SECRET_KEY, TOKEN_TTL_HOURS


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or os.urandom(8).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${h}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(user: dict) -> str:
    payload = {**user, "exp": int(time.time()) + TOKEN_TTL_HOURS * 3600}
    body = _b64(json.dumps(payload).encode())
    sig = _b64(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str) -> dict:
    try:
        body, sig = token.split(".")
        expected = _b64(hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        payload = json.loads(_unb64(body))
        if payload["exp"] < time.time():
            raise ValueError
        return payload
    except Exception:
        raise HTTPException(401, "Sesi tidak valid atau sudah berakhir. Silakan masuk kembali.")


def require_user(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)) -> dict:
    """Menerima header 'Authorization: Bearer <t>' atau query ?token= (untuk <img src=/video_feed>)."""
    raw = token
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:]
    if not raw:
        raise HTTPException(401, "Belum masuk. Pilih Login atau Guest terlebih dahulu.")
    return decode_token(raw)


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "Fitur ini hanya tersedia untuk Petugas terdaftar. Silakan Login.")
    return user
