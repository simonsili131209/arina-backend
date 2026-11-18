# Folder model YOLOv8

Letakkan file bobot model Anda di sini dengan nama `best.pt`:

    backend/models/best.pt

Lalu:
1. `pip install ultralytics`
2. Ubah `USE_YOLO_MODEL = True` di `backend/config.py`
3. Jalankan ulang backend.

Nama kelas model sebaiknya sama dengan 8 kategori ARINA (huruf besar/kecil dan
pemisah `_`, `-`, `/` diabaikan), misalnya `botol_plastik` -> "Botol plastik".
