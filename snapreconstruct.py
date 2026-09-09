#!/usr/bin/env python3
"""Merge segmented videos from a Snapchat data export.

Snapchat splits long videos into ~10s segment files in chat_media/. This tool
finds the segments (clustered by embedded creation_time), orders them by visual
continuity (last frame of one segment matches first frame of the next), and
losslessly concatenates each chain with ffmpeg stream copy.

Usage:
    python3 snapreconstruct.py <export_dir> [-o OUTPUT_DIR]

<export_dir> is the unzipped export (the folder containing chat_media/, and
optionally json/snap_history.json + json/chat_history.json for sender names).
Requires ffmpeg/ffprobe. Originals are never modified.
"""
import argparse
import datetime
import itertools
import json
import os
import subprocess
import sys
import tempfile

CLUSTER_GAP_S = 5        # segments of one video share creation_time within this
FRAME_MATCH_MAX = 30     # mean abs gray diff; real boundaries score <25, unrelated >36
SENDER_MATCH_MAX_S = 120 # history event must be this close to attribute a sender
FRAME_SIZE = 64          # comparison thumbnail edge


def ffprobe(path):
    p = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', path],
        capture_output=True, text=True)
    return json.loads(p.stdout) if p.stdout else None


def probe_videos(media_dir):
    rows = []
    files = sorted(f for f in os.listdir(media_dir) if f.lower().endswith('.mp4'))
    for i, f in enumerate(files):
        d = ffprobe(os.path.join(media_dir, f))
        if not d or 'format' not in d:
            print(f'  ! unreadable, skipping: {f}', file=sys.stderr)
            continue
        vstreams = [s for s in d['streams'] if s['codec_type'] == 'video']
        if not vstreams:
            continue
        v = vstreams[0]
        a = [s for s in d['streams'] if s['codec_type'] == 'audio']
        ct = d['format'].get('tags', {}).get('creation_time', '')
        try:
            ts = datetime.datetime.fromisoformat(ct.replace('Z', '+00:00')).timestamp()
        except ValueError:
            ts = 0
        rows.append(dict(
            f=f, dur=float(d['format'].get('duration', 0)), ct=ct, ts=ts,
            res=f"{v.get('width')}x{v.get('height')}", vcodec=v.get('codec_name'),
            audio=bool(a), mtime=os.path.getmtime(os.path.join(media_dir, f))))
        if (i + 1) % 50 == 0:
            print(f'  probed {i + 1}/{len(files)}')
    return rows


def cluster(rows):
    """Group mp4s whose creation_times are within CLUSTER_GAP_S of each other."""
    rows = sorted(rows, key=lambda r: (r['ts'], r['f']))
    out = []
    for r in rows:
        if (out and r['ts'] > 0 and out[-1][-1]['ts'] > 0
                and r['ts'] - out[-1][-1]['ts'] <= CLUSTER_GAP_S
                and r['f'][:10] == out[-1][-1]['f'][:10]):
            out[-1].append(r)
        else:
            out.append([r])
    return out


def boundary_frames(media_dir, fname, cache_dir):
    """Return (first_frame, last_frame) as raw 64x64 grayscale bytes."""
    base = os.path.join(cache_dir, fname)
    if not os.path.exists(base + '.first'):
        src = os.path.join(media_dir, fname)
        scale = f'scale={FRAME_SIZE}:{FRAME_SIZE}'
        subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-i', src, '-frames:v', '1',
             '-vf', scale, '-f', 'rawvideo', '-pix_fmt', 'gray', base + '.first'],
            check=True)
        p = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-sseof', '-0.3', '-i', src,
             '-vf', scale, '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
            capture_output=True, check=True)
        with open(base + '.last', 'wb') as fh:
            fh.write(p.stdout[-FRAME_SIZE * FRAME_SIZE:])
    with open(base + '.first', 'rb') as fh:
        first = fh.read()
    with open(base + '.last', 'rb') as fh:
        last = fh.read()
    return first, last


def frame_dist(a, b):
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def build_chains(clusters, media_dir, cache_dir):
    """Within each multi-file cluster, chain segments by frame continuity."""
    chains, standalone_in_bursts = [], []
    for c in clusters:
        if len(c) < 2:
            continue
        frames = {x['f']: boundary_frames(media_dir, x['f'], cache_dir) for x in c}
        n = len(c)
        dists = {}
        for i, j in itertools.permutations(range(n), 2):
            if c[i]['res'] != c[j]['res']:
                continue
            dists[(i, j)] = frame_dist(frames[c[i]['f']][1], frames[c[j]['f']][0])
        succ, pred = {}, {}
        for (i, j), d in sorted(dists.items(), key=lambda kv: kv[1]):
            if d >= FRAME_MATCH_MAX:
                break
            if i in succ or j in pred:
                continue
            k, cyclic = j, False
            while k in succ:
                k = succ[k]
                if k == i:
                    cyclic = True
                    break
            if cyclic:
                continue
            succ[i] = j
            pred[j] = i
        for i in range(n):
            if i in pred:
                continue
            ch = [i]
            while ch[-1] in succ:
                ch.append(succ[ch[-1]])
            if len(ch) > 1:
                chains.append([c[k] for k in ch])
            else:
                standalone_in_bursts.append(c[i]['f'])
    return chains, standalone_in_bursts


def load_sender_events(export_dir):
    events = []
    for name in ('snap_history.json', 'chat_history.json'):
        path = os.path.join(export_dir, 'json', name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            data = json.load(fh)
        for msgs in data.values():
            for m in msgs:
                if m.get('Media Type') in ('VIDEO', 'MEDIA'):
                    events.append((m['Created(microseconds)'] / 1000, m['From']))
    return sorted(events)


def sender_for(ts, events):
    if not events:
        return 'unknown'
    best = min(events, key=lambda e: abs(e[0] - ts))
    return best[1] if abs(best[0] - ts) <= SENDER_MATCH_MAX_S else 'unknown'


def merge_chain(chain, media_dir, out_path, ct, mtime, tmp_dir):
    """Concat segments: losslessly (stream copy) when all parts share one video
    codec, else via re-encode (Snapchat occasionally mixes h264 and hevc parts
    in one video, which cannot share an mp4 track). For the lossless path,
    stream order is normalized first because Snapchat sometimes muxes
    audio-first in one segment and video-first in the next, which silently
    corrupts concat-demuxer output."""
    all_audio = all(x['audio'] for x in chain)
    mixed_codec = len({x['vcodec'] for x in chain}) > 1
    if mixed_codec:
        inputs, fparts = [], []
        for k, x in enumerate(chain):
            inputs += ['-i', os.path.join(media_dir, x['f'])]
            fparts.append(f'[{k}:v:0]' + (f'[{k}:a:0]' if all_audio else ''))
        fc = ''.join(fparts) + \
            f'concat=n={len(chain)}:v=1:a={1 if all_audio else 0}[v]' + \
            ('[a]' if all_audio else '')
        maps = ['-map', '[v]'] + (['-map', '[a]'] if all_audio else [])
        cmd = (['ffmpeg', '-y', '-v', 'error'] + inputs
               + ['-filter_complex', fc] + maps
               + ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium']
               + (['-c:a', 'aac', '-b:a', '160k'] if all_audio else [])
               + ['-movflags', '+faststart', '-metadata', 'creation_time=' + ct,
                  out_path])
        r = subprocess.run(cmd, capture_output=True, text=True)
    else:
        norm_files = []
        for k, x in enumerate(chain):
            t = os.path.join(tmp_dir, f'norm{k}.mp4')
            maps = ['-map', '0:v:0'] + (['-map', '0:a:0'] if all_audio else [])
            subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-i', os.path.join(media_dir, x['f'])]
                + maps + ['-c', 'copy', t], check=True)
            norm_files.append(t)
        lst = os.path.join(tmp_dir, 'concat.txt')
        with open(lst, 'w') as fh:
            for t in norm_files:
                fh.write(f"file '{t}'\n")
        r = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', lst,
             '-c', 'copy', '-movflags', '+faststart',
             '-metadata', 'creation_time=' + ct, out_path],
            capture_output=True, text=True)
    if r.returncode or r.stderr.strip():
        return r.stderr.strip() or f'ffmpeg exit {r.returncode}'
    os.utime(out_path, (mtime, mtime))
    d = ffprobe(out_path)
    got = float(d['format']['duration'])
    want = sum(x['dur'] for x in chain)
    if abs(got - want) > 0.5:
        return f'duration mismatch: got {got:.2f}s, expected {want:.2f}s'
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('export_dir', help='unzipped Snapchat export (contains chat_media/)')
    ap.add_argument('-o', '--output', default=None,
                    help='output dir (default: <export_dir>/../merged_videos)')
    args = ap.parse_args()

    export_dir = os.path.abspath(args.export_dir)
    media_dir = os.path.join(export_dir, 'chat_media')
    if not os.path.isdir(media_dir):
        sys.exit(f'no chat_media/ folder in {export_dir}')
    out_dir = os.path.abspath(args.output or
                              os.path.join(export_dir, os.pardir, 'merged_videos'))
    os.makedirs(out_dir, exist_ok=True)

    print('Probing videos...')
    rows = probe_videos(media_dir)
    clusters = cluster(rows)
    multi = [c for c in clusters if len(c) > 1]
    print(f'{len(rows)} mp4s -> {len(multi)} same-time clusters to examine')

    print('Matching boundary frames...')
    with tempfile.TemporaryDirectory(prefix='snapreconstruct_') as tmp:
        cache = os.path.join(tmp, 'frames')
        os.makedirs(cache)
        chains, burst_singles = build_chains(multi, media_dir, cache)
        print(f'{len(chains)} videos to merge from '
              f'{sum(len(c) for c in chains)} segments; '
              f'{len(burst_singles)} burst files left standalone')

        events = load_sender_events(export_dir)
        report, errors = [], []
        for chain in chains:
            ct = chain[0]['ct']
            stamp = ct[:19].replace('T', '_').replace(':', '-')
            sender = sender_for(chain[0]['ts'], events)
            name = f'{stamp}_{sender}_{len(chain)}parts.mp4'
            err = merge_chain(chain, media_dir, os.path.join(out_dir, name),
                              ct, chain[0]['mtime'], tmp)
            if err:
                errors.append((name, err))
                print(f'  ERROR {name}: {err}')
            else:
                report.append(dict(output=name, sender=sender,
                                   duration=round(sum(x['dur'] for x in chain), 2),
                                   parts=[x['f'] for x in chain]))
                print(f'  merged {name} ({len(chain)} parts)')

    with open(os.path.join(out_dir, 'merge_report.json'), 'w') as fh:
        json.dump(dict(merged=report, errors=errors,
                       standalone_in_bursts=burst_singles), fh, indent=1)
    total = sum(r['duration'] for r in report)
    print(f'\nDone: {len(report)} merged videos ({total / 60:.1f} min) in {out_dir}')
    if errors:
        print(f'{len(errors)} FAILED - see merge_report.json')


if __name__ == '__main__':
    main()
