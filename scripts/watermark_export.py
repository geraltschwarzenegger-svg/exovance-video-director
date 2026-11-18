#!/usr/bin/env python3
"""Render one timed translucent signature, with optional movement and a soft glint."""
import argparse, json, math, subprocess, tempfile
from pathlib import Path


def run(command):
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-5000:])
    return result.stdout


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def smooth(expression):
    u = f'clip({expression},0,1)'
    return f'({u})*({u})*(3-2*({u}))'


def filter_path(path):
    return str(path).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--text', default='Exovance')
    parser.add_argument('--start', type=float, required=True)
    parser.add_argument('--fade', type=float, default=0)
    parser.add_argument('--end', type=float)
    parser.add_argument('--fade-out', type=float, default=.4)
    parser.add_argument('--effect', choices=['none', 'soft-glint'], default='none',
                        help='Optional small upward motion and one brief highlight as the title appears.')
    parser.add_argument('--opacity', type=float, default=.34)
    parser.add_argument('--font-size', type=int)
    parser.add_argument('--font', type=Path)
    parser.add_argument('--margin', '--bottom', dest='bottom', type=int)
    parser.add_argument('--position', required=True, choices=['bottom-center', 'bottom-left', 'bottom-right', 'top-center', 'top-left', 'top-right', 'center', 'custom'])
    parser.add_argument('--x', type=float)
    parser.add_argument('--y', type=float)
    parser.add_argument('--crop-bottom', type=int, default=0)
    parser.add_argument('--crop-mode', choices=['preserve-aspect', 'wide'], default='preserve-aspect')
    parser.add_argument('--allow-large-crop', action='store_true')
    parser.add_argument('--preview-at', type=float)
    parser.add_argument('--fps', type=int, help='Output frame rate, for example 60. Omit to retain the source cadence.')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if not source.is_file() or source == output:
        parser.error('Use an existing source and a separate output path.')
    if output.exists() and not args.overwrite:
        parser.error('Output exists; choose a new version or explicitly use --overwrite.')
    values = [args.start, args.fade, args.fade_out, args.opacity]
    values += [x for x in [args.end, args.preview_at] if x is not None]
    if not all(math.isfinite(x) for x in values) or min(args.start, args.fade, args.fade_out) < 0:
        parser.error('Times must be finite and non-negative.')
    if not 0 <= args.opacity <= 1 or args.end is not None and args.end <= args.start:
        parser.error('Invalid opacity or end time.')
    data = probe(source)
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if video is None:
        parser.error('Source has no video stream.')
    width, height = video['width'], video['height']
    rotation = next(
        (entry['rotation'] for entry in video.get('side_data_list', []) if 'rotation' in entry),
        0,
    )
    if rotation % 180 == 90:
        width, height = height, width
    duration = float(video.get('duration') or data.get('format', {}).get('duration') or 0)
    if duration and args.start >= duration:
        parser.error('Appearance begins after the video ends.')
    if args.preview_at is not None and (args.preview_at < 0 or duration and args.preview_at >= duration):
        parser.error('Preview time lies outside the video.')
    if args.crop_bottom < 0 or args.crop_bottom >= height-2:
        parser.error('Invalid bottom crop.')
    if args.crop_bottom / height > .05 and not args.allow_large_crop:
        parser.error('Crop exceeds 5%: explain/show the framing trade-off before using --allow-large-crop.')
    if args.fps is not None and args.fps <= 0:
        parser.error('Output frame rate must be positive.')
    filters = []
    cropped_w, cropped_h = width, height
    if args.crop_bottom:
        cropped_h = (height-args.crop_bottom)//2*2
        cropped_w = width if args.crop_mode == 'wide' else int(cropped_h*width/height)//2*2
        left = (width-cropped_w)//2
        filters.append(f'crop={cropped_w}:{cropped_h}:{left}:0')
        if args.crop_mode == 'preserve-aspect':
            filters.append(f'scale={width}:{height}:flags=lanczos')
    if args.fps is not None:
        filters.append(f'fps={args.fps}')
    out_w = width
    out_h = cropped_h if args.crop_mode == 'wide' else height
    fonts = [args.font] if args.font else [
        Path('/usr/share/fonts/truetype/open-sans/OpenSans-BoldItalic.ttf'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf')]
    font = next((p for p in fonts if p and p.is_file()), None)
    if font is None:
        parser.error('No suitable font found; specify --font with an existing font file.')
    font_size = args.font_size or max(8, round(out_h*.03))
    bottom = args.bottom if args.bottom is not None else max(6, round(out_h*.02))
    if font_size < 1 or bottom < 0:
        parser.error('Invalid font size or margin.')
    if args.position == 'custom':
        if args.x is None or args.y is None or not all(math.isfinite(x) for x in [args.x, args.y]) or not 0 <= args.x < out_w or not 0 <= args.y < out_h:
            parser.error('Custom placement requires --x and --y inside the output frame.')
        xpos, ypos = str(args.x), str(args.y)
    else:
        xpos = str(bottom) if args.position.endswith('left') else f'w-tw-{bottom}' if args.position.endswith('right') else '(w-tw)/2'
        ypos = f'h-th-{bottom}' if args.position.startswith('bottom') else str(bottom) if args.position.startswith('top') else '(h-th)/2'
    alpha = smooth(f'(t-{args.start})/{args.fade}') if args.fade else f'gte(t,{args.start})'
    if args.end is not None:
        tail = smooth(f'({args.end}-t)/{args.fade_out}') if args.fade_out else f'lt(t,{args.end})'
        alpha = f'({alpha})*({tail})'
    if args.effect == 'soft-glint':
        motion_duration = args.fade or 0.55
        available = args.end if args.end is not None else duration
        if available:
            motion_duration = min(motion_duration, available - args.start)
        progress = f'(t-{args.start})/{motion_duration}'
        shift = max(1, round(min(out_h * 0.01, font_size * 0.4)))
        ypos = f'min(h-th,({ypos})+{shift}*(1-({smooth(progress)})))'
        brighten = smooth(f'(({progress})-0.4)/0.4')
        soften = smooth(f'(1.6-({progress}))/0.8')
        glint_alpha = f'({alpha})*({brighten})*({soften})'
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix='exovance-title-') as private,
        tempfile.TemporaryDirectory(prefix='.exovance-render-', dir=output.parent) as staging,
    ):
        textfile = Path(private)/'title.txt'
        render = Path(staging) / output.name
        textfile.write_text(args.text, encoding='utf-8')
        drawtext = (
            f"drawtext=fontfile='{filter_path(font)}':textfile='{filter_path(textfile)}'"
            f":expansion=none:fontsize={font_size}:x={xpos}:y='{ypos}'"
        )
        filters.append(
            f"{drawtext}:fontcolor=0xb4bfc8@{args.opacity}"
            f":shadowcolor=black@0.10:shadowx=1:shadowy=1:alpha='{alpha}'"
        )
        if args.effect == 'soft-glint':
            highlight_opacity = args.opacity * 0.45
            filters.append(
                f"{drawtext}:fontcolor=0xf3fbff@{highlight_opacity}:alpha='{glint_alpha}'"
            )
        command = ['ffmpeg', '-v', 'error', '-y', '-i', str(source)]
        if args.preview_at is not None:
            if output.suffix.lower() not in ['.png', '.jpg', '.jpeg']:
                parser.error('Use a PNG/JPEG output for --preview-at.')
            filters.append(f"select='gte(t,{args.preview_at})'")
            command += ['-vf', ','.join(filters), '-map', '0:v:0', '-an', '-frames:v', '1', str(render)]
        else:
            audio = next((s for s in data['streams'] if s['codec_type'] == 'audio'), None)
            command += ['-vf', ','.join(filters), '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-threads', '4', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p']
            command += ['-c:a', 'copy'] if not audio or audio['codec_name'] == 'aac' else ['-c:a', 'aac', '-b:a', '192k']
            command += ['-movflags', '+faststart', str(render)]
        run(command)
        if not render.is_file() or render.stat().st_size == 0:
            parser.error('No output frame was rendered at that timestamp.')
        if output.exists() and not args.overwrite:
            parser.error('Output was created while rendering; choose a new output path.')
        render.replace(output)
    result = {'output':str(output), 'bytes':output.stat().st_size, 'watermarks':1,
              'start':args.start, 'fade':args.fade, 'end':args.end, 'opacity':args.opacity,
              'crop':[cropped_w,cropped_h], 'output_size':[out_w,out_h], 'position':args.position,
              'effect':args.effect}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
