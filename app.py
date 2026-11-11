"""
app.py - Prototipe Streamlit: standarisasi skala foto lesi kulit (mm per pixel).

Jalankan:  streamlit run app.py

Catatan: browser/Streamlit tidak punya akses ARCore/ARKit Depth. Maka `jarak_sekarang`
disimulasikan lewat slider di sidebar. Logika lainnya (focal px -> skala, Focus Box
merah/hijau, kunci/auto-simpan, metadata) sama dengan yang dipakai di aplikasi HP.
"""
import hashlib
from pathlib import Path

import streamlit as st
from PIL import Image
import io

import core

OUT = Path("output")
OUT.mkdir(exist_ok=True)

st.set_page_config(page_title="Standarisasi Foto Lesi Kulit", layout="wide")
st.title("Standarisasi Skala Foto Lesi Kulit (mm/pixel)")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Pengaturan")
    platform = st.radio("Platform HP", ["Android", "iOS"], horizontal=True)
    target = st.number_input("TARGET_JARAK (mm)", value=core.TARGET_JARAK_MM, step=1.0)
    tol = st.number_input("Toleransi ± (mm)", value=core.TOLERANSI_MM, step=0.5)

    st.subheader("Simulasi depth")
    jarak = st.slider("jarak_sekarang (mm)", 50.0, 400.0, float(target), 0.5,
                      help="Di HP: median depth ARCore (Android) / LiDAR (iOS) dari SEMUA piksel di dalam lingkaran.")

    st.subheader("Focal length (pixel)")
    sumber_f = st.radio("Sumber", ["Intrinsik kamera (input)", "Estimasi EXIF 35mm"])
    if sumber_f.startswith("Intrinsik"):
        if platform == "Android":
            st.caption("Dari `CameraCharacteristics.LENS_INTRINSIC_CALIBRATION` -> [fx, fy, cx, cy, s]. "
                       "Resolusi referensi = `SENSOR_INFO_PRE_CORRECTION_ACTIVE_ARRAY_SIZE`.")
        else:
            st.caption("Dari `intrinsicMatrix[0][0]` (AVCameraCalibrationData / ARCamera.intrinsics). "
                       "Resolusi referensi = `intrinsicMatrixReferenceDimensions` / `imageResolution`.")
        fx_ref = st.number_input("fx (px)", value=3000.0, step=10.0)
        lebar_ref = st.number_input("Lebar resolusi referensi (px)", value=4000, step=1)

# ------------------------------------------------------------------ sumber foto
MODE_JS = "Live + lingkaran (tanpa paket tambahan)"
MODE_LIVE = "Live + lingkaran (WebRTC, opsional)"
MODE_SNAP = "Kamera browser (snapshot)"
MODE_UPL = "Upload foto (dari kamera HP, resolusi penuh)"
sumber = st.radio("Sumber foto", [MODE_JS, MODE_LIVE, MODE_SNAP, MODE_UPL], horizontal=True)

# Lingkaran hanya bisa tampil di preview pada mode LIVE. st.camera_input (snapshot) tidak
# mendukung overlay, jadi lingkaran baru terlihat pada foto setelah diambil.
data = None
if sumber == MODE_JS:
    # Komponen HTML/JS lokal (folder focus_cam/): kamera + lingkaran digambar di browser,
    # tidak butuh pip install apa pun. Nilai kembaliannya = foto JPEG (data URL) dari stream kamera.
    import base64
    import streamlit.components.v1 as components

    _focus_cam = components.declare_component("focus_cam", path=str(Path(__file__).parent / "focus_cam"))
    hasil_js = _focus_cam(sesuai=bool(core.jarak_sesuai(jarak, target, tol)),
                          radius=core.RADIUS_FRAC, stabil=15, key="focus_cam", default=None)
    if not hasil_js or not hasil_js.get("jpeg"):
        st.info("Izinkan kamera di browser. Lingkaran tampil di atas video (merah = terkunci, hijau = sesuai). "
                "Atur `jarak_sekarang` di sidebar untuk simulasi.")
        st.stop()
    data = base64.b64decode(hasil_js["jpeg"].split(",", 1)[1])
elif sumber == MODE_LIVE:
    try:
        import threading
        import time
        import av
        from streamlit_webrtc import VideoProcessorBase, webrtc_streamer
    except ImportError:
        st.error("Mode live butuh paket tambahan: `python -m pip install streamlit-webrtc av` "
                 "(atau pilih mode snapshot/upload).")
        st.stop()

    class FocusProcessor(VideoProcessorBase):
        FRAME_STABIL = 15                       # ~0,5 detik hijau berturut-turut -> auto-simpan

        def __init__(self):
            self.sesuai = False                 # diisi dari thread utama tiap rerun
            self.stabil = 0
            self.terakhir = None                # PIL frame terbaru (tanpa lingkaran)
            self.auto = None                    # frame yang terpicu otomatis saat hijau stabil
            self.kunci = threading.Lock()

        def recv(self, frame):
            im = frame.to_image()
            with self.kunci:
                self.terakhir = im.copy()
                self.stabil = self.stabil + 1 if self.sesuai else 0
                if self.stabil >= self.FRAME_STABIL and self.auto is None:
                    self.auto = im.copy()
            return av.VideoFrame.from_image(core.gambar_focus_box(im, self.sesuai))

    # Pilihan kamera: key berbeda -> stream di-restart dengan constraint baru.
    # "ideal" (bukan "exact") agar laptop tanpa kamera belakang tetap jalan memakai kamera yang ada.
    kamera = st.radio("Kamera", ["Belakang", "Depan"], horizontal=True, key="pilih_kamera")
    facing = "environment" if kamera == "Belakang" else "user"
    ctx = webrtc_streamer(
        key=f"live_{facing}",
        video_processor_factory=FocusProcessor,
        media_stream_constraints={
            "video": {"facingMode": {"ideal": facing}, "width": {"ideal": 1920}, "height": {"ideal": 1080}},
            "audio": False,
        },
    )
    if not ctx.state.playing or ctx.video_processor is None:
        st.info("Klik START dan izinkan kamera. Lingkaran akan tampil di atas video "
                "(merah = terkunci, hijau = sesuai). Atur `jarak_sekarang` di sidebar untuk simulasi.")
        st.stop()

    proc = ctx.video_processor
    proc.sesuai = core.jarak_sesuai(jarak, target, tol)     # warna lingkaran ikut slider

    kol1, kol2 = st.columns(2)
    if kol2.button("Ulangi (reset auto-simpan)"):
        proc.auto = None
        proc.stabil = 0
    ambil = kol1.button("Ambil foto sekarang", disabled=not proc.sesuai)

    frame_pil = None
    if ambil:
        frame_pil = proc.terakhir
    elif proc.auto is not None:
        frame_pil = proc.auto
    else:
        st.caption("Menunggu lingkaran HIJAU stabil (auto-simpan) atau tekan tombol ambil foto...")
        for _ in range(50):                                  # polling ~10 dtk, lalu rerun
            if proc.auto is not None:
                break
            time.sleep(0.2)
        st.rerun()

    if frame_pil is None:
        st.stop()
    buf = io.BytesIO()
    frame_pil.convert("RGB").save(buf, "JPEG", quality=95)
    data = buf.getvalue()
else:
    foto = st.camera_input("Ambil foto") if sumber == MODE_SNAP else \
        st.file_uploader("Pilih foto", type=["jpg", "jpeg", "png"])
    if foto is None:
        st.info("Atur `jarak_sekarang` di sidebar, lalu ambil/unggah foto.")
        st.stop()
    data = foto.getvalue()

img = Image.open(io.BytesIO(data))
W, H = img.size

# ------------------------------------------------------------------ focal px & skala
if sumber_f.startswith("Intrinsik"):
    fokus_px = core.fokus_px_dari_intrinsik(fx_ref, lebar_ref, max(W, H))
    ket_f = f"intrinsik ({platform})"
else:
    f35 = core.baca_f35_exif(img)
    if not f35:
        st.error("EXIF FocalLengthIn35mmFilm tidak ada pada foto ini. Gunakan mode intrinsik.")
        st.stop()
    fokus_px = core.fokus_px_dari_35mm(f35, W, H)
    ket_f = f"EXIF 35mm = {f35:.1f} mm"

skala = core.hitung_skala(jarak, fokus_px)
sesuai = core.jarak_sesuai(jarak, target, tol)

# ------------------------------------------------------------------ tampilan
kiri, kanan = st.columns([3, 2])
with kiri:
    st.image(core.gambar_focus_box(img, sesuai), caption=f"Focus Box ({W}x{H} px, foto utuh)",
             use_container_width=True)
with kanan:
    st.metric("jarak_sekarang", f"{jarak:.1f} mm", delta=f"{jarak - target:+.1f} mm dari target")
    st.metric("focal length", f"{fokus_px:.1f} px", help=ket_f)
    st.metric("skala_mm_per_pixel", f"{skala:.5f}")
    st.caption(f"Diameter lingkaran ≈ {skala * min(W, H) * 2 * core.RADIUS_FRAC:.1f} mm")
    st.image(core.roi_lingkaran(img), width=180,
             caption="Area di dalam lingkaran = ROI untuk membaca jarak (median depth)")

# ------------------------------------------------------------------ gerbang: merah=kunci, hijau=auto-simpan
if not sesuai:
    st.error(f"MERAH - jarak di luar {target:.0f} ± {tol:.0f} mm. Pengambilan gambar DIKUNCI.")
    st.button("Simpan foto (terkunci)", disabled=True)
else:
    st.success(f"HIJAU - jarak sesuai ({jarak:.1f} mm). Foto disimpan otomatis.")
    kunci = hashlib.md5(data + f"{jarak:.1f}{fokus_px:.2f}".encode()).hexdigest()
    if st.session_state.get("kunci_terakhir") != kunci:
        meta = {
            "skala_mm_per_pixel": f"{skala:.6f}",
            "jarak_mm": f"{jarak:.1f}",
            "focal_px": f"{fokus_px:.2f}",
            "resolusi": f"{W}x{H}",
            "platform": platform,
        }
        jpeg = core.siapkan_jpeg(data, meta)
        nama = core.nama_file(skala)
        (OUT / nama).write_bytes(jpeg)
        st.session_state["kunci_terakhir"] = kunci
        st.session_state["hasil"] = (nama, jpeg)

    if "hasil" in st.session_state:
        nama, jpeg = st.session_state["hasil"]
        st.write(f"Tersimpan: `output/{nama}`")
        st.caption(f"EXIF UserComment: `{core.baca_metadata(jpeg)}`")
        st.download_button("Unduh foto", jpeg, file_name=nama, mime="image/jpeg")
