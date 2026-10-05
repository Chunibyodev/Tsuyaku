"""Connect the Firefox extension to this Tsuyaku install, and package the extension.

Firefox finds native messaging hosts through a small JSON manifest: on Windows its path is in the
registry (HKCU\\Software\\Mozilla\\NativeMessagingHosts\\tsuyaku), on Linux and macOS it sits in a
fixed folder. The manifest points at the `tsuyaku-host` program of this Python environment.
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from importlib import resources
from pathlib import Path

from .. import paths

HOST_NAME = "tsuyaku"
EXTENSION_ID = "tsuyaku@chunibyo.dev"
_REG_KEY = r"Software\Mozilla\NativeMessagingHosts" + "\\" + HOST_NAME


def extension_dir() -> Path:
    """The extension's source folder (load its manifest.json in about:debugging)."""
    return Path(str(resources.files("tsuyaku").joinpath("extension")))


def extension_version() -> str:
    return json.loads((extension_dir() / "manifest.json").read_text(encoding="utf-8"))["version"]


def host_program() -> Path:
    """`tsuyaku-host` next to this environment's Python (created by `uv sync`)."""
    name = paths.exe_name("tsuyaku-host")
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return candidate.absolute()
    found = shutil.which(name)
    if found:
        return Path(found).absolute()
    raise FileNotFoundError(f"{name} was not found next to {sys.executable}. Run install.bat / update.bat "
                            "(or `uv sync`) so it gets created, then try again.")


def manifest_dir() -> Path:
    """Where Firefox looks for the manifest (per user)."""
    home = Path.home()
    if sys.platform == "win32":
        return paths.data_dir() / "firefox"  # any folder: the registry points at it
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
    return home / ".mozilla" / "native-messaging-hosts"


def host_manifest(program: Path) -> dict:
    return {
        "name": HOST_NAME,
        "description": "Tsuyaku: local speech recognition and translation for the Tsuyaku extension",
        "path": str(program),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }


def register(program: Path | None = None, directory: Path | None = None, registry: bool = True) -> Path:
    """Write the manifest (and the registry entry on Windows). Returns the manifest path."""
    program = program or host_program()
    directory = directory or manifest_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{HOST_NAME}.json"
    path.write_text(json.dumps(host_manifest(program), indent=2), encoding="utf-8")
    if registry and sys.platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))
    return path


def unregister(directory: Path | None = None, registry: bool = True) -> None:
    path = (directory or manifest_dir()) / f"{HOST_NAME}.json"
    path.unlink(missing_ok=True)
    if registry and sys.platform == "win32":
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _REG_KEY)
        except OSError:
            pass


def registered_manifest() -> Path | None:
    """The manifest Firefox would use, if it exists."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as key:
                path = Path(winreg.QueryValue(key, None))
        except OSError:
            return None
    else:
        path = manifest_dir() / f"{HOST_NAME}.json"
    return path if path.exists() else None


def build_xpi(dest: Path | None = None) -> Path:
    """Zip the extension folder into an .xpi (unsigned)."""
    src = extension_dir()
    dest = dest or paths.data_dir() / "firefox" / f"tsuyaku-firefox-{extension_version()}.xpi"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            rel = f.relative_to(src)
            if f.is_file() and not any(part.startswith((".", "__")) for part in rel.parts):
                zf.write(f, rel.as_posix())
    tmp.replace(dest)
    return dest
