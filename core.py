"""
core.py - Logika inti standarisasi skala foto lesi kulit (tanpa UI).

Model kamera pinhole:  ukuran_fisik = ukuran_pixel * Z / f_pixel
Maka:                  skala (mm/pixel) = jarak_mm / focal_length_pixel
"""
import io
import math
from datetime import datetime

import numpy as np
import piexif
from PIL import Image, ImageDraw

TARGET_JARAK_MM = 100.0
TOLERANSI_MM = 5.0
RADIUS_FRAC = 0.18          # radius Focus Box = 18% dari sisi terpendek (dipakai overlay DAN ROI depth)
MAKS_SEBARAN_MM = 10.0      # selisih p90-p10 depth di dalam lingkaran; lebih besar = permukaan miring/tidak rata
MERAH = (220, 38, 38)
HIJAU = (22, 163, 74)
DIAGONAL_35MM = 43.2666  # mm, diagonal film 35mm


# ---------------------------------------------------------------- focal length
def fokus_px_dari_intrinsik(fx_ref: float, lebar_ref_px: float, lebar_gambar_px: float) -> float:
    """
    Skalakan fx (px) dari resolusi referensi ke resolusi foto sebenarnya.
    Android : fx dari LENS_INTRINSIC_CALIBRATION, ref = PRE_CORRECTION_ACTIVE_ARRAY_SIZE
    iOS     : fx dari intrinsicMatrix[0][0], ref = intrinsicMatrixReferenceDimensions
    """
    return fx_ref * lebar_gambar_px / lebar_ref_px


def fokus_px_dari_35mm(f35_mm: float, lebar_px: int, tinggi_px: int) -> float:
    """Estimasi fallback dari EXIF FocalLengthIn35mmFilm (tidak bergantung rasio aspek)."""
    return f35_mm * math.hypot(lebar_px, tinggi_px) / DIAGONAL_35MM


def baca_f35_exif(img: Image.Image):
    try:
        v = img.getexif().get_ifd(0x8769).get(0xA405)  # FocalLengthIn35mmFilm
        return float(v) if v else None
    except Exception:
        return None


# ---------------------------------------------------------------- skala & gerbang jarak
def hitung_skala(jarak_mm: float, fokus_px: float) -> float:
    return jarak_mm / fokus_px


def jarak_sesuai(jarak_mm: float, target=TARGET_JARAK_MM, toleransi=TOLERANSI_MM) -> bool:
    return abs(jarak_mm - target) <= toleransi


# ---------------------------------------------------------------- overlay Focus Box
def gambar_focus_box(img: Image.Image, sesuai: bool) -> Image.Image:
    """Salinan gambar + lingkaran di tengah (hijau jika sesuai, merah jika tidak). Asli tidak diubah."""
    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    w, h = out.size
    cx, cy, r = w // 2, h // 2, int(min(w, h) * RADIUS_FRAC)
    warna = HIJAU if sesuai else MERAH
    tebal = max(4, int(min(w, h) * 0.008))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=warna, width=tebal)
    k = r // 5
    d.line([cx - k, cy, cx + k, cy], fill=warna, width=max(2, tebal // 2))
    d.line([cx, cy - k, cx, cy + k], fill=warna, width=max(2, tebal // 2))
    return out


# ---------------------------------------------------------------- ROI lingkaran -> jarak
def mask_lingkaran(tinggi: int, lebar: int, radius_frac: float = RADIUS_FRAC) -> np.ndarray:
    """Mask boolean (tinggi x lebar): True untuk piksel di dalam Focus Box."""
    yy, xx = np.ogrid[:tinggi, :lebar]
    r = max(2, int(min(tinggi, lebar) * radius_frac))
    return (xx - lebar // 2) ** 2 + (yy - tinggi // 2) ** 2 <= r * r


def jarak_dari_depth(depth_mm: np.ndarray, radius_frac: float = RADIUS_FRAC, min_valid: float = 0.4):
    """
    Jarak = MEDIAN depth semua piksel valid (>0) di dalam lingkaran (bukan satu titik tengah).
    Return (median_mm, sebaran_mm, rasio_valid) atau None jika data valid < min_valid.
    sebaran = p90 - p10: besar bila permukaan miring / ada tepi, sehingga skala tidak seragam.
    (Logika yang sama dipakai di Kotlin & Swift.)
    """
    m = mask_lingkaran(*depth_mm.shape, radius_frac)
    v = depth_mm[m]
    valid = v[v > 0]
    if v.size == 0 or valid.size < min_valid * v.size:
        return None
    return (float(np.median(valid)),
            float(np.percentile(valid, 90) - np.percentile(valid, 10)),
            valid.size / v.size)


def roi_lingkaran(img: Image.Image, radius_frac: float = RADIUS_FRAC) -> Image.Image:
    """Potongan gambar persis area di dalam lingkaran (di luar lingkaran dibuat gelap)."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    r = int(min(w, h) * radius_frac)
    box = (w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r)
    crop = rgb.crop(box)
    m = mask_lingkaran(crop.size[1], crop.size[0], 0.5)
    arr = np.array(crop)
    arr[~m] = (30, 30, 30)
    return Image.fromarray(arr)


# ---------------------------------------------------------------- penyimpanan + metadata
def nama_file(skala: float, waktu: datetime = None) -> str:
    waktu = waktu or datetime.now()
    return f"lesi_{waktu:%Y%m%d_%H%M%S}_skala_mm_per_pixel_{skala:.5f}.jpg"


def siapkan_jpeg(data: bytes, meta: dict) -> bytes:
    """
    Sisipkan metadata ke EXIF TANPA re-encode piksel (foto utuh, tidak di-crop/kompres ulang).
    Jika input bukan JPEG, dikonversi sekali dengan kualitas tinggi.
    """
    if data[:2] != b"\xff\xd8":
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").save(buf, "JPEG", quality=98, subsampling=0)
        data = buf.getvalue()

    teks = ";".join(f"{k}={v}" for k, v in meta.items())
    try:
        exif = piexif.load(data)
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    exif["0th"][piexif.ImageIFD.ImageDescription] = teks.encode("ascii")
    exif["Exif"][piexif.ExifIFD.UserComment] = b"ASCII\x00\x00\x00" + teks.encode("ascii")

    out = io.BytesIO()
    piexif.insert(piexif.dump(exif), data, out)
    return out.getvalue()


def baca_metadata(jpeg_bytes: bytes) -> str:
    exif = piexif.load(jpeg_bytes)
    uc = exif["Exif"].get(piexif.ExifIFD.UserComment, b"")
    return uc[8:].decode("ascii", "ignore")
