# BMW Dashcam Video Merger

A small Python tool that takes the four camera videos a BMW dashcam writes to
the USB stick (front / rear / left / right) and combines them into a single
"cross" layout video with a HUD overlay showing the recorded speed and time.

It works with both German firmware (`..._Kamera_Vorn.mp4`) and English firmware
(`..._Camera_Front.mp4`), processes the normal recordings and the raw
("Rohdaten" / "Raw") recordings in one go, and runs on Windows, macOS, and
Linux.

---

## Features

- Merges the four BMW dashcam camera streams into one 3648x1920 layout video
- Burns a HUD with speed (km/h) and timestamp from the dashcam's XML telemetry
- Processes both normal and raw recordings in a single run, skipping whichever
  variant is missing
- Batch mode (`--tag`) processes every matching recording in a folder
- Hardware-accelerated encoding via NVIDIA NVENC, AMD AMF, or Intel QSV
- Optional fully lossless output (libx264 CRF 0)
- Cross-platform: Windows, macOS, Linux

---

## Requirements

- **Python 3.8+** (uses only the standard library, no `pip install` needed)
- **FFmpeg 4.4 or newer**, with both `ffmpeg` and `ffprobe` available in your
  `PATH`
- A TTF/OTF font for the HUD (auto-detected on most systems; see
  [Fonts](#fonts) below)

### Installing FFmpeg

| OS      | Command / Source                                                   |
|---------|--------------------------------------------------------------------|
| Windows | <https://www.gyan.dev/ffmpeg/builds/> (add `bin\` to PATH)         |
| macOS   | `brew install ffmpeg`                                              |
| Linux   | `sudo apt install ffmpeg` / `sudo dnf install ffmpeg` / etc.       |

Verify with:

```
ffmpeg -version
ffprobe -version
```

---

## What the script expects in the folder

The BMW saves each recording as four MP4 files plus an XML telemetry file. The
script recognizes these patterns (case-insensitive):

```
<YYYY-MM-DD>_<HH-MM-SS>_<TAG>_Kamera_Vorn.mp4
<YYYY-MM-DD>_<HH-MM-SS>_<TAG>_Kamera_Hinten.mp4
<YYYY-MM-DD>_<HH-MM-SS>_<TAG>_Kamera_Links.mp4
<YYYY-MM-DD>_<HH-MM-SS>_<TAG>_Kamera_Rechts.mp4
<YYYY-MM-DD>_<HH-MM-SS>_<TAG>.xml
```

Raw recordings have a `_Rohdaten` suffix before `.mp4`. English firmware uses
`Camera_Front/Rear/Left/Right` with a `_Raw` suffix. `<TAG>` is the recording
type from your iDrive setting, e.g. `Manuell`, `Manual`, `Automatik`, etc.

Either variant (normal, raw, or both) is fine; the script processes what it
finds and skips what is missing.

---

## Usage

### Process one specific recording

```
python bmw_dashcam_merger.py --dir "C:\path\to\recordings" --prefix 2026-05-13_07-36-36_Manuell
```

### Process every recording with a given tag in a folder (batch)

```
python bmw_dashcam_merger.py --dir "C:\path\to\recordings" --tag Manuell
```

This scans the folder, finds every `..._Manuell_..._Kamera_<dir>.mp4`, groups
them by date/time, and processes each recording in turn.

### Use a specific XML file

```
python bmw_dashcam_merger.py --prefix 2026-05-13_07-36-36_Manuell --xml my_telemetry.xml
```

If `--xml` is omitted, the script looks for `<prefix>.xml` next to the videos.
If no XML is found, the HUD falls back to dummy data (0 km/h, dummy clock).

### Hardware encoding

Append `--hwenc` to any of the commands above. Replace `<PREFIX>` with your
actual recording prefix (e.g. `2026-05-13_07-36-36_Manuell`):

```
# NVIDIA GPU
python bmw_dashcam_merger.py --prefix <PREFIX> --hwenc nvenc

# AMD GPU
python bmw_dashcam_merger.py --prefix <PREFIX> --hwenc amf

# Intel iGPU
python bmw_dashcam_merger.py --prefix <PREFIX> --hwenc qsv

# CPU (default)
python bmw_dashcam_merger.py --prefix <PREFIX> --hwenc cpu
```

### Lossless archive copy

```
python bmw_dashcam_merger.py --prefix <PREFIX> --lossless
```

Uses `libx264 -crf 0` (CPU only). Produces very large files but no quality
loss.

---

## All command-line options

| Option        | Default | Description                                                                                  |
|---------------|---------|----------------------------------------------------------------------------------------------|
| `--dir`       | `.`     | Folder containing the videos                                                                 |
| `--prefix`    | -       | Exact date/time/tag prefix to process (mutually exclusive with `--tag`)                      |
| `--tag`       | -       | Tag word to scan for in batch mode (e.g. `Manuell`, `Manual`)                                |
| `--xml`       | auto    | Telemetry XML file. With `--tag`, the matching `<prefix>.xml` is found automatically         |
| `--tz`        | `1.0`   | Timezone offset in hours added to UTC for the HUD clock (1 = CET winter)                     |
| `--offset`    | `0.0`   | Extra offset for the displayed clock, in seconds. Does NOT change telemetry-to-video sync    |
| `--hwenc`     | `cpu`   | One of `cpu` / `nvenc` / `amf` / `qsv`                                                       |
| `--lossless`  | off     | Use lossless `libx264 -crf 0` (CPU only)                                                     |
| `--font`      | auto    | Path to a TTF/OTF font for the HUD                                                           |

---

## Output

For each recording the script produces (where applicable):

```
<prefix>_NORMAL_LAYOUT_hud.mp4   # if normal videos were present
<prefix>_RAW_LAYOUT_hud.mp4      # if raw videos were present
```

Layout on a 3648x1920 canvas:

```
                    +---------------+
                    |               |
                    |     FRONT     |
                    |               |
    +---------------+---------------+---------------+
    |               |               |               |
    |     LEFT      |               |     RIGHT     |
    |               |               |               |
    +---------------+---------------+---------------+
                    |               |
                    |     REAR      |
                    |               |
                    +---------------+
```

Speed (large white text on dark box) is bottom-right, the timestamp sits to its
left. Audio from the front camera is kept if present.

---

## Fonts

The script auto-detects a bold sans-serif font on first run. If none of these
exist on your system, pass `--font /path/to/your.ttf`:

- Windows: `C:\Windows\Fonts\arialbd.ttf`
- macOS: `Arial Bold.ttf` or `Helvetica.ttc`
- Linux: DejaVu Sans Bold or Liberation Sans Bold (install with
  `sudo apt install fonts-dejavu` if needed)

---

## Adding more languages

If your BMW firmware writes the camera names in another language, edit the
`CAM_ALIASES` dict at the top of `bmw_dashcam_merger.py`:

```python
CAM_ALIASES = {
    'front': ['Vorn', 'Front', 'Avant'],          # add French
    'rear':  ['Hinten', 'Rear', 'Back', 'Arriere'],
    'left':  ['Links', 'Left', 'Gauche'],
    'right': ['Rechts', 'Right', 'Droite'],
}
```

The same applies to `KAMERA_WORDS` (e.g. add `'Camera'`/`'Caméra'`) and
`RAW_SUFFIXES`.

---

## Troubleshooting

**"ERROR: 'ffmpeg' / 'ffprobe' not found in PATH"**
FFmpeg isn't installed or not on your `PATH`. Install it and reopen the
terminal.

**"ERROR: no matching videos for --tag ..."**
The tag word doesn't appear in any filename, or your firmware uses a different
camera-name language. Either pass `--prefix` directly, or extend
`CAM_ALIASES` at the top of the script.

**Audio missing**
The script copies audio from the front camera only. If your front camera file
has no audio track, the output has none either.

**HUD looks wrong / wrong font**
Pass `--font` with the explicit path to a TTF you want to use.

**Time in the HUD is off by an hour**
Adjust `--tz` (e.g. `--tz 2` for CEST summer time).

**Output file is too large**
Drop `--lossless` and use the default encoder, or pick a hardware encoder via
`--hwenc`.

**FFmpeg encoding fails with very long videos (30+ minutes)**
The inline filtergraph may exceed the OS command-line length limit. Split the
recording into shorter clips, or open an issue.

---