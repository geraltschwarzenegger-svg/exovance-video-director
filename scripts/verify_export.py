#!/usr/bin/env python3
"""Verify an MP4 against its delivery settings and decode it to the end."""
import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess


def run(command, *, fail_on_stderr=False):
    result = subprocess.run(command, capture_output=True)
    if result.returncode or fail_on_stderr and result.stderr:
        message = result.stderr.decode(errors='replace').strip()[-5000:]
        raise RuntimeError(message or f'{command[0]} exited with code {result.returncode}.')
    return result.stdout


def probe(path, *, strict=False):
    return json.loads(run([
        'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json',
        str(path),
    ], fail_on_stderr=strict))


def video_streams(metadata):
    return [
        stream for stream in metadata['streams']
        if stream['codec_type'] == 'video' and not stream.get('disposition', {}).get('attached_pic')
    ]


def rotation_degrees(video):
    return next(
        (entry['rotation'] for entry in video.get('side_data_list', []) if 'rotation' in entry),
        0,
    )


def frame_rate(value):
    try:
        rate = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def duration_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def verify_export(path, *, size, fps, duration, audio_required, video_duration=None,
                  video_codec='h264', pixel_format='yuv420p', audio_codec='aac'):
    metadata = probe(path, strict=True)
    container = metadata['format']
    brand = container.get('tags', {}).get('major_brand', '').strip().lower()
    if ('mp4' not in container['format_name'].split(',') or not brand
            or brand == 'qt' or brand.startswith(('3gp', '3g2', 'mj2'))):
        raise RuntimeError('Expected an MP4 container.')

    videos = video_streams(metadata)
    if len(videos) != 1:
        raise RuntimeError(f'Expected one video stream, found {len(videos)}.')
    video = videos[0]
    rotation = rotation_degrees(video)
    dimensions = (video['width'], video['height'])
    if rotation % 180 == 90:
        dimensions = dimensions[::-1]
    actual_fps = frame_rate(video.get('avg_frame_rate'))
    actual_duration = duration_seconds(container.get('duration'))
    actual_video_duration = duration_seconds(video.get('duration'))
    audio_codecs = [
        stream.get('codec_name') for stream in metadata['streams']
        if stream['codec_type'] == 'audio'
    ]

    errors = []
    if dimensions != size:
        errors.append(f'Resolution: expected {size[0]}x{size[1]}, got {dimensions[0]}x{dimensions[1]}.')
    if actual_fps is None:
        errors.append('Video frame rate is unavailable.')
    elif fps is not None and abs(actual_fps - fps) > Fraction(1, 100):
        errors.append(f'FPS: expected {fps}, got {actual_fps}.')
    if video.get('codec_name') != video_codec:
        errors.append(f"Video codec: expected {video_codec}, got {video.get('codec_name')}.")
    if video.get('pix_fmt') != pixel_format:
        errors.append(f"Pixel format: expected {pixel_format}, got {video.get('pix_fmt')}.")
    if audio_required and not audio_codecs:
        errors.append('Expected audio, but no audio stream was found.')
    elif not audio_required and audio_codecs:
        errors.append('Expected a silent export without an audio stream.')
    if any(codec != audio_codec for codec in audio_codecs):
        errors.append(f'Audio codec: expected {audio_codec}, got {audio_codecs}.')

    # Frame-rate conversion can round the end to a frame boundary. Allow for
    # an AAC frame (about 23 ms at 44.1 kHz) and MP4 timestamp rounding too.
    tolerance = max(0.05, 1 / float(fps or actual_fps or 60) + 0.025)
    for label, actual, expected in (
        ('MP4 duration', actual_duration, duration),
        ('Video duration', actual_video_duration, video_duration),
    ):
        if actual is None:
            errors.append(f'{label} is unavailable.')
        elif expected is not None and abs(actual - expected) > tolerance:
            errors.append(f'{label}: expected {expected:.3f}s, got {actual:.3f}s.')
    if errors:
        raise RuntimeError('\n'.join(errors))

    try:
        run([
            'ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-err_detect', 'explode',
            '-i', str(path), '-map', '0:V', '-map', '0:a?', '-abort_on',
            'empty_output_stream', '-f', 'null', '-',
        ], fail_on_stderr=True)
    except RuntimeError as error:
        raise RuntimeError(f'Decoding failed: {error}') from error

    return {
        'size': list(dimensions),
        'rotation_degrees': rotation,
        'fps': str(actual_fps),
        'video_codec': video['codec_name'],
        'pixel_format': video['pix_fmt'],
        'duration_seconds': actual_duration,
        'video_duration_seconds': actual_video_duration,
        'audio_codecs': audio_codecs,
        'decoded': True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--size', type=int, nargs=2, metavar=('WIDTH', 'HEIGHT'), required=True)
    parser.add_argument('--fps', required=True,
                        help='Expected average FPS, e.g. 60 or 24000/1001. Use variable to report the average without a fixed target.')
    parser.add_argument('--duration', type=float, required=True, help='Expected MP4 duration in seconds.')
    parser.add_argument('--video-duration', type=float,
                        help='Expected video stream duration when it differs from --duration.')
    parser.add_argument('--audio', choices=['required', 'absent'], required=True)
    parser.add_argument('--video-codec', default='h264')
    parser.add_argument('--pixel-format', default='yuv420p')
    parser.add_argument('--audio-codec', default='aac')
    args = parser.parse_args()
    path = args.file.resolve()
    if not path.is_file() or path.suffix.lower() != '.mp4':
        parser.error('Specify an existing .mp4 file.')
    fps = frame_rate(args.fps)
    if fps is None and args.fps != 'variable':
        parser.error('--fps must be a positive number, a positive fraction or variable.')
    video_duration = args.duration if args.video_duration is None else args.video_duration
    if min(args.size) <= 0 or any(
        duration_seconds(value) is None for value in (args.duration, video_duration)
    ):
        parser.error('Size and durations must be positive and finite.')
    try:
        report = verify_export(
            path, size=tuple(args.size), fps=fps, duration=args.duration,
            audio_required=args.audio == 'required', video_duration=video_duration,
            video_codec=args.video_codec, pixel_format=args.pixel_format, audio_codec=args.audio_codec,
        )
    except (RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f'MP4 verification failed: {error}\n')
    print(json.dumps({'file': str(path), **report}, ensure_ascii=False))


if __name__ == '__main__':
    main()
