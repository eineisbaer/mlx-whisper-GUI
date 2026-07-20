#!/usr/bin/env bash
#
# One-shot setup for Transcriber.
#
#   ./install.sh            set up the virtualenv and dependencies
#   ./install.sh --build    also build dist/Transcriber.app
#   ./install.sh --fresh    delete an existing venv and start over
#   ./install.sh --yes      never ask, install missing system packages
#
set -euo pipefail

APP_ENTRY="transkribierer_app.py"
VENV="venv"
BUILD=0
FRESH=0
ASSUME_YES=0

# ---------------------------------------------------------------- output ----
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
    YLW=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
    B=""; DIM=""; RED=""; GRN=""; YLW=""; BLU=""; RST=""
fi

step()  { printf "\n%s==>%s %s%s%s\n" "$BLU" "$RST" "$B" "$*" "$RST"; }
ok()    { printf "  %s✓%s %s\n" "$GRN" "$RST" "$*"; }
info()  { printf "  %s·%s %s\n" "$DIM" "$RST" "$*"; }
warn()  { printf "  %s!%s %s\n" "$YLW" "$RST" "$*"; }
die()   { printf "\n%serror:%s %s\n\n" "$RED" "$RST" "$*" >&2; exit 1; }

ask() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    printf "  %s?%s %s [y/N] " "$YLW" "$RST" "$1"
    read -r reply </dev/tty || return 1
    [[ "$reply" =~ ^[Yy]$ ]]
}

# ------------------------------------------------------------------ args ----
while [ $# -gt 0 ]; do
    case "$1" in
        --build) BUILD=1 ;;
        --fresh) FRESH=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h)
            sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

cd "$(dirname "$0")"
[ -f "$APP_ENTRY" ] || die "$APP_ENTRY not found — run this from the repo root."

# ---------------------------------------------------------- environment ----
step "Checking the environment"

[ "$(uname -s)" = "Darwin" ] || die "This app is macOS only."
ok "macOS $(sw_vers -productVersion)"

if [ "$(uname -m)" != "arm64" ]; then
    die "Apple Silicon required. mlx-whisper needs Metal and will not run on Intel Macs."
fi
ok "Apple Silicon"

command -v brew >/dev/null 2>&1 || die \
"Homebrew is required for ffmpeg and the Tk bindings.
       Install it from https://brew.sh, then run this script again."
ok "Homebrew $(brew --version | head -n1 | awk '{print $2}')"

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 not found. Try: brew install python"
PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || die "Python 3.10 or newer required (found $PY_VER)."
ok "Python $PY_VER at $PYTHON"

# --------------------------------------------------------------- ffmpeg ----
step "Checking ffmpeg"

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    ok "ffmpeg and ffprobe present"
else
    warn "ffmpeg is missing. yt-dlp needs it to extract audio,"
    warn "and ffprobe supplies the length that drives the progress bar."
    if ask "Install it now with 'brew install ffmpeg'?"; then
        brew install ffmpeg
        ok "ffmpeg installed"
    else
        die "ffmpeg is required. Install it with: brew install ffmpeg"
    fi
fi

# ------------------------------------------------------------------ venv ----
step "Setting up the virtualenv"

create_venv() {
    rm -rf "$VENV"
    "$PYTHON" -m venv "$VENV"
}

if [ "$FRESH" -eq 1 ] && [ -d "$VENV" ]; then
    info "--fresh given, removing the existing venv"
    create_venv
    ok "created $VENV"
elif [ -d "$VENV" ]; then
    ok "reusing existing $VENV"
else
    create_venv
    ok "created $VENV"
fi

VPY="$VENV/bin/python"

# --------------------------------------------------------------- tkinter ----
step "Checking Tk bindings"

# Homebrew's Python does not bundle Tk. Critically, an existing venv will NOT
# pick up newly installed bindings, so the venv has to be rebuilt afterwards.
if "$VPY" -c 'import tkinter' >/dev/null 2>&1; then
    ok "tkinter available"
else
    warn "tkinter is missing — Homebrew ships Python without Tk."
    FORMULA="python-tk@$PY_VER"
    brew info "$FORMULA" >/dev/null 2>&1 || FORMULA="python-tk"
    if ask "Install '$FORMULA' and rebuild the venv?"; then
        brew install "$FORMULA"
        info "rebuilding the venv so it picks up the new bindings"
        create_venv
        "$VPY" -c 'import tkinter' >/dev/null 2>&1 \
            || die "tkinter still unavailable after installing $FORMULA.
       Check which python3 you are using: $PYTHON"
        ok "tkinter available"
    else
        die "tkinter is required. Install it with: brew install $FORMULA"
    fi
fi

# ---------------------------------------------------------- dependencies ----
step "Installing Python dependencies"

"$VPY" -m pip install --quiet --upgrade pip
if [ -f requirements.txt ]; then
    "$VPY" -m pip install --quiet -r requirements.txt
else
    "$VPY" -m pip install --quiet customtkinter tkinterdnd2 yt-dlp mlx-whisper py2app
fi
ok "dependencies installed"

for mod in customtkinter yt_dlp mlx_whisper; do
    "$VPY" -c "import $mod" >/dev/null 2>&1 \
        || die "$mod failed to import after installation."
done
ok "imports verified"

if "$VPY" -c 'import tkinterdnd2' >/dev/null 2>&1; then
    ok "drag & drop enabled"
else
    warn "tkinterdnd2 unavailable — the app runs, but without drag & drop"
fi

# --------------------------------------------------------------- launcher ----
step "Writing the launcher"

cat > run.sh <<'LAUNCHER'
#!/usr/bin/env bash
# Starts Transcriber. Generated by install.sh.
set -euo pipefail
cd "$(dirname "$0")"
exec venv/bin/python transkribierer_app.py "$@"
LAUNCHER
chmod +x run.sh
ok "run.sh created"

# ------------------------------------------------------------------ build ----
if [ "$BUILD" -eq 1 ]; then
    step "Building the .app bundle"
    rm -rf build dist
    "$VPY" setup.py py2app
    [ -d "dist/Transcriber.app" ] || die "py2app finished but dist/Transcriber.app is missing."
    ok "built dist/Transcriber.app"
    warn "The bundle calls yt-dlp, mlx_whisper and ffmpeg as external commands."
    warn "The app looks next to its own interpreter and in the Homebrew prefixes,"
    warn "so this normally works — but a bundle moved to another Mac will not"
    warn "find them unless those tools are installed there too."
fi

# ---------------------------------------------------------------- summary ----
printf "\n%s==>%s %sDone.%s\n\n" "$GRN" "$RST" "$B" "$RST"
echo "  Start the app:      ./run.sh"
if [ "$BUILD" -eq 1 ]; then
    echo "  Or open the bundle: open dist/Transcriber.app"
else
    echo "  Build a .app:       ./install.sh --build"
fi
echo ""
