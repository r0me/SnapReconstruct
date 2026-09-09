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
import shutil
import subprocess
import sys
import tempfile
import time

CLUSTER_GAP_S = 5        # segments of one video share creation_time within this
FRAME_MATCH_MAX = 30     # mean abs gray diff; real boundaries score <25, unrelated >36
SENDER_MATCH_MAX_S = 120 # history event must be this close to attribute a sender
FRAME_SIZE = 64          # comparison thumbnail edge


class Progress:
    """Single-line terminal progress bar; falls back to periodic plain prints
    when stdout is not a TTY (logs, pipes, CI)."""
    BAR_WIDTH = 26

    def __init__(self, label, total):
        self.label = label
        self.total = max(total, 1)
        self.n = 0
        self.start = time.monotonic()
        self.tty = sys.stdout.isatty()
        self._last_draw = 0.0

    def step(self, item=''):
        self.n += 1
        now = time.monotonic()
        if not self.tty:
            if self.n == self.total or now - self._last_draw >= 5:
                self._last_draw = now
                print(f'  {self.label}: {self.n}/{self.total}', flush=True)
            return
        if now - self._last_draw < 0.05 and self.n < self.total:
            return
        self._last_draw = now
        frac = self.n / self.total
        filled = int(self.BAR_WIDTH * frac)
        bar = '█' * filled + '░' * (self.BAR_WIDTH - filled)
        eta = ''
        if 0 < self.n < self.total:
            rem = (now - self.start) / self.n * (self.total - self.n)
            eta = f'  eta {int(rem) // 60}:{int(rem) % 60:02d}'
        cols = shutil.get_terminal_size((80, 20)).columns
        line = f'  {self.label} {bar} {self.n}/{self.total} {frac:4.0%}{eta}  {item}'
        print('\r' + line[:cols - 1].ljust(cols - 1), end='', flush=True)

    def interrupt(self, message):
        """Print a full line (e.g. an error) without corrupting the bar."""
        if self.tty:
            cols = shutil.get_terminal_size((80, 20)).columns
            print('\r' + ' ' * (cols - 1) + '\r', end='')
        print(message, flush=True)
        self._last_draw = 0.0

    def close(self):
        if self.tty:
            print()
        elapsed = time.monotonic() - self.start
        print(f'  done in {elapsed:.1f}s', flush=True)


def phase(title):
    print(f'\n▶ {title}', flush=True)


def pick_directory(title, initial=None):
    """Open the system folder picker; returns the chosen path or None if the
    dialog was cancelled. Tries tkinter, zenity, kdialog, macOS osascript and
    Windows PowerShell, then falls back to a plain text prompt."""
    initial = initial or os.path.expanduser('~')
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title=title, initialdir=initial)
        root.destroy()
        return path or None
    except Exception:
        pass
    candidates = []
    if sys.platform == 'darwin':
        candidates.append(['osascript', '-e',
                           f'POSIX path of (choose folder with prompt "{title}" '
                           f'default location POSIX file "{initial}")'])
    elif sys.platform.startswith('win'):
        candidates.append(['powershell', '-NoProfile', '-Command',
                           'Add-Type -AssemblyName System.Windows.Forms; '
                           '$d = New-Object System.Windows.Forms.FolderBrowserDialog; '
                           f"$d.Description = '{title}'; "
                           'if ($d.ShowDialog() -eq "OK") { $d.SelectedPath }'])
    else:
        candidates.append(['zenity', '--file-selection', '--directory',
                           f'--title={title}', f'--filename={initial}/'])
        candidates.append(['kdialog', '--title', title,
                           '--getexistingdirectory', initial])
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        return None  # dialog shown but cancelled
    if sys.stdin.isatty():
        answer = input(f'{title}\n  (no folder-picker dialog available) '
                       'path (blank to cancel): ').strip()
        return os.path.expanduser(answer) or None
    return None


def resolve_export_dir(path):
    """Be forgiving about what the user points at: accept the export folder,
    or chat_media itself, or a folder holding exactly one export."""
    if os.path.isdir(os.path.join(path, 'chat_media')):
        return path
    if os.path.basename(os.path.normpath(path)) == 'chat_media':
        return os.path.dirname(os.path.normpath(path))
    subs = [os.path.join(path, d) for d in os.listdir(path)
            if os.path.isdir(os.path.join(path, d, 'chat_media'))] \
        if os.path.isdir(path) else []
    if len(subs) == 1:
        return subs[0]
    return None


def ffprobe(path):
    p = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', path],
        capture_output=True, text=True)
    return json.loads(p.stdout) if p.stdout else None


def probe_videos(media_dir):
    rows = []
    files = sorted(f for f in os.listdir(media_dir) if f.lower().endswith('.mp4'))
    prog = Progress('reading metadata', len(files))
    for f in files:
        prog.step(f)
        d = ffprobe(os.path.join(media_dir, f))
        if not d or 'format' not in d:
            prog.interrupt(f'  ! unreadable, skipping: {f}')
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
    prog.close()
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
    todo = [c for c in clusters if len(c) >= 2]
    prog = Progress('comparing frames', sum(len(c) for c in todo))
    for c in clusters:
        if len(c) < 2:
            continue
        frames = {}
        for x in c:
            prog.step(x['f'])
            frames[x['f']] = boundary_frames(media_dir, x['f'], cache_dir)
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
    prog.close()
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


def unique_path(out_dir, name):
    base, ext = os.path.splitext(name)
    path = os.path.join(out_dir, name)
    n = 2
    while os.path.exists(path):
        path = os.path.join(out_dir, f'{base}_{n}{ext}')
        n += 1
    return path


def copy_singles(singles, media_dir, out_dir, events):
    """Copy videos that were never split into the output dir, renamed with the
    same timestamp+sender scheme (suffix _1part) so the set is complete."""
    copied = []
    prog = Progress('renaming + copying', len(singles))
    for x in singles:
        prog.step(x['f'])
        ts = x['ts'] or x['mtime']
        stamp = datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
        sender = sender_for(ts, events)
        path = unique_path(out_dir, f'{stamp}_{sender}_1part.mp4')
        shutil.copy2(os.path.join(media_dir, x['f']), path)
        copied.append(dict(output=os.path.basename(path), sender=sender,
                           duration=round(x['dur'], 2), parts=[x['f']]))
    prog.close()
    return copied


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('export_dir', nargs='?', default=None,
                    help='unzipped Snapchat export (contains chat_media/); '
                         'a folder picker opens if omitted')
    ap.add_argument('-o', '--output', default=None,
                    help='output dir; a folder picker opens if omitted '
                         '(Cancel there = default <export_dir>/../merged_videos)')
    ap.add_argument('--merged-only', action='store_true',
                    help='only write stitched videos; skip copying single-segment ones')
    args = ap.parse_args()

    if args.export_dir is None:
        print('No export folder given - opening folder picker...')
        args.export_dir = pick_directory(
            'Select your Snapchat export folder (the one containing chat_media)')
        if not args.export_dir:
            sys.exit('cancelled - no export folder chosen')
    export_dir = resolve_export_dir(os.path.abspath(args.export_dir))
    if not export_dir:
        sys.exit(f'no chat_media/ folder found in {args.export_dir}')
    media_dir = os.path.join(export_dir, 'chat_media')

    default_out = os.path.join(export_dir, os.pardir, 'merged_videos')
    if args.output is None:
        print('No output folder given - opening folder picker '
              '(Cancel = default merged_videos next to the export)...')
        args.output = pick_directory(
            'Select the output folder for merged videos (Cancel = default)',
            initial=os.path.dirname(export_dir))
    out_dir = os.path.abspath(args.output or default_out)
    os.makedirs(out_dir, exist_ok=True)

    phase(f'Probing {media_dir}')
    rows = probe_videos(media_dir)
    clusters = cluster(rows)
    multi = [c for c in clusters if len(c) > 1]
    print(f'  {len(rows)} videos, {len(multi)} same-time clusters to examine')

    phase('Detecting segments (boundary-frame matching)')
    with tempfile.TemporaryDirectory(prefix='snapreconstruct_') as tmp:
        cache = os.path.join(tmp, 'frames')
        os.makedirs(cache)
        chains, burst_singles = build_chains(multi, media_dir, cache)
        n_seg = sum(len(c) for c in chains)
        print(f'  {n_seg} segments form {len(chains)} split videos; '
              f'{len(burst_singles)} same-burst files are distinct videos')

        events = load_sender_events(export_dir)
        if not events:
            print('  ! no json/ history found - senders will be "unknown"')

        phase(f'Stitching {len(chains)} videos')
        report, errors = [], []
        prog = Progress('stitching', len(chains))
        for chain in chains:
            ct = chain[0]['ct']
            stamp = ct[:19].replace('T', '_').replace(':', '-')
            sender = sender_for(chain[0]['ts'], events)
            name = f'{stamp}_{sender}_{len(chain)}parts.mp4'
            prog.step(name)
            err = merge_chain(chain, media_dir, os.path.join(out_dir, name),
                              ct, chain[0]['mtime'], tmp)
            if err:
                errors.append((name, err))
                prog.interrupt(f'  ✗ {name}: {err}')
            else:
                report.append(dict(output=name, sender=sender,
                                   duration=round(sum(x['dur'] for x in chain), 2),
                                   parts=[x['f'] for x in chain]))
        prog.close()

    singles_report = []
    if not args.merged_only:
        chained = {x['f'] for chain in chains for x in chain}
        singles = [r for r in rows if r['f'] not in chained]
        phase(f'Renaming + copying {len(singles)} single-segment videos')
        singles_report = copy_singles(singles, media_dir, out_dir, events)

    with open(os.path.join(out_dir, 'merge_report.json'), 'w') as fh:
        json.dump(dict(merged=report, singles=singles_report, errors=errors,
                       standalone_in_bursts=burst_singles), fh, indent=1)

    total = sum(r['duration'] for r in report)
    senders = sorted({r['sender'] for r in report + singles_report} - {'unknown'})
    print('\n' + '─' * 56)
    print(f'✔ {len(report)} videos reconstructed from {n_seg} segments '
          f'({total / 60:.1f} min of footage)')
    if singles_report:
        print(f'✔ {len(singles_report)} single videos renamed + copied')
    if senders:
        print(f'✔ senders: {", ".join(senders)}')
    if burst_singles:
        print(f'• {len(burst_singles)} burst files kept separate '
              f'(same send time, different videos)')
    if errors:
        print(f'✗ {len(errors)} FAILED - details in merge_report.json')
    print(f'→ {out_dir}')


if __name__ == '__main__':
    main()
