"""Resumable downloads with progress callbacks (Hugging Face files, GitHub release assets)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tarfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__

log = logging.getLogger(__name__)

# progress(done_bytes, total_bytes_or_0, label)
ProgressFn = Callable[[int, int, str], None]

USER_AGENT = f"Tsuyaku/{__version__} (+https://github.com/Chunibyodev/tsuyaku)"


class DownloadCancelled(Exception):
    pass


@dataclass
class CancelToken:
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True

    def check(self) -> None:
        if self.cancelled:
            raise DownloadCancelled()


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, read=120.0),
        headers={"User-Agent": USER_AGENT},
    )


def download_file(
    url: str,
    dest: Path,
    *,
    progress: ProgressFn | None = None,
    label: str = "",
    cancel: CancelToken | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 4,
) -> Path:
    """Download url to dest, resuming a previous partial download (dest + '.part')."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    label = label or dest.name
    attempt = 0
    while True:
        attempt += 1
        try:
            _download_once(url, dest, part, progress, label, cancel, headers or {})
            return dest
        except DownloadCancelled:
            raise
        except (httpx.HTTPError, OSError) as exc:
            if attempt > retries:
                raise
            wait = 2**attempt
            log.warning("Download of %s failed (%s); retrying in %ss", label, exc, wait)
            time.sleep(wait)


def _download_once(url, dest, part, progress, label, cancel, headers) -> None:
    start = part.stat().st_size if part.exists() else 0
    req_headers = dict(headers)
    if start:
        req_headers["Range"] = f"bytes={start}-"
    with _client() as client, client.stream("GET", url, headers=req_headers) as resp:
        if resp.status_code == 416:  # already complete
            part.replace(dest)
            return
        resp.raise_for_status()
        if start and resp.status_code != 206:
            start = 0  # server ignored Range; start over
        total = int(resp.headers.get("Content-Length", "0") or 0)
        total = total + start if total else 0
        done = start
        mode = "ab" if start else "wb"
        last_report = 0.0
        with open(part, mode) as fh:
            for chunk in resp.iter_bytes(1 << 20):
                if cancel:
                    cancel.check()
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and now - last_report > 0.2:
                    last_report = now
                    progress(done, total, label)
        if progress:
            progress(done, total or done, label)
    part.replace(dest)


# ----------------------------------------------------------------------- Hugging Face

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co")


def hf_file_url(repo: str, filename: str, revision: str = "main") -> str:
    return f"{HF_ENDPOINT}/{repo}/resolve/{revision}/{filename}"


def hf_list_files(repo: str, revision: str = "main") -> list[tuple[str, int]]:
    with _client() as client:
        resp = client.get(f"{HF_ENDPOINT}/api/models/{repo}/tree/{revision}", params={"recursive": "1"})
        resp.raise_for_status()
        return [(f["path"], int(f.get("size") or 0)) for f in resp.json() if f.get("type") == "file"]


def hf_download_file(
    repo: str, filename: str, dest_dir: Path, *, progress: ProgressFn | None = None,
    cancel: CancelToken | None = None,
) -> Path:
    dest = dest_dir / filename
    if dest.exists():
        return dest
    return download_file(hf_file_url(repo, filename), dest, progress=progress, label=filename, cancel=cancel)


def hf_download_repo(
    repo: str, dest_dir: Path, *, progress: ProgressFn | None = None, cancel: CancelToken | None = None,
    skip: Callable[[str], bool] | None = None,
) -> Path:
    """Download every file of a (small) model repo, e.g. a CTranslate2 Whisper model."""
    marker = dest_dir / ".complete"
    if marker.exists():
        return dest_dir
    files = hf_list_files(repo)
    for name, _size in files:
        if name.startswith(".") or (skip and skip(name)):
            continue
        hf_download_file(repo, name, dest_dir, progress=progress, cancel=cancel)
    marker.write_text("ok")
    return dest_dir


def repo_dir_name(repo: str) -> str:
    return repo.replace("/", "--")


# ----------------------------------------------------------------------- GitHub releases


def github_latest_release(owner_repo: str) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:  # CI: avoid the low anonymous rate limit
        headers["Authorization"] = f"Bearer {token}"
    with _client() as client:
        resp = client.get(f"https://api.github.com/repos/{owner_repo}/releases/latest", headers=headers)
        resp.raise_for_status()
        return resp.json()


def github_find_asset(release: dict, pattern: str) -> tuple[str, str] | None:
    rx = re.compile(pattern)
    for asset in release.get("assets", []):
        if rx.search(asset["name"]):
            return asset["name"], asset["browser_download_url"]
    return None


# ----------------------------------------------------------------------- archives


def extract_archive(archive: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    elif name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest_dir, filter="data")
    else:
        raise ValueError(f"Unknown archive type: {archive}")
    return dest_dir


def remove_quietly(path: Path) -> None:
    """Delete a temporary file; on Windows it can still be locked briefly (antivirus, a
    finished extractor), which must not fail the install."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("could not delete %s: %s", path, exc)


def find_file(root: Path, name: str) -> Path | None:
    for path in root.rglob(name):
        if path.is_file():
            return path
    return None


def flatten_single_dir(dest_dir: Path) -> None:
    """If extraction produced exactly one top-level folder, move its contents up."""
    entries = [p for p in dest_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), dest_dir / child.name)
        inner.rmdir()
