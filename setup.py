"""
Builds the .app on your Mac.

Setup (in the same folder as transkribierer_app.py):
    python3 -m venv venv
    source venv/bin/activate
    pip install customtkinter tkinterdnd2 py2app yt-dlp mlx-whisper

Build:
    python3 setup.py py2app

Result: ./dist/Transcriber.app

For a fast development build (symlinks instead of a full bundle):
    python3 setup.py py2app -A
"""

import os
from setuptools import setup

APP = ['transkribierer_app.py']
DATA_FILES = []

# App icon. Regenerate with icon/make_icns.sh after changing the source art.
ICON = 'icon/Transcriber.icns' if os.path.isfile('icon/Transcriber.icns') else None

# customtkinter and tkinterdnd2 ship data files (themes, tkdnd binaries) that
# py2app does not pick up automatically, so point at their package folders.
#
# yt-dlp and mlx-whisper are NOT bundled: mlx is a native, namespace-style
# package that py2app's dependency scanner cannot graph. They are installed
# separately (install.sh uses pipx to put them in ~/.local/bin) and the app
# locates them there — see resolve_tool in transkribierer_app.py.
packages = ['customtkinter']
includes = ['tkinter']

try:
    import tkinterdnd2
    packages.append('tkinterdnd2')
    tkdnd_path = os.path.join(os.path.dirname(tkinterdnd2.__file__), 'tkdnd')
    if os.path.isdir(tkdnd_path):
        DATA_FILES.append(('tkinterdnd2/tkdnd', [
            os.path.join(tkdnd_path, f) for f in os.listdir(tkdnd_path)
            if os.path.isfile(os.path.join(tkdnd_path, f))
        ]))
except ImportError:
    print("Note: tkinterdnd2 not installed — building without drag & drop support.")

OPTIONS = {
    'argv_emulation': False,
    'packages': packages,
    'includes': includes,
    'plist': {
        'CFBundleName': 'Transcriber',
        'CFBundleDisplayName': 'Transcriber',
        'CFBundleIdentifier': 'com.eineisbaer.transcriber',
        'CFBundleVersion': '1.1.0',
        'CFBundleShortVersionString': '1.1.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
    },
}

if ICON:
    OPTIONS['iconfile'] = ICON

setup(
    app=APP,
    name='Transcriber',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
