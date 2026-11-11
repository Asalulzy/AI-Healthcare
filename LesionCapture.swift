// =====================================================================================
// LesionCapture.swift - iOS: ARKit (LiDAR sceneDepth) + intrinsik kamera
//
// Pada iOS satu ARSession bisa memberi preview + depth + intrinsik sekaligus, dan
// captureHighResolutionFrame (iOS 16+) mengambil foto resolusi tinggi dari sesi yang sama,
// jadi tidak ada "handover" kamera seperti CameraX <-> ARCore di Android.
//
// Syarat: iPhone/iPad ber-LiDAR (Pro 12+). Tanpa LiDAR, depth tidak tersedia -> pakai
// penanda referensi fisik (lihat catatan). Uji kompatibilitas sceneDepth dengan videoFormat
// high-res di perangkat targetmu; bila tidak cocok, ambil foto lewat AVCapturePhotoOutput.
// =====================================================================================
import ARKit
import ImageIO
import UniformTypeIdentifiers

final class LesionCaptureController: NSObject, ARSessionDelegate {
    static let targetJarakMM: Float = 100
    static let toleransiMM: Float = 5
    static let frameStabil = 5
    static let maksSebaranMM: Float = 10

    let session = ARSession()
    /// (sesuai, jarakMM, skalaMMperPx) -> update FocusBox (merah/hijau) di UI
    var onState: ((Bool, Float?, Float?) -> Void)?
    var onSaved: ((URL) -> Void)?

    private var stabil = 0
    private var sedangAmbil = false

    func start() {
        guard ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) else {
            onState?(false, nil, nil); return        // tidak ada LiDAR
        }
        let cfg = ARWorldTrackingConfiguration()
        cfg.frameSemantics = .sceneDepth
        if let fmt = ARWorldTrackingConfiguration.recommendedVideoFormatForHighResolutionFrameCapturing {
            cfg.videoFormat = fmt
        }
        session.delegate = self
        session.run(cfg)
    }

    // MARK: real-time
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard let depth = frame.sceneDepth?.depthMap else { return }
        guard let ukur = Self.depthDalamLingkaranMM(depth) else {     // data valid di lingkaran terlalu sedikit
            stabil = 0
            DispatchQueue.main.async { self.onState?(false, nil, nil) }
            return
        }
        let jarak = ukur.median                                       // mm (median seluruh area lingkaran)
        let fx = frame.camera.intrinsics.columns.0.x                  // px @ frame.camera.imageResolution
        let skala = jarak / fx                                        // skala = jarak / focal_px
        let sesuai = abs(jarak - Self.targetJarakMM) <= Self.toleransiMM
                     && ukur.sebaran <= Self.maksSebaranMM            // permukaan harus cukup rata

        stabil = sesuai ? stabil + 1 : 0
        DispatchQueue.main.async { self.onState?(sesuai, jarak, skala) }

        // MERAH (sesuai == false) -> stabil = 0 -> tidak pernah memicu capture (terkunci)
        if stabil >= Self.frameStabil && !sedangAmbil {
            sedangAmbil = true
            ambil(jarakMM: jarak)
        }
    }

    /// Median depth semua piksel valid DI DALAM lingkaran (radius = radiusFrac x sisi terpendek,
    /// sama dengan radius Focus Box). sebaran = p90 - p10 (besar bila permukaan miring/ada tepi).
    private static func depthDalamLingkaranMM(_ pb: CVPixelBuffer, radiusFrac: Float = 0.18)
        -> (median: Float, sebaran: Float, valid: Float)? {
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        let bpr = CVPixelBufferGetBytesPerRow(pb)
        let base = CVPixelBufferGetBaseAddress(pb)!
        let cx = w / 2, cy = h / 2
        let r = max(2, Int(Float(min(w, h)) * radiusFrac))

        var v: [Float] = []
        var total = 0
        for dy in -r...r {
            let row = base.advanced(by: (cy + dy) * bpr).assumingMemoryBound(to: Float32.self)
            for dx in -r...r where dx * dx + dy * dy <= r * r {
                total += 1
                let mm = row[cx + dx] * 1000                          // meter -> mm
                if mm.isFinite && mm > 0 { v.append(mm) }
            }
        }
        guard total > 0, Float(v.count) >= 0.4 * Float(total) else { return nil }
        v.sort()
        let p10 = v[Int(Float(v.count) * 0.1)]
        let p90 = v[min(v.count - 1, Int(Float(v.count) * 0.9))]
        return (v[v.count / 2], p90 - p10, Float(v.count) / Float(total))
    }

    // MARK: capture resolusi penuh (bukan crop dari preview)
    private func ambil(jarakMM: Float) {
        session.captureHighResolutionFrame { [weak self] frame, error in
            guard let self, let frame else { self?.sedangAmbil = false; return }
            let fx = frame.camera.intrinsics.columns.0.x              // fx pada resolusi foto tsb
            let skala = jarakMM / fx
            self.simpan(frame.capturedImage, skala: skala, jarak: jarakMM, fx: fx,
                        res: frame.camera.imageResolution)
            self.stabil = 0; self.sedangAmbil = false
        }
    }

    private func simpan(_ pb: CVPixelBuffer, skala: Float, jarak: Float, fx: Float, res: CGSize) {
        let ci = CIImage(cvPixelBuffer: pb)
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
              let jpeg = CIContext().jpegRepresentation(of: ci, colorSpace: cs, options: [
                  CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.98
              ]),
              let src = CGImageSourceCreateWithData(jpeg as CFData, nil) else { return }

        let teks = String(format: "skala_mm_per_pixel=%.6f;jarak_mm=%.1f;focal_px=%.2f;resolusi=%dx%d;platform=iOS",
                          skala, jarak, fx, Int(res.width), Int(res.height))
        let props: [CFString: Any] = [
            kCGImagePropertyExifDictionary: [kCGImagePropertyExifUserComment: teks],
            kCGImagePropertyTIFFDictionary: [kCGImagePropertyTIFFImageDescription: teks],
        ]
        let out = NSMutableData()
        guard let dst = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImageFromSource(dst, src, 0, props as CFDictionary)
        guard CGImageDestinationFinalize(dst) else { return }

        let f = DateFormatter(); f.dateFormat = "yyyyMMdd_HHmmss"; f.locale = Locale(identifier: "en_US_POSIX")
        let nama = String(format: "lesi_%@_skala_mm_per_pixel_%.5f.jpg", f.string(from: Date()), skala)
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent(nama)
        try? (out as Data).write(to: url)
        DispatchQueue.main.async { self.onSaved?(url) }
    }
}
