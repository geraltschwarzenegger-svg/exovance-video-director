"""Exercise the CLI on generated media without committing video fixtures."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

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


def bottom_peak(path, timestamp):
    frame = run(
        "ffmpeg", "-v", "error", "-ss", timestamp, "-i", path,
        "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "-",
    )
    with Image.open(io.BytesIO(frame)) as image:
        region = image.crop((WIDTH // 3, HEIGHT - 65, 2 * WIDTH // 3, HEIGHT))
        return region.convert("L").getextrema()[1]


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
            run_script(
                "watermark_export.py", source, output, "--start", "1",
                "--fade", "0.8", "--position", "bottom-center", "--font", FONT,
                "--opacity", "0.35", "--fps", "60",
            )

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


if __name__ == "__main__":
    unittest.main()
