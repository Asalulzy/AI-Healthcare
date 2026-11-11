// =====================================================================================
// LesionCapture.kt  -  Android (Kotlin): CameraX + ARCore Depth + LENS_INTRINSIC_CALIBRATION
//
// PENTING (batasan arsitektur):
//  * CameraX dan ARCore TIDAK bisa membuka kamera belakang secara bersamaan.
//  * Karena itu alurnya 2 fase:
//      FASE UKUR  : ARCore Session berjalan (GLSurfaceView + BackgroundRenderer dari sample
//                   hello_ar_kotlin) -> baca depth tengah -> FocusBoxView merah/hijau.
//      FASE AMBIL : saat hijau stabil N frame -> session.pause() -> bind CameraX
//                   (ImageCapture resolusi penuh) -> takePicture() -> session.resume().
//  * CameraX dipaksa memakai cameraId yang SAMA dengan ARCore agar fx konsisten.
//
// Dependensi (build.gradle):
//   androidx.camera:camera-core / camera-camera2 / camera-lifecycle / camera-view  (1.4.x)
//   androidx.exifinterface:exifinterface
//   com.google.ar:core  (versi terbaru)
// =====================================================================================
package com.example.lesioncapture

import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.util.AttributeSet
import android.view.View
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.*
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.exifinterface.media.ExifInterface
import androidx.lifecycle.LifecycleOwner
import com.google.ar.core.Config
import com.google.ar.core.Frame
import com.google.ar.core.Session
import com.google.ar.core.exceptions.NotYetAvailableException
import java.io.File
import java.nio.ByteOrder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.abs
import kotlin.math.max

// -------------------------------------------------------------------------------------
// 1. KONFIGURASI
// -------------------------------------------------------------------------------------
object CaptureConfig {
    const val TARGET_JARAK_MM = 100f   // jarak baku kamera -> kulit (10 cm)
    const val TOLERANSI_MM = 5f        // ±5 mm
    const val FRAME_STABIL = 5         // jumlah frame berturut-turut sebelum auto-capture
}

// -------------------------------------------------------------------------------------
// 2. FOCAL LENGTH (PIXEL) DARI LENS_INTRINSIC_CALIBRATION
//    Nilai = [fx, fy, cx, cy, s] dalam pixel, relatif terhadap
//    SENSOR_INFO_PRE_CORRECTION_ACTIVE_ARRAY_SIZE -> harus diskalakan ke resolusi foto.
// -------------------------------------------------------------------------------------
data class FocalPx(val fx: Float, val fy: Float, val sumber: String)

class IntrinsicsProvider(context: Context) {
    private val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager

    /** lebar/tinggi null -> asumsi resolusi sensor penuh (untuk tampilan real-time). */
    fun focalPx(cameraId: String, lebar: Int? = null, tinggi: Int? = null): FocalPx? {
        val c = manager.getCameraCharacteristics(cameraId)
        val ref = c.get(CameraCharacteristics.SENSOR_INFO_PRE_CORRECTION_ACTIVE_ARRAY_SIZE)
            ?: c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: return null

        // pakai sisi terpanjang agar tidak terpengaruh rotasi EXIF
        val faktor = if (lebar != null && tinggi != null)
            max(lebar, tinggi).toFloat() / max(ref.width(), ref.height()) else 1f

        val k = c.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION)
        if (k != null && k.size >= 2 && k[0] > 0f)
            return FocalPx(k[0] * faktor, k[1] * faktor, "LENS_INTRINSIC_CALIBRATION")

        // Fallback (HP tanpa data kalibrasi): f_mm / lebar_sensor_mm * lebar_px
        val fMm = c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)?.firstOrNull() ?: return null
        val sensor = c.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE) ?: return null
        val px = fMm / sensor.width * ref.width() * faktor
        return FocalPx(px, px, "fallback_focal_length_mm")
    }
}

/** skala (mm/pixel) = jarak / focal_length_pixel */
fun hitungSkala(jarakMm: Float, fx: Float): Float = jarakMm / fx

// -------------------------------------------------------------------------------------
// 3. JARAK DARI ARCORE DEPTH API (median seluruh area di dalam lingkaran Focus Box)
//    acquireDepthImage16Bits(): tiap piksel = jarak sepanjang sumbu Z kamera dalam mm
//    (cocok dengan model pinhole Z / f). Nilai 0 = tidak valid.
// -------------------------------------------------------------------------------------
fun buatSessionDepth(context: android.app.Activity): Session {
    val session = Session(context)
    val cfg = Config(session)
    if (session.isDepthModeSupported(Config.DepthMode.AUTOMATIC)) cfg.depthMode = Config.DepthMode.AUTOMATIC
    session.configure(cfg)
    return session
}

/** Hasil pengukuran area di dalam lingkaran Focus Box. */
data class DepthUkur(val medianMm: Float, val sebaranMm: Float, val rasioValid: Float)

/**
 * Jarak = MEDIAN depth semua piksel valid di dalam LINGKARAN (bukan satu titik tengah).
 * Radius = RADIUS_FRAC x sisi terpendek, sama dengan radius yang digambar FocusBoxView.
 * sebaranMm = p90 - p10: besar bila permukaan miring / ada tepi -> skala tidak seragam.
 * Catatan: depth image sejajar dengan frame kamera ARCore; bila preview memakai FILL_CENTER
 * (ter-crop), lingkaran di layar dan di depth image sedikit berbeda -> pakai FIT atau petakan
 * koordinat layar ke depth (Frame.transformCoordinates2d).
 */
const val RADIUS_FRAC = 0.18f

fun jarakDalamLingkaranMm(frame: Frame): DepthUkur? {
    val img = try { frame.acquireDepthImage16Bits() } catch (e: NotYetAvailableException) { return null }
    img.use {
        val plane = it.planes[0]
        val buf = plane.buffer.order(ByteOrder.nativeOrder())
        val w = it.width; val h = it.height; val stride = plane.rowStride
        val cx = w / 2; val cy = h / 2
        val r = (minOf(w, h) * RADIUS_FRAC).toInt().coerceAtLeast(2)
        val nilai = ArrayList<Int>()
        var total = 0
        for (dy in -r..r) for (dx in -r..r) {
            if (dx * dx + dy * dy > r * r) continue          // hanya piksel di dalam lingkaran
            total++
            val mm = buf.getShort((cy + dy) * stride + (cx + dx) * 2).toInt() and 0xFFFF
            if (mm > 0) nilai.add(mm)                        // 0 = tidak valid
        }
        if (total == 0 || nilai.size < 0.4f * total) return null
        nilai.sort()
        val p10 = nilai[(nilai.size * 0.1f).toInt()]
        val p90 = nilai[(nilai.size * 0.9f).toInt().coerceAtMost(nilai.size - 1)]
        return DepthUkur(nilai[nilai.size / 2].toFloat(), (p90 - p10).toFloat(), nilai.size.toFloat() / total)
    }
}

// -------------------------------------------------------------------------------------
// 4. GERBANG JARAK: sesuai? stabil N frame? -> siap auto-capture
// -------------------------------------------------------------------------------------
class FocusGate(
    private val target: Float = CaptureConfig.TARGET_JARAK_MM,
    private val toleransi: Float = CaptureConfig.TOLERANSI_MM,
    private val frameStabil: Int = CaptureConfig.FRAME_STABIL,
) {
    data class Hasil(val sesuai: Boolean, val siapAmbil: Boolean)
    private var stabil = 0

    /** Hijau hanya jika median di dalam lingkaran dalam toleransi DAN permukaan cukup rata. */
    fun update(ukur: DepthUkur?, maksSebaranMm: Float = 10f): Hasil {
        val ok = ukur != null && abs(ukur.medianMm - target) <= toleransi && ukur.sebaranMm <= maksSebaranMm
        stabil = if (ok) stabil + 1 else 0
        return Hasil(ok, stabil >= frameStabil)
    }
    fun reset() { stabil = 0 }
}

// -------------------------------------------------------------------------------------
// 5. FOCUS BOX (Canvas) di atas preview: MERAH = terkunci, HIJAU = sesuai
// -------------------------------------------------------------------------------------
class FocusBoxView @JvmOverloads constructor(ctx: Context, attrs: AttributeSet? = null) : View(ctx, attrs) {
    var sesuai: Boolean = false
        set(v) { field = v; invalidate() }
    var info: String = ""
        set(v) { field = v; invalidate() }

    private val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 8f * resources.displayMetrics.density / 2f
    }
    private val teks = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; textSize = 16f * resources.displayMetrics.scaledDensity; textAlign = Paint.Align.CENTER
        setShadowLayer(4f, 0f, 0f, Color.BLACK)
    }

    override fun onDraw(c: Canvas) {
        val cx = width / 2f; val cy = height / 2f
        val r = minOf(width, height) * 0.18f
        ring.color = if (sesuai) Color.rgb(22, 163, 74) else Color.rgb(220, 38, 38)
        c.drawCircle(cx, cy, r, ring)
        c.drawLine(cx - r / 5, cy, cx + r / 5, cy, ring)
        c.drawLine(cx, cy - r / 5, cx, cy + r / 5, ring)
        c.drawText(info, cx, cy + r + teks.textSize * 1.6f, teks)
    }
}

// -------------------------------------------------------------------------------------
// 6. CAMERAX: ImageCapture resolusi penuh, TANPA crop, + nama file & EXIF berisi skala
// -------------------------------------------------------------------------------------
@OptIn(ExperimentalCamera2Interop::class)
class LesionCapturer(private val context: Context, private val intrinsics: IntrinsicsProvider) {

    // 4:3 + resolusi tertinggi = area sensor terbesar. TIDAK memakai ViewPort/UseCaseGroup
    // sehingga file hasil = frame utuh sensor, bukan hasil crop sesuai tampilan PreviewView.
    val imageCapture: ImageCapture = ImageCapture.Builder()
        .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
        .setResolutionSelector(
            ResolutionSelector.Builder()
                .setAspectRatioStrategy(AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY)
                .setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY)
                .build()
        ).build()

    private val preview = Preview.Builder().build()

    /** Bind CameraX ke kamera dengan cameraId yang sama dengan ARCore. */
    fun bind(owner: LifecycleOwner, previewView: androidx.camera.view.PreviewView,
             arCoreCameraId: String, siap: (cameraId: String) -> Unit) {
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            val provider = future.get()
            val selector = CameraSelector.Builder()
                .addCameraFilter { infos -> infos.filter { Camera2CameraInfo.from(it).cameraId == arCoreCameraId } }
                .build()
            provider.unbindAll()
            preview.setSurfaceProvider(previewView.surfaceProvider)
            val cam = provider.bindToLifecycle(owner, selector, preview, imageCapture)
            cam.cameraControl.setZoomRatio(1f)               // tanpa digital zoom -> skala konsisten
            siap(Camera2CameraInfo.from(cam.cameraInfo).cameraId)
        }, ContextCompat.getMainExecutor(context))
    }

    fun ambilFoto(cameraId: String, jarakMm: Float, onSelesai: (File) -> Unit, onError: (Exception) -> Unit) {
        val dir = context.getExternalFilesDir(android.os.Environment.DIRECTORY_PICTURES)!!
        val tmp = File(dir, "tmp_${System.currentTimeMillis()}.jpg")
        val opsi = ImageCapture.OutputFileOptions.Builder(tmp).build()

        imageCapture.takePicture(opsi, ContextCompat.getMainExecutor(context),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(r: ImageCapture.OutputFileResults) {
                    try {
                        // resolusi ASLI file -> skalakan fx ke resolusi itu
                        val b = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                        BitmapFactory.decodeFile(tmp.path, b)
                        val f = intrinsics.focalPx(cameraId, b.outWidth, b.outHeight)
                            ?: throw IllegalStateException("Focal length tidak tersedia")
                        val skala = hitungSkala(jarakMm, f.fx)

                        val ts = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
                        val nama = String.format(Locale.US, "lesi_%s_skala_mm_per_pixel_%.5f.jpg", ts, skala)
                        val akhir = File(dir, nama)
                        tmp.renameTo(akhir)

                        val meta = String.format(Locale.US,
                            "skala_mm_per_pixel=%.6f;jarak_mm=%.1f;focal_px=%.2f;resolusi=%dx%d;sumber_f=%s",
                            skala, jarakMm, f.fx, b.outWidth, b.outHeight, f.sumber)
                        ExifInterface(akhir.path).apply {
                            setAttribute(ExifInterface.TAG_USER_COMMENT, meta)
                            setAttribute(ExifInterface.TAG_IMAGE_DESCRIPTION, meta)
                            saveAttributes()
                        }
                        onSelesai(akhir)
                    } catch (e: Exception) { onError(e) }
                }
                override fun onError(e: ImageCaptureException) { onError(e) }
            })
    }
}

// -------------------------------------------------------------------------------------
// 7. ALUR UTAMA (ringkas) - tempel di Activity
// -------------------------------------------------------------------------------------
/*
enum class Fase { UKUR, AMBIL }

class MainActivity : AppCompatActivity() {
    private lateinit var session: Session
    private lateinit var focusBox: FocusBoxView
    private lateinit var previewView: PreviewView          // CameraX (tampil saat fase AMBIL)
    private lateinit var glView: GLSurfaceView             // ARCore (tampil saat fase UKUR)
    private val gate = FocusGate()
    private lateinit var intrinsics: IntrinsicsProvider
    private lateinit var capturer: LesionCapturer
    private var fase = Fase.UKUR
    private var jarakTerakhir = 0f

    // Dipanggil dari GLSurfaceView.Renderer.onDrawFrame (setelah session.update()).
    // Gambar background kamera dengan BackgroundRenderer dari sample hello_ar_kotlin.
    fun onArFrame(frame: Frame) {
        if (fase != Fase.UKUR) return
        val ukur = jarakDalamLingkaranMm(frame)                  // median depth di dalam lingkaran
        val jarak = ukur?.medianMm
        val camId = session.cameraConfig.cameraId
        val fx = intrinsics.focalPx(camId)?.fx                   // fx sensor penuh, untuk tampilan
        val hasil = gate.update(ukur)

        runOnUiThread {
            focusBox.sesuai = hasil.sesuai                       // HIJAU / MERAH
            focusBox.info = if (jarak != null && fx != null)
                "%.0f mm | %.4f mm/px".format(Locale.US, jarak, hitungSkala(jarak, fx)) else "mengukur..."
        }
        if (hasil.siapAmbil && jarak != null) {                  // MERAH => tidak pernah sampai sini (terkunci)
            fase = Fase.AMBIL; jarakTerakhir = jarak; gate.reset()
            runOnUiThread { mulaiAmbil(camId) }
        }
    }

    private fun mulaiAmbil(arCamId: String) {
        session.pause()                                          // lepaskan kamera dari ARCore
        glView.visibility = View.GONE; previewView.visibility = View.VISIBLE
        capturer.bind(this, previewView, arCamId) { camId ->
            capturer.ambilFoto(camId, jarakTerakhir,
                onSelesai = { file -> Toast.makeText(this, file.name, Toast.LENGTH_LONG).show(); kembaliUkur() },
                onError   = { e -> Log.e("Lesi", "gagal", e); kembaliUkur() })
        }
    }

    private fun kembaliUkur() {
        ProcessCameraProvider.getInstance(this).get().unbindAll()
        previewView.visibility = View.GONE; glView.visibility = View.VISIBLE
        session.resume(); fase = Fase.UKUR
    }
}
*/
