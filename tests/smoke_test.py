"""Exercise the CLI on generated media without committing video fixtures."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
WIDTH, HEIGHT = 640, 360


def run(*args):
    result = subprocess.run(
        [str(arg) for arg in args], capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result.stdout


def run_script(script, *args):
    return run(sys.executable, ROOT / "scripts" / script, *args)


def frame_luminance(path, timestamp):
    frame = run(
        "ffmpeg", "-v", "error", "-ss", timestamp, "-i", path,
        "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "-",
    )
    with Image.open(io.BytesIO(frame)) as image:
        return image.convert("L")


def bottom_peak(path, timestamp):
    frame = frame_luminance(path, timestamp)
    return frame.crop((WIDTH // 3, HEIGHT - 65, 2 * WIDTH // 3, HEIGHT)).getextrema()[1]


def rgb_frame(path, timestamp):
    return run(
        "ffmpeg", "-v", "error", "-ss", timestamp, "-i", path, "-frames:v", "1",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    )


class VideoSmokeTest(unittest.TestCase):
    def test_audit_and_watermark_export(self):
        with TemporaryDirectory(prefix="exovance-smoke-") as directory:
            work = Path(directory)
            source = work / "source.mp4"
            output = work / "export.mp4"
            audit_dir = work / "audit"

            run(
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", f"color=black:size={WIDTH}x{HEIGHT}:rate=24:d=3",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=3",
                "-vf", f"drawtext=fontfile={FONT}:text=ZAPPER:fontsize=48:"
                "fontcolor=white:x=28:y=34",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", source,
            )
            source_digest = hashlib.sha256(source.read_bytes()).digest()

            run_script(
                "media_audit.py", source, "--out-dir", audit_dir,
                "--frames", "3", "--ocr", "--audio-onsets",
            )
            audit = json.loads((audit_dir / "audit.json").read_text())
            self.assertEqual(len(audit["ocr_samples"]), 3)
            self.assertIn("music_onsets", audit)
            self.assertTrue((audit_dir / "contact.jpg").is_file())
            for sample in audit["ocr_samples"]:
                self.assertEqual(sample["frame_width"], WIDTH)
                words = {word["text"]: word["box"] for word in sample["words"]}
                self.assertIn("ZAPPER", words)
                self.assertLess(words["ZAPPER"]["left"], 50)
                self.assertLess(words["ZAPPER"]["top"], 65)

            detail_dir = work / "detail"
            run_script(
                "media_audit.py", source, "--out-dir", detail_dir, "--frames", "3",
                "--frames-from", "0.8", "--frames-to", "1.2", "--ocr",
            )
            detail = json.loads((detail_dir / "audit.json").read_text())
            for sample, expected in zip(detail["ocr_samples"], (0.8, 1.0, 1.2), strict=True):
                self.assertAlmostEqual(sample["sample_time_seconds"], expected)
                self.assertIn("ZAPPER", {word["text"] for word in sample["words"]})

            export = json.loads(run_script(
                "watermark_export.py", source, output, "--start", "1",
                "--fade", "0.8", "--position", "bottom-center", "--font", FONT,
                "--opacity", "0.35", "--fps", "60", "--resolution", "source",
            ))
            self.assertEqual(export["verification"], {
                "size": [WIDTH, HEIGHT],
                "rotation_degrees": 0,
                "fps": "60",
                "video_codec": "h264",
                "pixel_format": "yuv420p",
                "duration_seconds": 3,
                "video_duration_seconds": 3,
                "audio_codecs": ["aac"],
                "decoded": True,
            })

            meta = json.loads(run(
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", output,
            ))
            streams = {stream["codec_type"]: stream for stream in meta["streams"]}
            self.assertEqual(streams["video"]["codec_name"], "h264")
            self.assertEqual(streams["video"]["avg_frame_rate"], "60/1")
            self.assertEqual(
                (streams["video"]["width"], streams["video"]["height"]), (WIDTH, HEIGHT)
            )
            self.assertEqual(streams["audio"]["codec_name"], "aac")
            self.assertAlmostEqual(float(meta["format"]["duration"]), 3, delta=0.1)
            before, halfway, after = (
                bottom_peak(output, time) for time in ("0.5", "1.4", "2.2")
            )
            self.assertLessEqual(before, 3)
            self.assertLess(before + 10, halfway)
            self.assertLess(halfway + 10, after)
            run(
                "ffmpeg", "-v", "error", "-xerror", "-i", output,
                "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
            )
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), source_digest)

    def test_soft_glint_and_metadata_rotation(self):
        with TemporaryDirectory(prefix="exovance-portrait-") as directory:
            work = Path(directory)
            source = work / "landscape.mp4"
            portrait = work / "rotated.mp4"
            output = work / "effect.mp4"
            run(
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                f"color=black:size={WIDTH}x{HEIGHT}:rate=24:d=2.5",
                "-c:v", "libx264", source,
            )
            run(
                "ffmpeg", "-v", "error", "-display_rotation:v:0", "90",
                "-i", source, "-c", "copy", portrait,
            )
            render = json.loads(run_script(
                "watermark_export.py", portrait, output, "--start", "0.5",
                "--fade", "0.8", "--position", "bottom-center", "--font", FONT,
                "--font-size", "24", "--effect", "soft-glint", "--crop-bottom", "20",
                "--resolution", "source",
            ))
            self.assertEqual(render["output_size"], [HEIGHT, WIDTH])
            self.assertEqual(render["verification"]["size"], [HEIGHT, WIDTH])
            self.assertEqual(render["verification"]["audio_codecs"], [])
            self.assertTrue(render["verification"]["decoded"])
            before, rising, glint, settled = (
                frame_luminance(output, time) for time in ("0.3", "0.82", "1.2", "2")
            )
            self.assertEqual(settled.size, (HEIGHT, WIDTH))
            self.assertLessEqual(before.getextrema()[1], 3)
            self.assertGreater(glint.getextrema()[1], settled.getextrema()[1] + 8)
            rising_box = rising.point(lambda pixel: 255 if pixel > 15 else 0).getbbox()
            settled_box = settled.point(lambda pixel: 255 if pixel > 15 else 0).getbbox()
            self.assertIsNotNone(rising_box)
            self.assertIsNotNone(settled_box)
            self.assertGreater(rising_box[1], settled_box[1])
            self.assertGreater(settled_box[1], WIDTH - 65)

    def test_verify_export_rejects_mismatches_and_truncated_media(self):
        with TemporaryDirectory(prefix="exovance-verify-") as directory:
            work = Path(directory)
            valid = work / "valid.mp4"
            run(
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=24:d=2",
                "-c:v", "libx264", "-g", "12", "-movflags", "+faststart", valid,
            )
            expected = ("--size", "128", "72", "--fps", "24", "--duration", "2", "--audio", "absent")
            report = json.loads(run_script("verify_export.py", valid, *expected))
            self.assertEqual(report["fps"], "24")
            self.assertTrue(report["decoded"])

            def verify_failure(file, *options):
                result = subprocess.run(
                    [sys.executable, ROOT / "scripts" / "verify_export.py", file, *options],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                return result.stderr

            errors = verify_failure(
                valid, "--size", "1920", "1080", "--fps", "60", "--duration", "3",
                "--audio", "required", "--video-codec", "hevc",
            )
            for message in ("Resolution:", "FPS:", "Video codec:", "MP4 duration:", "Expected audio"):
                self.assertIn(message, errors)

            truncated = work / "truncated.mp4"
            data = valid.read_bytes()
            truncated.write_bytes(data[:len(data) * 3 // 4])
            # Faststart keeps the declared metadata readable even though late packets are missing.
            self.assertEqual(run(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", truncated,
            ).strip(), b"2.000000")
            self.assertIn("Decoding failed:", verify_failure(truncated, *expected))

    def test_text_replacement_preserves_clean_details_and_aac(self):
        with TemporaryDirectory(prefix="exovance-text-") as directory:
            work = Path(directory)
            plate, marked = work / "clean.png", work / "marked.png"
            title, mask = work / "title.png", work / "mask.png"
            source, master, delivery = work / "source.mkv", work / "master.mkv", work / "delivery.mp4"
            width, height = 192, 108
            rng = np.random.default_rng(42)
            background = rng.integers(16, 200, (height, width, 3), dtype=np.uint8)
            Image.fromarray(background).save(plate)
            damaged = background.copy()
            damaged[35:57, 55:59] = 255
            damaged[48:52, 48:69] = 255
            Image.fromarray(damaged).save(marked)
            run(
                "ffmpeg", "-v", "error", "-loop", "1", "-framerate", "12", "-i", marked,
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=1",
                "-t", "1", "-c:v", "ffv1", "-pix_fmt", "bgr0", "-c:a", "aac", source,
            )
            run_script(
                "replace_text.py", "mask", marked, plate, mask, "--roi", "35", "20", "90", "60",
                "--threshold", "1", "--expand", "0",
            )
            with Image.open(mask) as image:
                black_and_white = np.asarray(image)
            np.testing.assert_array_equal(black_and_white == 255, np.any(damaged != background, axis=2))
            run_script(
                "replace_text.py", "title", title, "--size", width, height,
                "--text", "НОВЫЙ", "--font", FONT, "--font-size", "22", "--position", "46", "35",
            )
            options = ("--clean-plate", plate, "--mask", mask, "--start", "0.15", "--end", "0.85")
            preview = work / "restored.png"
            run_script("replace_text.py", "render", source, preview, *options, "--preview-at", "0.5")
            with Image.open(preview) as image:
                np.testing.assert_array_equal(np.array(image), background)
            run_script("replace_text.py", "render", source, master, *options, "--title-overlay", title)
            outside = rgb_frame(master, "0.0")
            self.assertEqual(outside, damaged.tobytes())
            actual = np.frombuffer(rgb_frame(master, "0.5"), dtype=np.uint8).reshape((height, width, 3))
            with Image.open(title) as image:
                alpha = np.asarray(image.getchannel("A"))
            np.testing.assert_array_equal(actual[alpha == 0], background[alpha == 0])
            self.assertGreater(np.count_nonzero(actual[alpha == 255] != background[alpha == 255]), 10)
            report = json.loads(run_script(
                "replace_text.py", "render", source, delivery, *options, "--title-overlay", title,
            ))
            self.assertEqual(report["source_size"], [width, height])
            self.assertEqual(report["verification"]["size"], [1920, 1080])
            self.assertEqual(report["verification"]["fps"], "12")
            self.assertTrue(report["verification"]["decoded"])
            def audio_hash(path):
                return run("ffmpeg", "-v", "error", "-i", path, "-map", "0:a", "-c", "copy", "-f", "hash", "-")
            self.assertEqual(audio_hash(source), audio_hash(delivery))

    def test_text_replacement_synchronizes_moving_layers(self):
        with TemporaryDirectory(prefix="exovance-moving-text-") as directory:
            work = Path(directory)
            width, height = 96, 54
            frames = []
            for index in range(4):
                clean = np.full((height, width, 3), (16 * index, 100, 163), dtype=np.uint8)
                clean[8:24, 10 + index:29 + index] = (182, 136, 58)
                marked = clean.copy()
                marked[12:22, 14 + index:17 + index] = 255
                mask = np.any(marked != clean, axis=2).astype(np.uint8) * 255
                title = np.zeros((height, width, 4), dtype=np.uint8)
                title[28:34, 30 + index:45 + index] = (13, 61, 242, 255)
                for label, frame in (("plate", clean), ("source", marked), ("mask", mask), ("title", title)):
                    Image.fromarray(frame).save(work / f"{label}{index}.png")
                combined = clean.copy()
                combined[28:34, 30 + index:45 + index] = (13, 61, 242)
                frames.append(combined)
            for label, pix_fmt in (("plate", "bgr0"), ("source", "bgr0"), ("mask", "gray"), ("title", "bgra")):
                run("ffmpeg", "-v", "error", "-framerate", "4", "-i", work / f"{label}%d.png",
                    "-c:v", "ffv1", "-pix_fmt", pix_fmt, work / f"{label}.mkv")
            output = work / "restored.mkv"
            run_script(
                "replace_text.py", "render", work / "source.mkv", output,
                "--clean-plate", work / "plate.mkv", "--mask", work / "mask.mkv",
                "--title-overlay", work / "title.mkv",
            )
            for index, frame in enumerate(frames):
                self.assertEqual(rgb_frame(output, str(index / 4)), frame.tobytes())

            rejected = subprocess.run(
                [sys.executable, ROOT / "scripts" / "replace_text.py", "render", work / "source.mkv",
                 work / "rejected.mkv", "--clean-plate", work / "plate.mkv", "--mask", work / "mask.mkv",
                 "--title-overlay", work / "plate.mkv"], capture_output=True, check=False,
            )
            self.assertIn(b"needs a video stream with alpha", rejected.stderr)
            self.assertFalse((work / "rejected.mkv").exists())

    def test_mp4_resolution_defaults_to_full_hd_and_supports_4k(self):
        with TemporaryDirectory(prefix="exovance-resolution-") as directory:
            work = Path(directory)
            source = work / "source.mp4"
            run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=1280x720:rate=30:d=0.2",
                "-c:v", "libx264", source)
            portrait = work / "portrait.mp4"
            run("ffmpeg", "-v", "error", "-display_rotation:v:0", "90",
                "-i", source, "-c", "copy", portrait)
            for name, video, options, size in (
                ("landscape", source, (), [1920, 1080]),
                ("portrait", portrait, (), [1080, 1920]),
                ("uhd", source, ("--resolution", "4k"), [3840, 2160]),
            ):
                with self.subTest(name=name):
                    report = json.loads(run_script(
                        "watermark_export.py", video, work / f"export-{name}.mp4", "--start", "0",
                        "--position", "bottom-center", "--font", FONT, *options,
                    ))
                    self.assertEqual(report["verification"]["size"], size)
                    self.assertEqual(report["verification"]["fps"], "30")


if __name__ == "__main__":
    unittest.main()
