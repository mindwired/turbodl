# Built-in imports
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from hashlib import new as hashlib_new
from math import ceil, log2, sqrt
from mimetypes import guess_extension as guess_mimetype_extension
from mmap import mmap
from os import PathLike, ftruncate
from pathlib import Path
from shutil import disk_usage
from threading import Lock
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from urllib.parse import unquote, urlparse

# Third-party imports
from httpx import Client, HTTPStatusError, Limits
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from tenacity import retry, stop_after_attempt, wait_exponential

# Local imports
from .exceptions import DownloadError, HashVerificationError, InsufficientSpaceError, RequestError
from .utils import ChunkBuffer


class TurboDL:
    """
    A class for downloading direct download URLs.
    """

    def __init__(
        self,
        max_connections: Union[int, Literal['auto']] = 'auto',
        connection_speed: float = 80,
        overwrite: bool = True,
        show_optimization_progress_bar: bool = True,
        show_progress_bar: bool = True,
        custom_headers: Optional[Dict[Any, Any]] = None,
        timeout: Optional[int] = None,
    ) -> None:
        """
        Initialize the class with the required settings for downloading a file.

        Args:
            max_connections: The maximum number of connections to use for downloading the file. (default: 'auto')
            connection_speed: Your connection speed in Mbps (megabits per second). (default: 80)
            overwrite: Overwrite the file if it already exists. Otherwise, a "_1", "_2", etc. suffix will be added. (default: True)
            show_optimization_progress_bar: Show or hide the initial optimization progress bar. (default: True)
            show_progress_bar: Show or hide the download progress bar. (default: True)
            custom_headers: Custom headers to include in the request. If None, default headers will be used. Imutable headers are 'Accept-Encoding', 'Connection' and 'Range'. (default: None)
            timeout: Timeout in seconds for the download process. Or None for no timeout. (default: None)

        Raises:
            ValueError: If max_connections is not between 1 and 32 or connection_speed is not positive.
        """

        with Progress(
            SpinnerColumn(spinner_name='dots', style='bold cyan'),
            TextColumn('[bold cyan]Generating the best settings...', justify='left'),
            BarColumn(bar_width=40, style='cyan', complete_style='green'),
            TimeRemainingColumn(),
            TextColumn('[bold][progress.percentage]{task.percentage:>3.0f}%'),
            transient=True,
            disable=not show_optimization_progress_bar,
        ) as progress:
            task = progress.add_task('', total=100)

            self._max_connections: Union[int, Literal['auto']] = max_connections
            self._connection_speed: int = connection_speed

            if isinstance(self._max_connections, int):
                if not 1 <= self._max_connections <= 32:
                    raise ValueError('max_connections must be between 1 and 32')

            if self._connection_speed <= 0:
                raise ValueError('connection_speed must be positive')

            self._overwrite: bool = overwrite
            self._show_progress_bar: bool = show_progress_bar
            self._timeout: Optional[int] = timeout

            self._custom_headers: Dict[Any, Any] = {
                'Accept': '*/*',
                'Accept-Encoding': 'identity',
                'Connection': 'keep-alive',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            }

            if custom_headers:
                for key, value in custom_headers.items():
                    if key.title() not in ['Accept-Encoding', 'Range', 'Connection']:
                        self._custom_headers[key.title()] = value

            self._client: Client = Client(
                headers=self._custom_headers,
                follow_redirects=True,
                verify=True,
                http2=True,
                limits=Limits(max_keepalive_connections=32, max_connections=64, keepalive_expiry=30.0),
                timeout=self._timeout,
            )

            self.output_path: str = None

            progress.update(task, advance=100)

    def _is_enough_space_to_download(self, path: Union[str, PathLike], size: int) -> bool:
        """
        Checks if there is enough space to download the file.

        Args:
            path: The path to save the downloaded file to.
            size: The size of the file in bytes.

        Returns:
            True if there is enough space, False otherwise.
        """

        required_space = size + (1 * 1024 * 1024 * 1024)
        _, _, free = disk_usage(Path(path).as_posix())

        if free < required_space:
            return False

        return True

    @lru_cache(maxsize=256)
    def _calculate_connections(self, file_size: int, connection_speed: Union[float, Literal['auto']]) -> int:
        """
        Calculates the optimal number of connections based on file size and connection speed.

        Uses a sophisticated formula that considers:
        - File size scaling using logarithmic growth
        - Connection speed with square root scaling
        - System resource optimization
        - Network overhead management

        Formula:
        \[ connections = 2 \cdot \left\lceil\frac{\beta \cdot \log_2(1 + \frac{S}{M}) \cdot \sqrt{\frac{V}{100}}}{2}\right\rceil \]

        Where:
        - S: File size in MB
        - V: Connection speed in Mbps
        - M: Base size factor (1 MB)
        - β: Dynamic coefficient (5.6)

        Args:
            file_size: The size of the file in bytes.
            connection_speed: Your connection speed in Mbps.

        Returns:
            Estimated optimal number of connections, capped between 2 and 32.
        """

        if self._max_connections != 'auto':
            return self._max_connections

        file_size_mb = file_size / (1024 * 1024)
        speed = 80.0 if connection_speed == 'auto' else float(connection_speed)

        beta = 5.6
        base_size = 1.0
        conn_float = beta * log2(1 + file_size_mb / base_size) * sqrt(speed / 100)

        return max(2, min(32, 2 * ceil(conn_float / 2)))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5), reraise=True)
    def _get_file_info(self, url: str) -> Tuple[int, str, str]:
        try:
            with self._client.stream('HEAD', url, headers=self._custom_headers, timeout=self._timeout) as r:
                r.raise_for_status()

                headers = r.headers
                content_length = int(headers.get('content-length', 0))
                content_type = headers.get('content-type', 'application/octet-stream').split(';')[0].strip()

                if content_disposition := headers.get('content-disposition'):
                    if 'filename*=' in content_disposition:
                        filename = content_disposition.split('filename*=')[-1].split("'")[-1]
                    elif 'filename=' in content_disposition:
                        filename = content_disposition.split('filename=')[-1].strip('"\'')
                    else:
                        filename = None
                else:
                    filename = None

                if not filename:
                    filename = (
                        Path(unquote(urlparse(url).path)).name or f'downloaded_file{guess_mimetype_extension(content_type) or ""}'
                    )

                return (content_length, content_type, filename)
        except HTTPStatusError as e:
            raise RequestError(f'Request failed with status code "{e.response.status_code}"') from e

    @lru_cache(maxsize=256)
    def _get_chunk_ranges(self, total_size: int) -> List[Tuple[int, int]]:
        if total_size == 0:
            return [(0, 0)]

        connections = self._calculate_connections(total_size, self._connection_speed)
        chunk_size = ceil(total_size / connections)

        ranges = []
        start = 0

        while total_size > 0:
            current_chunk = min(chunk_size, total_size)
            end = start + current_chunk - 1
            ranges.append((start, end))
            start = end + 1
            total_size -= current_chunk

        return ranges

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True)
    def _download_chunk(self, url: str, start: int, end: int, progress: Progress, task_id: int) -> bytes:
        headers = {**self._custom_headers}

        if end > 0:
            headers['Range'] = f'bytes={start}-{end}'

        try:
            with self._client.stream('GET', url, headers=headers) as r:
                r.raise_for_status()

                chunk = b''

                for data in r.iter_bytes(chunk_size=8192):
                    chunk += data
                    progress.update(task_id, advance=len(data))

                return chunk
        except HTTPStatusError as e:
            raise DownloadError(f'An error occurred while downloading chunk: {str(e)}') from e

    def _download_with_buffer(
        self, url: str, output_path: Union[str, PathLike], total_size: int, progress: Progress, task_id: int
    ) -> None:
        chunk_buffers = {}
        write_positions = {}

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).touch()

        def download_worker(start: int, end: int, chunk_id: int) -> None:
            chunk_buffers[chunk_id] = ChunkBuffer()
            headers = {**self._custom_headers}

            if end > 0:
                headers['Range'] = f'bytes={start}-{end}'

            try:
                with self._client.stream('GET', url, headers=headers) as r:
                    r.raise_for_status()

                    for data in r.iter_bytes(chunk_size=1024 * 1024):
                        if complete_chunk := chunk_buffers[chunk_id].write(data):
                            write_to_file(complete_chunk, start + write_positions[chunk_id])
                            write_positions[chunk_id] += len(complete_chunk)

                        progress.update(task_id, advance=len(data))

                    if remaining := chunk_buffers[chunk_id].current_buffer.getvalue():
                        write_to_file(remaining, start + write_positions[chunk_id])
            except Exception as e:
                raise DownloadError(f'Download error: {str(e)}')

        def write_to_file(data: bytes, position: int) -> None:
            with Path(output_path).open('r+b') as f:
                current_size = f.seek(0, 2)

                if current_size < total_size:
                    ftruncate(f.fileno(), total_size)

                with mmap(f.fileno(), 0) as mm:
                    mm[position : position + len(data)] = data
                    mm.flush()

        ranges = self._get_chunk_ranges(total_size)

        for i, (_, _) in enumerate(ranges):
            write_positions[i] = 0

        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [executor.submit(download_worker, start, end, i) for i, (start, end) in enumerate(ranges)]

            for future in futures:
                future.result()

    def _download_direct(
        self, url: str, output_path: Union[str, PathLike], total_size: int, progress: Progress, task_id: int
    ) -> None:
        write_lock = Lock()
        futures = []

        def download_worker(start: int, end: int) -> None:
            headers = {**self._custom_headers}
            if end > 0:
                headers['Range'] = f'bytes={start}-{end}'

            try:
                with self._client.stream('GET', url, headers=headers) as r:
                    r.raise_for_status()
                    with write_lock:
                        with open(output_path, 'r+b') as f:
                            f.seek(start)
                            for data in r.iter_bytes(chunk_size=8192):
                                f.write(data)
                                progress.update(task_id, advance=len(data))
            except Exception as e:
                raise DownloadError(f'Download error: {str(e)}')

        ranges = self._get_chunk_ranges(total_size)
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [executor.submit(download_worker, start, end) for start, end in ranges]

            for future in futures:
                future.result()

    def download(
        self,
        url: str,
        output_path: Union[str, PathLike] = Path.cwd(),
        pre_allocate_space: bool = False,
        use_ram_buffer: bool = True,
        expected_hash: Optional[str] = None,
        hash_type: Literal[
            'md5',
            'sha1',
            'sha224',
            'sha256',
            'sha384',
            'sha512',
            'blake2b',
            'blake2s',
            'sha3_224',
            'sha3_256',
            'sha3_384',
            'sha3_512',
            'shake_128',
            'shake_256',
        ] = 'md5',
    ) -> None:
        """
        Downloads a file from the provided URL to the output file path.

        - If the output_path is a directory, the file name will be generated from the server response.
        - If the output_path is a file, the file will be saved with the provided name.
        - If not provided, the file will be saved to the current working directory.

        Args:
            url: The download URL to download the file from. (required)
            output_path: The path to save the downloaded file to. If the path is a directory, the file name will be generated from the server response. If the path is a file, the file will be saved with the provided name. If not provided, the file will be saved to the current working directory. (default: Path.cwd())
            pre_allocate_space: Whether to pre-allocate space for the file, useful to avoid disk fragmentation. (default: False)
            use_ram_buffer: Whether to use a RAM buffer to download the file. (default: True)
            expected_hash: The expected hash of the downloaded file. If provided, the hash will be verified after the download is complete. (default: None)
            hash_type: The hash type to use for the hash verification. (default: 'md5')

        Raises:
            DownloadError: If an error occurs while downloading the file.
            HashVerificationError: If the hash of the downloaded file does not match the expected hash.
            InsufficientSpaceError: If there is not enough space to download the file.
            RequestError: If an error occurs while getting file info.
        """

        output_path = Path(output_path)

        try:
            total_size, _, suggested_filename = self._get_file_info(url)
        except RequestError as e:
            raise DownloadError(f'An error occurred while getting file info: {str(e)}') from e

        if not self._is_enough_space_to_download(output_path, total_size):
            raise InsufficientSpaceError(f'Not enough space to download {total_size} bytes to "{output_path.as_posix()}"')

        try:
            if output_path.is_dir():
                output_path = Path(output_path, suggested_filename)

            if not self._overwrite:
                base_name = output_path.stem
                extension = output_path.suffix
                counter = 1

                while output_path.exists():
                    output_path = Path(output_path.parent, f'{base_name}_{counter}{extension}')
                    counter += 1

            if pre_allocate_space and total_size > 0:
                with output_path.open('wb') as f:
                    f.truncate(total_size)

            self.output_path = output_path.as_posix()

            progress_columns = [
                TextColumn(output_path.name, style='magenta'),
                BarColumn(style='bold white', complete_style='bold red', finished_style='bold green'),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                TextColumn('[bold][progress.percentage]{task.percentage:>3.0f}%'),
            ]

            with Progress(*progress_columns, disable=not self._show_progress_bar) as progress:
                task_id = progress.add_task('download', total=total_size or 100, filename=output_path.name)

                if total_size == 0:
                    Path(output_path).write_bytes(self._download_chunk(url, 0, 0, progress, task_id))
                else:
                    if use_ram_buffer:
                        self._download_with_buffer(url, output_path, total_size, progress, task_id)
                    else:
                        self._download_direct(url, output_path, total_size, progress, task_id)
        except KeyboardInterrupt:
            Path(output_path).unlink(missing_ok=True)
            self.output_path = None
            return None
        except Exception as e:
            raise DownloadError(f'An error occurred while downloading file: {str(e)}') from e

        if expected_hash is not None:
            hasher = hashlib_new(hash_type)

            with Path(output_path).open('rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    hasher.update(chunk)

            file_hash = hasher.hexdigest()

            if file_hash != expected_hash:
                Path(output_path).unlink(missing_ok=True)
                self.output_path = None

                raise HashVerificationError(
                    f'Hash verification failed. Hash type: "{hash_type}". Expected hash: "{expected_hash}". Actual hash: "{file_hash}"'
                )
