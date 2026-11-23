#!/usr/bin/env python3
"""Restore a masked title from a clean plate, then optionally composite a new title."""
import argparse
import json
import math
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

from verify_export import duration_seconds, frame_rate, probe, rotation_degrees, run, verify_export, video_streams


def require_file(path):
    if not path.is_file():
        raise ValueError(f'File not found: {path}')
    return path.resolve()


def distinct_output(path, inputs, overwrite):
    output = path.resolve()
    if output in inputs:
        raise ValueError('Use a separate output path.')
    if output.exists() and not overwrite:
        raise ValueError('Output exists. Choose a new version or pass --overwrite.')
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def finish(staged, output, overwrite):
    if not staged.is_file() or not staged.stat().st_size:
        raise RuntimeError('No output was rendered. Check the preview time or input timeline.')
    if overwrite:
        staged.replace(output)
    else:
        # Publish atomically without replacing a file another render may have created.
        output.hardlink_to(staged)


def png_pixels(path, mode):
    with Image.open(path) as image:
        if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1:
            raise ValueError(f'Expected a single PNG frame: {path.name}.')
        if image.mode.startswith('I') or image.mode == 'F':
            raise ValueError(f'Expected an 8-bit PNG: {path.name}.')
        return image.convert(mode)


def make_title(args):
    font_path = require_file(args.font)
    output = distinct_output(args.output, (font_path,), args.overwrite)
    if output.suffix.lower() != '.png':
        raise ValueError('Save the title as a transparent PNG.')
    if min(args.size) <= 0 or args.font_size <= 0 or args.stroke < 0 or not args.text.strip():
        raise ValueError('Provide a positive frame and font size, a nonnegative stroke and visible text.')
    image = Image.new('RGBA', tuple(args.size))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), args.font_size)
    fill, outline = [ImageColor.getcolor(value, 'RGBA') for value in (args.color, args.stroke_color)]
    style = {'font': font, 'stroke_width': args.stroke}
    left, top, right, bottom = draw.multiline_textbbox((0, 0), args.text, **style)
    x, y = args.position
    if min(x, y) < 0 or x + right - left > image.width or y + bottom - top > image.height:
        raise ValueError('The title does not fit the frame at this position.')
    draw.multiline_text((x - left, y - top), args.text, fill=fill, stroke_fill=outline, **style)
    if image.getbbox() is None:
        raise ValueError('The rendered title is fully transparent.')
    with tempfile.TemporaryDirectory(prefix='.exovance-title-', dir=output.parent) as temp:
        staged = Path(temp) / output.name
        image.save(staged)
        finish(staged, output, args.overwrite)
    return {'output': str(output), 'size': list(image.size), 'position': list(args.position)}


def make_mask(args):
    source, plate = (require_file(path) for path in (args.frame, args.clean_plate))
    output = distinct_output(args.output, (source, plate), args.overwrite)
    if output.suffix.lower() != '.png':
        raise ValueError('Save the mask as a PNG.')
    marked, clean = png_pixels(source, 'RGB'), png_pixels(plate, 'RGB')
    if marked.size != clean.size:
        raise ValueError('The source frame and clean frame must have the same size.')
    left, top, width, height = args.roi
    right, bottom = left + width, top + height
    if min(left, top) < 0 or min(width, height) <= 0 or right > marked.width or bottom > marked.height:
        raise ValueError('--roi must be a positive rectangle inside the frame.')
    if not 1 <= args.threshold <= 255 or not 0 <= args.expand <= 16:
        raise ValueError('Use a threshold from 1 to 255 and expansion from 0 to 16 pixels.')
    box = (left, top, right, bottom)
    reference = np.asarray(clean.crop(box), dtype=np.int16)
    marked_pixels = np.asarray(marked.crop(box), dtype=np.int16)
    difference = np.max(np.abs(marked_pixels - reference), axis=2)
    selected = Image.fromarray((difference >= args.threshold).astype(np.uint8) * 255)
    if args.expand:
        selected = selected.filter(ImageFilter.MaxFilter(2 * args.expand + 1))
    fraction = selected.histogram()[255] / (width * height)
    if not 0 < fraction < 1:
        raise ValueError('The ROI selected nothing or everything. Check alignment, threshold and expansion.')
    mask = Image.new('L', marked.size)
    mask.paste(selected, box)
    with tempfile.TemporaryDirectory(prefix='.exovance-mask-', dir=output.parent) as temp:
        staged = Path(temp) / output.name
        mask.save(staged)
        finish(staged, output, args.overwrite)
    return {'output': str(output), 'size': list(mask.size), 'selected_roi_fraction': round(fraction, 4)}


def stream_size(video):
    width, height = video['width'], video['height']
    rotation = rotation_degrees(video) % 360
    if rotation not in (0, 90, 180, 270):
        raise ValueError('Apply rotation to the source before preparing aligned layers.')
    return (height, width) if rotation in (90, 270) else (width, height)


def choose_stream(metadata):
    videos = video_streams(metadata)
    if len(videos) != 1:
        raise ValueError('Use one video stream per input.')
    return videos[0]


def check_sdr(video):
    if video.get('color_transfer') in ('smpte2084', 'arib-std-b67') or video.get('color_primaries') == 'bt2020':
        raise ValueError('Use an HDR-aware compositor for HDR or BT.2020 sources.')
    if video.get('pix_fmt') not in {
        'yuv420p', 'yuvj420p', 'nv12', 'yuv422p', 'yuvj422p', 'yuv444p', 'yuvj444p',
        'gbrp', 'gbrap', 'rgb24', 'rgba', 'argb', 'abgr', 'bgr24', 'bgr0', 'bgra', 'gray',
        'yuva420p', 'yuva422p', 'yuva444p',
    }:
        raise ValueError('The RGB compositor expects standard 8-bit SDR footage.')


def stream_timing(metadata, video):
    container = metadata['format']
    video_length = duration_seconds(video.get('duration'))
    total_length = duration_seconds(container.get('duration'))
    rate = frame_rate(video.get('avg_frame_rate'))
    return video_length or total_length, total_length, rate


def inspect_layer(path, kind, size, source_video, duration, rate):
    if path.suffix.lower() == '.png':
        with Image.open(path) as image:
            if kind == 'title' and 'A' not in image.getbands() and 'transparency' not in image.info:
                raise ValueError('The replacement title needs a transparent PNG canvas.')
        mode = {'title': 'RGBA', 'mask': 'L', 'plate': 'RGB'}[kind]
        image = png_pixels(path, mode)
        if image.size != size:
            raise ValueError(f'The {kind} dimensions must match the displayed source.')
        if kind == 'mask' and image.getextrema() != (0, 255):
            raise ValueError('The mask must include protected black and fully replaced white pixels.')
        if kind == 'title' and image.getchannel('A').getextrema() == (0, 0):
            raise ValueError('The replacement title PNG is fully transparent.')
        return 0
    metadata = probe(path, strict=True)
    stream = choose_stream(metadata)
    check_sdr(stream)
    if kind == 'title' and stream['pix_fmt'] not in {
        'rgba', 'argb', 'abgr', 'bgra', 'gbrap', 'yuva420p', 'yuva422p', 'yuva444p',
    }:
        raise ValueError('A moving replacement title needs a video stream with alpha.')
    if kind == 'mask' and stream['codec_name'] not in {'ffv1', 'rawvideo', 'png', 'qtrle'}:
        raise ValueError('Use a lossless video codec for a moving mask so its black and white values survive.')
    layer_length, _, layer_rate = stream_timing(metadata, stream)
    if stream_size(stream) != size:
        raise ValueError(f'The {kind} dimensions must match the displayed source.')
    source_cfr = rate is not None and frame_rate(source_video.get('r_frame_rate')) == rate
    layer_cfr = layer_rate is not None and frame_rate(stream.get('r_frame_rate')) == layer_rate
    if not source_cfr or not layer_cfr or layer_rate != rate:
        raise ValueError(f'Moving {kind} requires the same constant FPS as the source.')
    if layer_length is None or abs(layer_length - duration) > 0.5 / float(rate):
        raise ValueError(f'Moving {kind} must span the same timeline as the source.')
    return stream['index']


def filter_graph(indices, start, end):
    source, plate, mask, *title = indices
    timing = f"enable='gte(t,{start})*lt(t,{end})'"
    filters = [
        f'[0:{source}]format=gbrp[base]',
        f'[1:{plate}]setpts=PTS-STARTPTS,format=gbrp[plate]',
        f'[2:{mask}]setpts=PTS-STARTPTS,format=gray,format=gbrp[mask]',
        f'[base][plate][mask]maskedmerge={timing}[restored]',
    ]
    if title:
        filters += [
            f'[3:{title[0]}]setpts=PTS-STARTPTS,format=rgba[title]',
            f'[restored][title]overlay=format=gbrp:{timing}[edited]',
        ]
        return ';'.join(filters), '[edited]'
    return ';'.join(filters), '[restored]'


def render(args):
    layers = [require_file(p) for p in (args.source, args.clean_plate, args.mask)]
    if args.title_overlay:
        layers.append(require_file(args.title_overlay))
    output = distinct_output(args.output, layers, args.overwrite)
    suffix = output.suffix.lower()
    valid_suffixes = ('.png',) if args.preview_at is not None else ('.mp4', '.mkv')
    if suffix not in valid_suffixes:
        raise ValueError('Use PNG for a preview, MKV for a lossless RGB master or MP4 for delivery.')
    metadata = probe(layers[0], strict=True)
    video = choose_stream(metadata)
    check_sdr(video)
    size = stream_size(video)
    duration, total_length, rate = stream_timing(metadata, video)
    if duration is None:
        raise ValueError('The input video must have a known duration.')
    end = duration if args.end is None else args.end
    if not math.isfinite(args.start) or not math.isfinite(end) or not 0 <= args.start < end <= duration:
        raise ValueError('The edit must lie inside the video timeline.')
    if args.preview_at is not None and not 0 <= args.preview_at < duration:
        raise ValueError('The preview time must lie inside the video timeline.')
    if suffix == '.mp4' and any(dimension % 2 for dimension in size):
        raise ValueError('H.264 yuv420p requires even dimensions. Use a lossless MKV master for this size.')
    source_start = float(video.get('start_time') or 0) - float(metadata['format'].get('start_time') or 0)
    if any(path.suffix.lower() != '.png' for path in layers[1:]) and abs(source_start) > 0.0005:
        raise ValueError('Align the source and moving layers to the same zero video timestamp first.')
    indices = [video['index']]
    indices.extend(inspect_layer(path, kind, size, video, duration, rate)
                   for path, kind in zip(layers[1:], ('plate', 'mask', 'title')))
    filters, output_label = filter_graph(indices, args.start, end)
    audio = [stream for stream in metadata['streams'] if stream['codec_type'] == 'audio']
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-filter_complex_threads', '2']
    for path in layers:
        command += ['-i', str(path)]
    command += ['-filter_complex', filters, '-map', output_label]
    if suffix == '.png':
        command += ['-ss', str(args.preview_at), '-frames:v', '1', '-pix_fmt', 'rgb24', '-threads', '1', '-update', '1']
    else:
        command += ['-map', '0:a?', '-c:a', 'copy', '-fps_mode', 'passthrough']
        if suffix == '.mkv':
            command += ['-c:v', 'ffv1', '-level', '3', '-pix_fmt', 'bgr0']
        else:
            command += ['-c:v', 'libx264', '-crf', '16', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                        '-movflags', '+faststart']
            for index, stream in enumerate(audio):
                if stream['codec_name'] != 'aac':
                    command += [f'-c:a:{index}', 'aac', f'-b:a:{index}', '192k']
    with tempfile.TemporaryDirectory(prefix='.exovance-replace-', dir=output.parent) as temp:
        staged = Path(temp) / output.name
        run([*command, str(staged)], fail_on_stderr=True)
        report = {'output': str(output), 'size': list(size), 'start': args.start, 'end': end}
        if suffix == '.mp4':
            expected_fps = rate if rate == frame_rate(video.get('r_frame_rate')) else None
            expected_total = total_length if len(audio) + 1 == len(metadata['streams']) else None
            report['verification'] = verify_export(
                staged, size=size, fps=expected_fps, duration=expected_total, video_duration=duration,
                audio_required=bool(audio),
            )
        finish(staged, output, args.overwrite)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    mask = modes.add_parser('mask', help='Build a narrow mask from the difference between two aligned PNG frames.')
    mask.add_argument('frame', type=Path)
    mask.add_argument('clean_plate', type=Path)
    mask.add_argument('output', type=Path)
    mask.add_argument('--roi', type=int, nargs=4, required=True, metavar=('X', 'Y', 'WIDTH', 'HEIGHT'))
    mask.add_argument('--threshold', type=int, default=8, help='Minimum per-channel difference, from 1 to 255.')
    mask.add_argument('--expand', type=int, default=1, help='Expand the difference mask to include glyph edges.')
    mask.add_argument('--overwrite', action='store_true')
    mask.set_defaults(handler=make_mask)

    title = modes.add_parser('title', help='Render replacement text to a full-frame transparent PNG.')
    title.add_argument('output', type=Path)
    title.add_argument('--size', type=int, nargs=2, required=True, metavar=('WIDTH', 'HEIGHT'))
    title.add_argument('--text', required=True)
    title.add_argument('--font', type=Path, required=True)
    title.add_argument('--font-size', type=int, required=True)
    title.add_argument('--position', type=int, nargs=2, required=True, metavar=('X', 'Y'))
    title.add_argument('--color', default='white')
    title.add_argument('--stroke', type=int, default=0)
    title.add_argument('--stroke-color', default='black')
    title.add_argument('--overwrite', action='store_true')
    title.set_defaults(handler=make_title)

    composite = modes.add_parser('render', help='Composite the clean plate and replacement title.')
    composite.add_argument('source', type=Path)
    composite.add_argument('output', type=Path)
    composite.add_argument('--clean-plate', type=Path, required=True)
    composite.add_argument('--mask', type=Path, required=True)
    composite.add_argument('--title-overlay', type=Path)
    composite.add_argument('--start', type=float, default=0)
    composite.add_argument('--end', type=float)
    composite.add_argument('--preview-at', type=float)
    composite.add_argument('--overwrite', action='store_true')
    composite.set_defaults(handler=render)
    args = parser.parse_args()
    try:
        report = args.handler(args)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f'Text replacement failed: {error}\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
