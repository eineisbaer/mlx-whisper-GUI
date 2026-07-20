# Transcriber

A small macOS desktop app that turns YouTube videos, playlists and local media
files into text transcripts. Everything runs on-device: no API keys, no uploads,
no accounts. Transcription uses [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
which runs Whisper on Apple Silicon via Metal.

![status](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey)

## Features

- Paste a YouTube URL or drop audio/video files onto the window
- Playlist and channel URLs are expanded into individual jobs
- Job queue processed sequentially, with per-job status
- Separate progress bars for model download, media download and transcription,
  including a time estimate
- Output as `txt`, `srt`, `vtt` and/or `json`
- Word-level timestamps, `transcribe` vs. `translate`, language selection
- Configurable output filename template (yt-dlp syntax)
- Transcript preview with copy button, plus a history of recent runs
- Cancel a running job; the Mac is kept awake while the queue runs
- All settings persist between launches

## Requirements

**Hardware:** a Mac with Apple Silicon (M1 or newer). mlx-whisper depends on
Metal and will not run on Intel Macs.

**System dependencies** (Homebrew):

```bash
brew install ffmpeg          # required: audio extraction + duration probing
brew install python-tk       # required: Tcl/Tk bindings for the GUI
```

`ffmpeg` is not optional — yt-dlp uses it to extract audio, and `ffprobe` (part of
the same package) determines the media length, which is what drives the
transcription progress bar.

Homebrew's Python does not bundle Tk. If you see
`ModuleNotFoundError: No module named '_tkinter'`, install `python-tk` matching
your Python version (e.g. `brew install python-tk@3.13`) and recreate the
virtualenv afterwards — an existing venv will not pick up the new bindings.

**Python:** 3.10 or newer.

## Installation

```bash
git clone https://github.com/eineisbaer/mlx-whisper-GUI.git
cd mlx-whisper-GUI
./install.sh
```

The script checks the platform, installs `ffmpeg` and the Tk bindings via
Homebrew if they are missing, creates the virtualenv, installs the Python
dependencies, and writes a `run.sh` launcher. It asks before installing anything
system-wide; pass `--yes` to skip the prompts.

```bash
./run.sh                 # start the app
./install.sh --build     # also build dist/Transcriber.app
./install.sh --fresh     # rebuild the venv from scratch
```

### Manual installation

```bash
brew install ffmpeg python-tk
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 transkribierer_app.py
```

`tkinterdnd2` is optional. Without it the app still works, you just lose drag &
drop and the drop zone becomes a click-to-browse button.

## Building a standalone .app

```bash
source venv/bin/activate
python3 setup.py py2app
```

The bundle appears at `dist/Transcriber.app`.

**Known limitation:** py2app bundles the Python dependencies, but `yt-dlp`,
`mlx_whisper` and `ffmpeg` are invoked as external commands. The app resolves
them by looking next to its own interpreter and in the Homebrew prefixes
(`/opt/homebrew/bin`, `/usr/local/bin`) rather than trusting the inherited
`PATH`, which is what makes Finder-launched bundles work. A bundle copied to a
different Mac still needs those tools installed there.

For a faster development build that symlinks instead of copying:

```bash
python3 setup.py py2app -A
```

## Why there are no prebuilt downloads

There is no `.app` attached to releases, on purpose.

An app bundle you build yourself never gets the `com.apple.quarantine` attribute,
so Gatekeeper leaves it alone — on Apple Silicon macOS will even ad-hoc sign it
automatically on first launch. A bundle downloaded from GitHub does get that
attribute, and since macOS Sequoia the old Control-click → Open shortcut is gone:
users have to open System Settings → Privacy & Security, find the blocked app,
click *Open Anyway* within an hour of the failed launch, and authenticate as an
admin. For completely unsigned bundles that entry may not even appear.

Shipping a binary that opens on a double-click would mean joining the Apple
Developer Program (~$99/year), signing with a Developer ID certificate, and
submitting the bundle to Apple for notarization.

That effort would buy very little here: the bundle is not self-contained anyway.
`ffmpeg`, `yt-dlp` and `mlx_whisper` are invoked as external commands and have to
be installed separately regardless, and a Finder-launched bundle may not even find
them on its `PATH` (see above). Building from source is three commands and avoids
all of it.

## Model notes

Models are pulled from Hugging Face on first use and cached in
`~/.cache/huggingface/hub`. `whisper-large-v3-turbo` is roughly 1.6 GB;
`large-v3` is around 3 GB. The app shows a separate progress bar for this and
labels the model as `cached` once it is present locally.

Rough guidance:

| Model | Speed | Accuracy |
|---|---|---|
| `whisper-large-v3-turbo` | fast | very good — sensible default |
| `whisper-large-v3-mlx` | slow | best |
| `whisper-medium-mlx` | faster | good |
| `whisper-small-mlx` / `base` | fastest | noticeably weaker |

## Where things are stored

| Path | Contents |
|---|---|
| `~/Library/Application Support/Transcriber/settings.json` | UI settings |
| `~/Library/Application Support/Transcriber/history.json` | recent runs |
| `~/.cache/huggingface/hub` | downloaded Whisper models |

## Not included

Speaker diarization ("who said what") is out of scope. Whisper does not do it;
it would need something like `pyannote.audio`, which requires a Hugging Face
account, accepting a model licence, and a PyTorch dependency.

## Licence

MIT
