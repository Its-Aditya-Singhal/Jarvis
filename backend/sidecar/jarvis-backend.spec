# PyInstaller spec for the backend sidecar: a folder (not one file, which would unpack
# gigabytes on every launch) that scripts/build_dmg.sh copies into
# JARVIS.app/Contents/Resources/backend/.
#
#   cd backend && .venv/bin/pyinstaller --noconfirm --distpath dist --workpath build sidecar/jarvis-backend.spec
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

BACKEND = os.path.dirname(SPECPATH)  # noqa: F821 (defined by PyInstaller)
datas = [(os.path.join(BACKEND, "jarvis", "model_manifest.json"), "jarvis"),
         (os.path.join(BACKEND, "jarvis", "auth", "fusion", "default_model.json"), "jarvis/auth/fusion")]
binaries = []
hiddenimports = collect_submodules("jarvis") + [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on", "keyring.backends.macOS",
]

# packages that load data files, native libraries or submodules by name at runtime
PACKAGES = [
    "insightface", "onnxruntime", "cv2", "speechbrain", "silero_vad", "faster_whisper", "ctranslate2", "tokenizers",
    "kokoro_onnx", "espeakng_loader", "phonemizer", "language_tags", "segments", "csvw", "indic_transliteration",
    "sounddevice", "_sounddevice_data", "soundfile", "_soundfile_data", "torchaudio", "sentencepiece", "rapidfuzz",
    "hyperpyyaml",
]
if sys.platform == "darwin":
    PACKAGES += ["mlx", "mlx_whisper", "AppKit", "AVFoundation", "Foundation", "objc"]

for pkg in PACKAGES:
    try:
        d, b, h = collect_all(pkg)
    except Exception:  # an optional package that isn't installed
        continue
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(SPECPATH, "run_backend.py")],  # noqa: F821
    pathex=[BACKEND],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "pytest", "mypy", "ruff"],
    # speechbrain and torch read their own source files at runtime (inspect / lazy imports)
    module_collection_mode={"speechbrain": "pyz+py", "torch": "pyz+py", "insightface": "pyz+py"},
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jarvis-backend",
    console=True,  # no window of its own: the desktop shell starts it
    strip=False,
    upx=False,
    target_arch="arm64" if sys.platform == "darwin" else None,
    codesign_identity=None,  # signed together with the app by build_dmg.sh
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="jarvis-backend")
