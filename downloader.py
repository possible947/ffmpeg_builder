"""Download management with progress tracking."""
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests
from tqdm import tqdm


ProgressCB = Callable[[int, int], None]


class Downloader:
    """Downloads files with progress tracking."""

    def __init__(self, packages_dir: Path):
        """Initialize downloader.

        Args:
            packages_dir: Directory for downloaded files.
        """
        self.packages_dir = packages_dir
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def download(
        self,
        url: str,
        filename: Optional[str] = None,
        max_retries: int = 3,
        show_progress: bool = True,
        progress_cb: Optional[ProgressCB] = None,
    ) -> Path:
        """Download a file with progress bar.

        Args:
            url: Download URL.
            filename: Target filename. If None, extracted from URL.
            max_retries: Maximum retry attempts.
            show_progress: Whether to render a tqdm progress bar.
            progress_cb: Optional callback receiving (downloaded_bytes, total_bytes)
                on each chunk. Takes precedence over the tqdm bar.

        Returns:
            Path to downloaded file.

        Raises:
            RuntimeError: If download fails after all retries.
        """
        if filename is None:
            filename = url.split("/")[-1].split("?")[0]

        target_path = self.packages_dir / filename
        lock = self._get_lock(filename)

        with lock:
            if target_path.exists() and target_path.stat().st_size > 0:
                return target_path

            candidate_urls = self._candidate_urls(url)

            for attempt in range(max_retries):
                last_error: Optional[Exception] = None
                try:
                    for candidate_url in candidate_urls:
                        try:
                            self._download_file(candidate_url, target_path, show_progress, progress_cb)
                            return target_path
                        except Exception as candidate_error:
                            last_error = candidate_error
                            part_path = target_path.with_name(f"{target_path.name}.part")
                            if part_path.exists():
                                part_path.unlink()
                    if last_error is not None:
                        raise last_error
                except Exception as e:
                    part_path = target_path.with_name(f"{target_path.name}.part")
                    if part_path.exists():
                        part_path.unlink()
                    if attempt < max_retries - 1:
                        if show_progress and progress_cb is None:
                            print(f"Download failed (attempt {attempt + 1}/{max_retries}): {e}")
                            print("Retrying in 10 seconds...")
                        time.sleep(10)
                    else:
                        raise RuntimeError(
                            f"Failed to download {url} after {max_retries} attempts: {e}"
                        )

            raise RuntimeError(f"Failed to download {url}")

    @staticmethod
    def _candidate_urls(url: str) -> List[str]:
        """Build a prioritized list of candidate URLs for one artifact.

        Keeps the original HTTPS URL first and adds Xiph/OSUOSL HTTP fallbacks
        for environments where TLS chain verification fails on that mirror.
        """
        candidates = [url]

        if url.startswith("https://ftp.osuosl.org/"):
            candidates.append("http://ftp.osuosl.org/" + url[len("https://ftp.osuosl.org/"):])

        if url.startswith("https://downloads.xiph.org/releases/"):
            rel_path = url.split("/releases/", 1)[1]
            candidates.append(f"http://ftp.osuosl.org/pub/xiph/releases/{rel_path}")

        return candidates

    def _get_lock(self, filename: str) -> threading.Lock:
        with self._locks_guard:
            if filename not in self._locks:
                self._locks[filename] = threading.Lock()
            return self._locks[filename]

    def _download_file(
        self,
        url: str,
        target_path: Path,
        show_progress: bool,
        progress_cb: Optional[ProgressCB] = None,
    ) -> None:
        """Download a single file.

        Args:
            url: Download URL.
            target_path: Target file path.
            show_progress: Whether to render a tqdm progress bar.
            progress_cb: Optional callback receiving (downloaded_bytes, total_bytes)
                on each chunk. When provided, the tqdm bar is suppressed.
        """
        part_path = target_path.with_name(f"{target_path.name}.part")
        response = requests.get(url, stream=True, timeout=300)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        use_tqdm = show_progress and progress_cb is None
        progress = tqdm(
            total=total_size,
            unit="B",
            unit_scale=True,
            desc=target_path.name,
            leave=False,
            disable=not use_tqdm,
        )

        downloaded = 0
        with open(part_path, "wb") as f:
            with progress as pbar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))
                        downloaded += len(chunk)
                        if progress_cb is not None:
                            try:
                                progress_cb(downloaded, total_size)
                            except Exception:
                                pass

        if part_path.stat().st_size == 0:
            part_path.unlink()
            raise RuntimeError(f"Downloaded file is empty: {target_path}")

        part_path.replace(target_path)


class AsyncDownloadManager:
    """Background source archive download manager."""

    def __init__(
        self,
        downloader: Downloader,
        max_workers: int,
        on_status: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
    ):
        """Initialize async download manager.

        Args:
            downloader: Shared downloader instance.
            max_workers: Maximum concurrent downloads.
            on_status: Optional status callback receiving component name and status.
            on_log: Optional message callback.
            on_progress: Optional per-component progress callback receiving
                (component_name, downloaded_bytes, total_bytes).
        """
        self.downloader = downloader
        self.max_workers = max(1, int(max_workers))
        self.on_status = on_status
        self.on_log = on_log
        self.on_progress = on_progress
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.futures: Dict[str, Future] = {}
        self._lock = threading.Lock()

    def prefetch(self, components: Iterable[Any]) -> None:
        """Queue component archive downloads.

        Args:
            components: Components to prefetch.
        """
        for component in components:
            self.schedule(component)

    def schedule(self, component: Any) -> None:
        """Queue one component archive download.

        Args:
            component: Component to prefetch.
        """
        filename = component.get_archive_filename()
        target_path = self.downloader.packages_dir / filename
        if target_path.exists() and target_path.stat().st_size > 0:
            return

        with self._lock:
            future = self.futures.get(filename)
            if future is not None and not future.done():
                return
            component_name = component.name
            url = component.get_url()
            progress_cb = self._make_progress_cb(component_name)
            future = self.executor.submit(
                self.downloader.download,
                url,
                filename,
                3,
                False,
                progress_cb,
            )
            self.futures[filename] = future
            if self.on_status is not None:
                self.on_status(component_name, "downloading")
            if self.on_log is not None:
                self.on_log(f"Queued download for {component_name}")
            future.add_done_callback(
                lambda done, name=component_name: self._download_done(name, done)
            )

    def _make_progress_cb(self, component_name: str) -> Optional[ProgressCB]:
        if self.on_progress is None:
            return None

        last_emit = [0.0]
        min_interval = 0.25

        def _cb(downloaded: int, total: int) -> None:
            now = time.monotonic()
            if total <= 0:
                return
            if now - last_emit[0] < min_interval and downloaded < total:
                return
            last_emit[0] = now
            try:
                self.on_progress(component_name, downloaded, total)
            except Exception:
                pass

        return _cb

    def _download_done(self, component_name: str, future: Future) -> None:
        if future.cancelled():
            return
        error = future.exception()
        if error is None:
            if self.on_status is not None:
                self.on_status(component_name, "pending")
            if self.on_log is not None:
                self.on_log(f"Downloaded {component_name}")
        elif self.on_log is not None:
            self.on_log(f"Download failed for {component_name}: {error}")

    def get(self, component: Any) -> Path:
        """Return component archive, waiting for background download if needed.

        Args:
            component: Component to get archive for.

        Returns:
            Path to downloaded archive.
        """
        filename = component.get_archive_filename()
        with self._lock:
            future = self.futures.get(filename)

        if future is None:
            if self.on_status is not None:
                self.on_status(component.name, "downloading")
            if self.on_log is not None:
                self.on_log(f"Downloading {component.name}")
            progress_cb = self._make_progress_cb(component.name)
            return self.downloader.download(
                component.get_url(),
                filename,
                show_progress=False,
                progress_cb=progress_cb,
            )

        try:
            return future.result()
        except Exception:
            with self._lock:
                if self.futures.get(filename) is future:
                    del self.futures[filename]
            raise

    def retry(self, component: Any) -> None:
        """Queue a fresh download after a failed attempt.

        Args:
            component: Component to retry.
        """
        filename = component.get_archive_filename()
        with self._lock:
            future = self.futures.pop(filename, None)
        if future is not None and not future.done():
            future.cancel()
        self.schedule(component)

    def shutdown(self, wait: bool = True) -> None:
        """Stop background download workers.

        Args:
            wait: Whether to wait for running downloads.
        """
        try:
            self.executor.shutdown(wait=wait, cancel_futures=not wait)
        except TypeError:
            self.executor.shutdown(wait=wait)
