#!/usr/bin/env python3
"""Probe media, create timestamped source stills, and optionally scan text or audio."""
import argparse
import csv
import io
import json
import math
import shutil
import subprocess
from pathlib import Path
from fractions import Fraction


def run(command, *, input_data=None):
    p = subprocess.run(command, input=input_data, capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors='replace')[-4000:])
    return p.stdout


def detect_words(frame, language):
    tsv = run(
        ['tesseract', 'stdin', 'stdout', '-l', language, '--psm', '11', 'tsv'],
        input_data=frame,
    )
    rows = csv.DictReader(
        io.StringIO(tsv.decode('utf-8', errors='replace')),
        delimiter='\t',
        quoting=csv.QUOTE_NONE,
    )
    words = []
    for row in rows:
        text = (row.get('text') or '').strip()
        if row['level'] != '5' or not text:
            continue
        words.append({
            'text': text,
            'confidence': float(row['conf']),
            'box': {key: int(row[key]) for key in ('left', 'top', 'width', 'height')},
        })
    return words


def onsets(source, seconds):
    import numpy as np
    rate, hop, window = 16000, 160, 1024
    raw = run(['ffmpeg','-v','error','-i',str(source),'-t',str(seconds),'-vn','-ac','1','-ar',str(rate),'-f','f32le','-'])
    signal = np.frombuffer(raw,dtype='<f4')
    if len(signal) < window:
        return {'note':'Not enough audio.', 'candidates_seconds':[]}
    count = 1+(len(signal)-window)//hop
    index = np.arange(window)[None,:]+np.arange(count)[:,None]*hop
    frames = signal[index]*np.hanning(window)
    spectrum = np.abs(np.fft.rfft(frames,axis=1))
    flux = np.maximum(0,np.diff(np.log1p(spectrum),axis=0)).mean(axis=1)
    if not flux.size or float(flux.max()) < 1e-9:
        return {'note':'No clear transients.', 'candidates_seconds':[]}
    smooth = np.convolve(flux,np.ones(3)/3,mode='same')
    baseline = np.convolve(smooth,np.ones(41)/41,mode='same')
    strength = smooth-baseline
    selected=[]
    for i in sorted(range(1,len(strength)-1),key=lambda j:-strength[j]):
        if strength[i] <= max(float(np.std(strength))*.5,0):
            continue
        if smooth[i] < smooth[i-1] or smooth[i] < smooth[i+1]:
            continue
        t=((i+1)*hop+window/2)/rate
        if all(abs(t-p[0]) >= .20 for p in selected):
            selected.append((t,float(strength[i])))
        if len(selected) >= 200:
            break
    selected.sort()
    return {'note':'Transient candidates, not verified beats or a guaranteed BPM grid. Validate against the chosen musical phrase.',
            'analyzed_seconds':min(seconds,len(signal)/rate),
            'candidates_seconds':[round(t,3) for t,_ in selected],
            'strength':[round(s,6) for _,s in selected]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=Path)
    p.add_argument('--out-dir',type=Path,required=True)
    p.add_argument('--frames',type=int,default=10)
    p.add_argument('--frames-from', type=float, default=0, help='First timestamp to sample, in source seconds.')
    p.add_argument('--frames-to', type=float, help='Last timestamp to sample, in source seconds.')
    p.add_argument('--ocr', action='store_true', help='Scan sampled video frames with local Tesseract.')
    p.add_argument('--ocr-language', help='Installed Tesseract language(s), for example eng or eng+rus.')
    p.add_argument('--audio-onsets',action='store_true')
    p.add_argument('--analysis-seconds',type=float,default=90)
    args=p.parse_args();source=args.source.resolve();out=args.out_dir.resolve()
    if not source.is_file() or not 1 <= args.frames <= 30:
        p.error('Use an existing source and 1–30 stills.')
    if not math.isfinite(args.frames_from) or args.frames_from < 0:
        p.error('--frames-from must be a finite, non-negative timestamp.')
    if args.frames_to is not None and (
        not math.isfinite(args.frames_to) or args.frames_to <= args.frames_from
    ):
        p.error('--frames-to must be finite and later than --frames-from.')
    if args.ocr_language and not args.ocr:
        p.error('--ocr-language requires --ocr.')
    if args.ocr and not shutil.which('tesseract'):
        p.error('--ocr requires Tesseract on PATH.')
    out.mkdir(parents=True,exist_ok=True)
    meta=json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(source)]))
    duration=float(meta.get('format',{}).get('duration') or 0)
    result={'source':str(source),'duration_seconds':duration,'streams':meta.get('streams',[])}
    video=next((s for s in meta.get('streams',[]) if s['codec_type']=='video'),None)
    if (args.ocr or args.frames_from or args.frames_to is not None) and (not video or duration <= 0):
        p.error('OCR or a frame range requires a video with a known duration.')
    if video and duration>0:
        from PIL import Image,ImageDraw
        video_duration = float(video.get('duration') or duration)
        first_sample = args.frames_from
        if first_sample >= video_duration:
            p.error('--frames-from must be before the video ends.')
        try:
            frame_rate = Fraction(video.get('avg_frame_rate') or '0')
        except (ValueError, ZeroDivisionError):
            frame_rate = Fraction(0)
        frame_interval = float(1 / frame_rate) if frame_rate > 0 else 0.05
        # Container duration can include audio beyond the final video frame.
        safe_end = (
            max(0, video_duration - frame_interval)
            if args.frames_to is not None
            else max(0, min(video_duration * 0.98, video_duration - frame_interval))
        )
        last_sample = max(first_sample, min(safe_end, args.frames_to or video_duration))
        if args.frames == 1:
            times = [(first_sample + last_sample) / 2]
        else:
            span = last_sample - first_sample
            times = [first_sample + span * i / (args.frames - 1) for i in range(args.frames)]
        images=[]
        actual_times = []
        ocr_samples = []
        frame_filter = [] if args.ocr else ['-vf', 'scale=320:-2']
        for i, requested_time in enumerate(times):
            # The estimated frame interval may fall between uneven timestamps.
            for t in dict.fromkeys((
                requested_time, max(first_sample, requested_time - max(0.15, frame_interval)),
                first_sample,
            )):
                raw = run([
                    'ffmpeg', '-v', 'error', '-ss', str(t), '-i', str(source),
                    '-frames:v', '1', *frame_filter, '-f', 'image2pipe', '-vcodec', 'png', '-',
                ])
                if raw:
                    break
            else:
                raise RuntimeError(f'No video frame decoded for sample at {requested_time:.3f} s.')
            actual_times.append(t)
            im=Image.open(io.BytesIO(raw)).convert('RGB')
            filename = f'frame_{i:02d}.png'
            if args.ocr:
                ocr_samples.append({
                    'sample_time_seconds': t,
                    'preview_file': filename,
                    'frame_width': im.width,
                    'frame_height': im.height,
                    'words': detect_words(raw, args.ocr_language or 'eng'),
                })
                thumbnail_height = max(1, round(320 * im.height / im.width))
                im = im.resize((320, thumbnail_height), Image.Resampling.LANCZOS)
            im.save(out / filename)
            ImageDraw.Draw(im).text((5,5),f'{t:.3f} s',fill='white',stroke_width=1,stroke_fill='black')
            images.append(im)
        width,height=images[0].size;columns=min(5,len(images));rows=math.ceil(len(images)/columns)
        canvas=Image.new('RGB',(columns*width,rows*height),(20,20,20))
        for i,im in enumerate(images):canvas.paste(im,((i%columns)*width,(i//columns)*height))
        canvas.save(out/'contact.jpg');result['contact_sheet']=str(out/'contact.jpg');result['sample_times_seconds']=actual_times
        if args.ocr:
            result['ocr_samples'] = ocr_samples
    if args.audio_onsets and any(s['codec_type']=='audio' for s in meta.get('streams',[])):
        if not math.isfinite(args.analysis_seconds) or not 0<args.analysis_seconds<=600:
            p.error('Analyze a positive music excerpt of at most 600 seconds.')
        result['music_onsets']=onsets(source,args.analysis_seconds)
    (out/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'audit':str(out/'audit.json'),'duration':duration,'contact_sheet':result.get('contact_sheet'),'onset_candidates':len(result.get('music_onsets',{}).get('candidates_seconds',[]))},ensure_ascii=False))


if __name__=='__main__':main()
