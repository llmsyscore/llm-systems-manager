# PyInstaller spec for the single-file agent binary (#386).
# Build from repo root: pyinstaller --clean agent/llm-systems-agent.spec
import sys

sys.path.insert(0, SPECPATH)

# Static imports cover providers/collectors; only the lazy imports
# (influxdb_client in _probe_influxdb, uvicorn's optional speedups) need naming.
hiddenimports = [
    "influxdb_client", "influxdb_client.client.write_api",
    "uvloop", "httptools", "websockets",
]

a = Analysis(
    [f"{SPECPATH}/llm-systems-agent.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="llm-systems-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
