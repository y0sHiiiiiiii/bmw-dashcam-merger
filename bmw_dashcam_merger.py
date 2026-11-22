"""
BMW Dashcam Video Merger
Combines the four BMW dashcam videos (Front/Rear/Left/Right) into one layout
video with a HUD overlay.

Recognizes both German (Vorn/Hinten/Links/Rechts) and English (Front/Rear/
Left/Right) filenames. Runs cross-platform on Windows, macOS, and Linux.
"""

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path


# Camera-position aliases recognized in filenames.
# Add localized names here if your firmware uses a different language.
CAM_ALIASES = {
    'front': ['Vorn', 'Front'],
    'rear':  ['Hinten', 'Rear', 'Back'],
    'left':  ['Links', 'Left'],
    'right': ['Rechts', 'Right'],
}
KAMERA_WORDS = ['Kamera', 'Camera']
RAW_SUFFIXES = ['_Rohdaten', '_Raw']
NORMAL_SUFFIXES = ['']

# Telemetry XML tags. The car's USB export uses DATE_UTC/TIME_UTC and follows
# the car's locale for the date format and speed unit (e.g. VELOCITY_MPH and
# DD/MM/YYYY on UK cars). The My BMW app export uses DATE/TIME (also UTC).
DATE_TAGS = ('DATE_UTC', 'DATE')
TIME_TAGS = ('TIME_UTC', 'TIME')
SPEED_TAGS = (('VELOCITY_KMH', 'kmh'), ('VELOCITY_MPH', 'mph'))
KMH_PER_MPH = 1.609344
UNIT_LABELS = {'kmh': 'km/h', 'mph': 'mph'}

_DATE_RE = re.compile(r'^(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})$')


def _ensure_utf8_stdout():
    """Make stdout able to print Unicode on legacy Windows shells."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass


def detect_font():
    """Find a usable bold sans-serif font across Windows/macOS/Linux."""
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _filter_path(path):
    """Format a path for use inside an FFmpeg filter value.

    FFmpeg's filter-argument parser uses ':' as a separator and does not
    reliably honor single quotes or backslash escapes around path values.
    Workarounds per platform:
      - Windows: convert backslashes to forward slashes and strip the
        leading drive letter (e.g. "C:/Windows/Fonts/arialbd.ttf"
        becomes "/Windows/Fonts/arialbd.ttf"). Windows resolves paths
        starting with "/" against the current drive, so this still works,
        and the resulting string has no colon.
      - macOS/Linux: paths have no drive letter / no colon, return as-is.
    Limitation: on Windows, if the font lives on a different drive than
    the script's working directory, this fails. Use a font on the same
    drive in that case.
    """
    s = str(path).replace("\\", "/")
    if len(s) >= 2 and s[1] == ':':
        s = s[2:]
    return s


def parse_arguments():
    parser = argparse.ArgumentParser(
        description='BMW Dashcam Video Merger with HUD overlay'
    )
    parser.add_argument('--dir', type=str, default='.',
                        help='Folder containing the videos (default: current folder)')

    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--prefix', type=str,
                   help='File prefix including date/time (e.g. 2026-02-04_17-24-37_Manuell)')
    g.add_argument('--tag', type=str,
                   help='Constant part of the name, e.g. "Manuell"/"Manual" (date/time is auto-detected)')

    parser.add_argument('--xml', type=str, default=None,
                        help='Path to the telemetry XML file (optional; with --tag the matching XML is found automatically per prefix)')

    parser.add_argument('--tz', type=float, default=1.0,
                        help='Timezone offset in hours added to UTC for the HUD clock (default: 1 = CET winter)')
    parser.add_argument('--offset', type=float, default=0.0,
                        help='HUD time offset in seconds (default: 0.0). Only shifts the DISPLAYED clock, not the telemetry-to-video alignment.')
    parser.add_argument('--hwenc', type=str, default='cpu',
                        choices=['cpu', 'nvenc', 'amf', 'qsv'],
                        help='Hardware encoder (default: cpu)')
    parser.add_argument('--lossless', action='store_true',
                        help='Lossless encoding with libx264 crf 0 (CPU only)')
    parser.add_argument('--font', type=str, default=None,
                        help='Path to a .ttf/.otf font for the HUD (default: auto-detect)')
    parser.add_argument('--units', type=str, default='auto',
                        choices=['auto', 'kmh', 'mph'],
                        help='Speed unit shown in the HUD (default: auto = same unit as the XML)')
    parser.add_argument('--date-format', type=str, default='auto',
                        choices=['auto', 'dmy', 'mdy', 'ymd'],
                        help='Date order in the XML (default: auto-detect). Use mdy for US-style MM/DD/YYYY dates.')

    return parser.parse_args()


def find_prefixes_by_tag(base_dir: Path, tag: str):
    """Find all unique prefixes that contain _<tag> and have at least one camera mp4."""
    all_aliases = '|'.join(re.escape(a) for a in sum(CAM_ALIASES.values(), []))
    kam_words = '|'.join(re.escape(k) for k in KAMERA_WORDS)
    raw_alts = '|'.join(re.escape(r) for r in RAW_SUFFIXES)
    cam_re = re.compile(
        rf"^(\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}_{re.escape(tag)})"
        rf"_(?:{kam_words})_(?:{all_aliases})(?:{raw_alts})?\.mp4$",
        re.IGNORECASE
    )
    prefixes = set()
    for f in base_dir.iterdir():
        if not f.is_file():
            continue
        m = cam_re.match(f.name)
        if m:
            prefixes.add(m.group(1))
    return sorted(prefixes)


def auto_find_xml_for_prefix(base_dir: Path, prefix: str):
    """Locate an XML file for a given prefix."""
    exact = base_dir / f"{prefix}.xml"
    if exact.exists():
        return exact
    candidates = sorted(base_dir.glob(f"{prefix}*.xml"))
    return candidates[0] if candidates else None


def find_camera_file(base_dir: Path, prefix: str, direction: str, suffix_list):
    """Find one camera file, trying all known language aliases and raw/normal suffixes."""
    for kam_word in KAMERA_WORDS:
        for alias in CAM_ALIASES[direction]:
            for suffix in suffix_list:
                p = base_dir / f"{prefix}_{kam_word}_{alias}{suffix}.mp4"
                if p.exists():
                    return p
    return None


def find_video_set(base_dir: Path, prefix: str, suffix_list):
    """Locate all four camera files for one variant. Returns dict or None if any are missing."""
    result = {}
    for direction in ('front', 'rear', 'left', 'right'):
        p = find_camera_file(base_dir, prefix, direction, suffix_list)
        if p is None:
            return None
        result[direction] = p
    return result


def check_ffmpeg():
    """Check that ffmpeg + ffprobe are available; show useful error if not."""
    for tool in ('ffmpeg', 'ffprobe'):
        try:
            subprocess.run([tool, '-version'], capture_output=True, check=True)
        except FileNotFoundError:
            print(f"ERROR: '{tool}' not found in PATH. Install from https://ffmpeg.org/download.html")
            return False
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b'').decode(errors='replace').strip()
            print(f"ERROR: '{tool}' failed to run:\n{stderr}")
            return False
    return True


def get_video_duration(video_path):
    """Get video duration in seconds via ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'json',
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        return float(data['format']['duration'])
    except Exception as e:
        print(f"ERROR reading duration of {video_path}: {e}")
        return None


def build_dummy_telemetry(duration_seconds: float):
    """Generate one entry per second with 0 km/h when no XML is available."""
    n = max(1, int(duration_seconds) + 1)
    return [{'time': i, 'display_time': f'00:00:{i:02d}', 'velocity': 0.0} for i in range(n)]


def _find_text(entry, tags):
    """Return the stripped text of the first tag in `tags` present in `entry`."""
    for tag in tags:
        el = entry.find(tag)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return None


def _split_date(date_str):
    m = _DATE_RE.match(date_str)
    if not m:
        raise ValueError(f"unrecognized date {date_str!r}")
    return tuple(int(x) for x in m.groups())


def detect_date_order(date_strings):
    """Guess the field order ('dmy', 'mdy' or 'ymd') from all dates in a file.

    Returns (order, ambiguous). When no value above 12 shows whether
    slash-separated dates are DD/MM (UK/EU) or MM/DD (US), 'dmy' is assumed.
    The HUD only shows the time, and dates only matter for the offset
    between entries, so `ambiguous` is True only if the recording also
    spans more than one date (i.e. crosses midnight).
    """
    parts = [_split_date(s) for s in date_strings if _DATE_RE.match(s)]
    if not parts:
        return 'dmy', False
    if any(p[0] > 31 for p in parts):
        return 'ymd', False
    if any(p[0] > 12 for p in parts):
        return 'dmy', False
    if any(p[1] > 12 for p in parts):
        return 'mdy', False
    return 'dmy', any('/' in s for s in date_strings) and len(set(parts)) > 1


def _parse_date(date_str, order):
    a, b, c = _split_date(date_str)
    if order == 'ymd':
        y, m, d = a, b, c
    elif order == 'mdy':
        m, d, y = a, b, c
    else:
        d, m, y = a, b, c
    if y < 100:
        y += 2000
    return date(y, m, d)


def _parse_time(time_str):
    """Parse 24h (HH:MM:SS) or 12h (HH:MM:SS AM/PM) times."""
    s = time_str.strip().upper()
    for fmt in ('%H:%M:%S', '%H:%M:%S.%f', '%I:%M:%S %p', '%I:%M:%S%p'):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"unrecognized time {time_str!r}")


def _read_speed_kmh(entry):
    """Return (speed in km/h, source unit) for an ENTRY."""
    for tag, unit in SPEED_TAGS:
        text = _find_text(entry, (tag,))
        if text is not None:
            value = float(text.replace(',', '.'))
            return (value * KMH_PER_MPH if unit == 'mph' else value), unit
    raise ValueError(f"no speed tag (expected one of {', '.join(t for t, _ in SPEED_TAGS)})")


def parse_xml_telemetry(xml_path, tz_offset_hours, time_offset_seconds, date_format='auto'):
    """Parse XML telemetry (time + speed).

    Handles the car's USB export in any locale (DATE_UTC/TIME_UTC,
    VELOCITY_KMH or VELOCITY_MPH, '.', '/' or '-' separated dates) and the
    My BMW app export (DATE/TIME). Speeds are returned in km/h.

    Returns (telemetry_data, source_unit). Falls back to dummy data with
    source_unit None on error.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        entries = root.findall('.//ENTRY')

        if date_format == 'auto':
            dates = [d for d in (_find_text(e, DATE_TAGS) for e in entries) if d]
            date_order, ambiguous = detect_date_order(dates)
            if ambiguous:
                print("NOTE: recording crosses midnight and XML dates could be DD/MM/YYYY or "
                      "MM/DD/YYYY; assuming DD/MM/YYYY. Pass --date-format mdy if your car uses "
                      "US-style dates.")
        else:
            date_order = date_format

        telemetry_data = []
        skipped = 0
        first_error = None
        start_time = None
        source_unit = None

        for entry in entries:
            try:
                date_str = _find_text(entry, DATE_TAGS)
                time_str = _find_text(entry, TIME_TAGS)
                if date_str is None or time_str is None:
                    raise ValueError(f"missing date/time tag (expected {'/'.join(DATE_TAGS)} and {'/'.join(TIME_TAGS)})")

                dt_utc = datetime.combine(_parse_date(date_str, date_order), _parse_time(time_str))
                velocity_kmh, unit = _read_speed_kmh(entry)
                if source_unit is None:
                    source_unit = unit

                dt_local = dt_utc + timedelta(hours=tz_offset_hours, seconds=time_offset_seconds)

                if start_time is None:
                    start_time = dt_utc

                seconds_offset = (dt_utc - start_time).total_seconds()

                telemetry_data.append({
                    'time': seconds_offset,
                    'display_time': dt_local.strftime('%H:%M:%S'),
                    'velocity': velocity_kmh,
                })
            except Exception as e:
                skipped += 1
                if first_error is None:
                    first_error = e
                continue

        if skipped:
            print(f"NOTE: skipped {skipped} of {len(entries)} XML entries (first error: {first_error}).")

        if not telemetry_data:
            print("WARNING: No usable telemetry data found in XML. HUD will show dummy data (0 km/h).")
            return build_dummy_telemetry(60), None

        print(f"Telemetry: speed unit in XML = {UNIT_LABELS[source_unit]}, date order = {date_order}")
        print(f"First telemetry: time={telemetry_data[0]['display_time']}, speed={telemetry_data[0]['velocity']:.1f} km/h")
        print(f"Last telemetry:  time={telemetry_data[-1]['display_time']}, speed={telemetry_data[-1]['velocity']:.1f} km/h")

        return telemetry_data, source_unit

    except Exception as e:
        print(f"ERROR parsing XML: {e}")
        print("Using dummy data.")
        return build_dummy_telemetry(60), None


def create_hud_drawtext_filters(telemetry_data, duration, font_path, speed_unit='kmh'):
    """Build a drawtext filter chain that shows speed + time per second."""
    data_per_second = {}
    for data in telemetry_data:
        second = int(data['time'])
        if second not in data_per_second and second < duration:
            data_per_second[second] = data

    font_part = f"fontfile={_filter_path(font_path)}:" if font_path else ""
    unit_label = UNIT_LABELS[speed_unit]
    divisor = KMH_PER_MPH if speed_unit == 'mph' else 1.0

    filters = []
    for second in sorted(data_per_second.keys()):
        data = data_per_second[second]
        time_text = data['display_time'].replace(':', '\\:')
        speed_text = f"{data['velocity'] / divisor:.0f}"
        start_time = second
        end_time = second + 1

        speed_filter = (
            f"drawtext={font_part}"
            f"text='{speed_text} {unit_label}':"
            f"fontcolor=white:fontsize=70:"
            f"box=1:boxcolor=black@0.8:boxborderw=10:"
            f"x=w-tw-80:y=h-th-80:"
            f"enable='between(t,{start_time},{end_time})'"
        )

        time_filter = (
            f"drawtext={font_part}"
            f"text='{time_text}':"
            f"fontcolor=white:fontsize=70:"
            f"box=1:boxcolor=black@0.8:boxborderw=10:"
            f"x=w-tw-500:y=h-th-80:"
            f"enable='between(t,{start_time},{end_time})'"
        )

        filters.append(speed_filter)
        filters.append(time_filter)

    return ','.join(filters)


def build_ffmpeg_command(args, video_files, telemetry_data, output_file, min_duration, font_path, speed_unit='kmh'):
    """Build the ffmpeg command (inline -filter_complex). Very long videos
    (several thousand seconds) may exceed the OS command-line length limit;
    typical dashcam clips are short enough that inline is fine and avoids
    the buggy script-file parser."""
    if args.lossless:
        encoder_opts = ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '0']
    elif args.hwenc == 'nvenc':
        encoder_opts = ['-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '23', '-b:v', '0']
    elif args.hwenc == 'amf':
        encoder_opts = ['-c:v', 'h264_amf', '-quality', 'balanced', '-rc', 'cqp', '-qp_i', '23', '-qp_p', '23']
    elif args.hwenc == 'qsv':
        encoder_opts = ['-c:v', 'h264_qsv', '-preset', 'medium', '-global_quality', '23']
    else:
        encoder_opts = ['-c:v', 'libx264', '-preset', 'medium', '-crf', '23']

    print("Building dynamic HUD...")
    hud_filters = create_hud_drawtext_filters(telemetry_data, min_duration, font_path, speed_unit)

    scale_chain = ("fps=30,scale=1216:960:force_original_aspect_ratio=decrease,"
                   "pad=1216:960:(ow-iw)/2:(oh-ih)/2,format=yuv420p")

    filtergraph = (
        f"[0:v]trim=duration={min_duration},setpts=PTS-STARTPTS,{scale_chain}[front];"
        f"[1:v]trim=duration={min_duration},setpts=PTS-STARTPTS,{scale_chain}[rear];"
        f"[2:v]trim=duration={min_duration},setpts=PTS-STARTPTS,{scale_chain}[left];"
        f"[3:v]trim=duration={min_duration},setpts=PTS-STARTPTS,{scale_chain}[right];"
        f"color=c=black:s=3648x1920:r=30:d={min_duration}[bg];"
        f"[bg][front]overlay=x=1216:y=0[tmp1];"
        f"[tmp1][rear]overlay=x=1216:y=960[tmp2];"
        f"[tmp2][left]overlay=x=0:y=480[tmp3];"
        f"[tmp3][right]overlay=x=2432:y=480[video];"
        f"[video]{hud_filters}[out]"
    )

    cmd = [
        'ffmpeg',
        '-y',
        '-hide_banner',
        '-loglevel', 'warning',
        '-stats',
        '-i', str(video_files['front']),
        '-i', str(video_files['rear']),
        '-i', str(video_files['left']),
        '-i', str(video_files['right']),
        '-filter_complex', filtergraph,
        '-map', '[out]',
        '-map', '0:a?',
        '-c:a', 'aac',
        '-b:a', '192k',
        *encoder_opts,
        '-pix_fmt', 'yuv420p',
        '-fps_mode', 'cfr',
        '-movflags', '+faststart',
        '-max_muxing_queue_size', '1024',
        str(output_file)
    ]

    return cmd


def process_video_set(args, video_type, font_path):
    """Process one variant. Returns 'ok', 'missing', or 'error'."""
    base_dir = Path(args.dir)
    prefix = args.prefix
    suffix_list = RAW_SUFFIXES if video_type == 'RAW' else NORMAL_SUFFIXES

    video_files = find_video_set(base_dir, prefix, suffix_list)
    if video_files is None:
        return 'missing'

    print(f"\n{'='*60}")
    print(f"Processing {video_type} videos")
    print(f"{'='*60}")
    print("Found files:")
    for name, path in video_files.items():
        print(f"  {name:6s}: {path.name}")

    durations = {}
    for name, path in video_files.items():
        dur = get_video_duration(path)
        if dur is None:
            print(f"ERROR: could not read duration of {name}")
            return 'error'
        durations[name] = dur
        print(f"{name:8s}: {dur:.2f} seconds")

    min_duration = min(durations.values())
    print(f"\nShortest duration: {min_duration:.2f} seconds - output will be trimmed to this length.")

    xml_path = Path(args.xml) if getattr(args, "xml", None) else None
    source_unit = None
    if (xml_path is None) or (not xml_path.exists()):
        if xml_path is None:
            print("\nWARNING: No XML file provided/found - HUD will use dummy telemetry (0 km/h).")
        else:
            print(f"\nWARNING: XML file not found: {xml_path} - HUD will use dummy telemetry (0 km/h).")
        telemetry_data = build_dummy_telemetry(min_duration)
        print(f"Telemetry entries (dummy): {len(telemetry_data)}")
    else:
        print(f"\nParsing telemetry from: {xml_path}")
        telemetry_data, source_unit = parse_xml_telemetry(xml_path, args.tz, args.offset, args.date_format)
        print(f"Telemetry entries: {len(telemetry_data)}")

    speed_unit = args.units if args.units != 'auto' else (source_unit or 'kmh')
    print(f"HUD speed unit: {UNIT_LABELS[speed_unit]}")

    output_file = base_dir / f"{prefix}_{video_type}_LAYOUT_hud.mp4"
    print(f"\nEncoding with FFmpeg...")
    print(f"Hardware encoder: {args.hwenc}")
    print(f"Output: {output_file}")

    cmd = build_ffmpeg_command(args, video_files, telemetry_data, output_file, min_duration, font_path, speed_unit)

    try:
        subprocess.run(cmd, check=True)
        print(f"\n[OK] Created: {output_file}")
        return 'ok'
    except subprocess.CalledProcessError as e:
        print(f"\n[FAIL] FFmpeg encoding failed: {e}")
        return 'error'


def main():
    _ensure_utf8_stdout()

    print("BMW Dashcam Video Merger")
    print("=" * 60)

    args = parse_arguments()

    if not check_ffmpeg():
        return 1

    base_dir = Path(args.dir)
    if not base_dir.exists():
        print(f"ERROR: directory not found: {args.dir}")
        return 1

    font_path = args.font or detect_font()
    if font_path:
        print(f"Font: {font_path}")
    else:
        print("WARNING: No TTF font found. FFmpeg will use its default font "
              "(HUD may look worse). Use --font to specify a path.")

    if getattr(args, "prefix", None):
        prefixes = [args.prefix]
    else:
        prefixes = find_prefixes_by_tag(base_dir, args.tag)
        if not prefixes:
            print(f"ERROR: no matching videos for --tag {args.tag!r} in {base_dir.resolve()}.")
            print("Expected patterns:")
            print("  <DATE>_<TIME>_<TAG>_Kamera_<Vorn|Hinten|Links|Rechts>[_Rohdaten].mp4")
            print("  <DATE>_<TIME>_<TAG>_Camera_<Front|Rear|Left|Right>[_Raw].mp4")
            return 1

    print(f"\nDirectory: {base_dir.resolve()}")
    if getattr(args, "tag", None) and not getattr(args, "prefix", None):
        print(f"Tag: {args.tag}")
        print(f"Recordings found: {len(prefixes)}")
    else:
        print(f"Prefix: {prefixes[0]}")

    print(f"Timezone: UTC+{args.tz}")
    print(f"HUD offset: {args.offset} seconds")

    # Preserve the user-supplied XML; never overwrite it inside the loop.
    user_xml = args.xml

    overall_ok = True

    for pref in prefixes:
        args.prefix = pref

        if user_xml:
            xml_path = Path(user_xml)
            if not xml_path.is_absolute():
                xml_path = base_dir / xml_path
        else:
            xml_path = auto_find_xml_for_prefix(base_dir, pref)
        args.xml = str(xml_path) if xml_path else None

        print(f"\n{'#'*60}")
        print(f"RECORDING: {pref}")
        print(f"XML: {args.xml if args.xml else '(none)'}")
        print(f"{'#'*60}")

        results = {}
        for vt in ('RAW', 'NORMAL'):
            results[vt] = process_video_set(args, vt, font_path)

        print(f"\n{'='*60}")
        print("SUMMARY (this recording)")
        print(f"{'='*60}")
        labels = {
            'ok':      '[OK] Done',
            'missing': '[--] No videos present - skipped',
            'error':   '[FAIL] Failed',
        }
        for vt, status in results.items():
            print(f"{vt:6s}: {labels[status]}")

        statuses = list(results.values())
        if all(s == 'missing' for s in statuses):
            print("ERROR: neither RAW nor NORMAL videos found for this recording.")
            overall_ok = False
        elif any(s == 'error' for s in statuses):
            overall_ok = False

    if overall_ok:
        print("\n[OK] All available videos processed successfully!")
        return 0
    else:
        print("\n[FAIL] Some videos could not be created.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
