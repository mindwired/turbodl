# Built-in imports
from concurrent.futures import ThreadPoolExecutor
from hashlib import new as hashlib_new
from io import BytesIO
from math import ceil, log2, sqrt
from mmap import ACCESS_WRITE, mmap
from os import PathLike, ftruncate
from pathlib import Path
from threading import Lock
from typing import Any, Literal

# Third-party imports
from httpx import Client, HTTPStatusError, Limits
from psutil import virtual_memory
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
    TaskID,
)
from tenacity import retry, stop_after_attempt, wait_exponential

# Local imports
from .exceptions import DownloadError, HashVerificationError, InsufficientSpaceError, InvalidArgumentError
from .functions import fetch_file_info, has_available_space, looks_like_a_ram_directory


class ChunkBuffer:
    """
    A class for buffering chunks of data.
    """

    def __init__(self, chunk_size_bytes: int = 256 * (1024**2), max_buffer_bytes: int = 1 * (1024**3)) -> None:
        """
        Initialize the ChunkBuffer class.

        This class is used to buffer chunks of data. The buffer size is limited by the available
        virtual memory and the maximum buffer size. The chunks are written to the buffer in
        order to be able to write the data to the file in chunks.

        Args:
            chunk_size_bytes (int): The size of each chunk in bytes.
            max_buffer_bytes (int): The maximum size of the buffer in bytes.
        """

        # Calculate the chunk size in bytes
        self.chunk_size = chunk_size_bytes

        # Calculate the maximum buffer size in bytes
        # The maximum buffer size is the minimum of the maximum buffer size and 30% of the available virtual memory
        self.max_buffer_size = min(max_buffer_bytes, virtual_memory().available * 0.30)

        # Initialize the current buffer as an empty BytesIO object
        self.current_buffer = BytesIO()

        # Initialize the current size of the buffer to 0
        self.current_size = 0

        # Initialize the total amount of data buffered to 0
        self.total_buffered = 0

    def write(self, data: bytes, total_file_size_bytes: int) -> bytes | None:
        """
        Write data to the buffer.

        The following conditions must be met before writing data to the buffer:
        - The current buffer size must be less than the maximum buffer size.
        - The total size of data written to the buffer must be less than the maximum buffer size.
        - The total size of data written to the buffer must be less than the total file size.

        Args:
            data (bytes): The data to write to the buffer.
            total_file_size_bytes (int): The total size of the file in bytes.

        Returns:
            bytes | None: Returns buffered data when buffer is full or conditions are met, None if buffer still has space.
        """

        # Check if the current buffer size is less than the maximum buffer size
        if self.current_size + len(data) > self.max_buffer_size:
            return None

        # Check if the total size of data written to the buffer is less than the maximum buffer size
        if self.total_buffered + len(data) > self.max_buffer_size:
            return None

        # Check if the total size of data written to the buffer is less than the total file size
        if self.total_buffered + len(data) > total_file_size_bytes:
            return None

        self.current_buffer.write(data)
        self.current_size += len(data)
        self.total_buffered += len(data)

        if (
            self.current_size >= self.chunk_size
            or self.total_buffered >= total_file_size_bytes
            or self.current_size >= self.max_buffer_size
        ):
            chunk_data = self.current_buffer.getvalue()

            self.current_buffer.close()
            self.current_buffer = BytesIO()
            self.current_size = 0

            return chunk_data

        return None


class TurboDL:
    """A class for downloading direct download URLs."""

    def __init__(
        self,
        max_connections: int | Literal["auto"] = "auto",
        connection_speed: float = 80,
        show_progress_bars: bool = True,
        custom_headers: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> None:
        """
        Initialize the class with the required settings for downloading a file.

        Args:
            max_connections (int | str | Literal['auto']): The maximum number of connections to use for downloading the file. Defaults to 'auto'.
                - 'auto' will dynamically calculate the number of connections based on the file size and connection speed.
                - An integer between 1 and 24 will set the number of connections to that value.
            connection_speed (float): Your connection speed in Mbps (megabits per second). Defaults to 80.
                - Your connection speed will be used to help calculate the optimal number of connections.
            show_progress_bars (bool): Show or hide all progress bars. Defaults to True.
            custom_headers (dict[str, Any] | None): Custom headers to include in the request. If None, default headers will be used. Defaults to None.
                - Immutable headers are (case-insensitive):
                    - 'Accept-Encoding': 'identity'
                    - 'Range': ...
                    - 'Connection': ...
                - All other headers will be included in the request.
            timeout (int | None): Timeout in seconds for the download process. Or None for no timeout. Default to None.

        Raises:
            InvalidArgumentError: If max_connections is not 'auto' or an integer between 1 and 32, or if connection_speed is not positive.
        """

        # Initialize the instance variables
        self._max_connections: int | Literal["auto"] = max_connections
        self._connection_speed: float = connection_speed

        # Validate the arguments
        if isinstance(self._max_connections, str) and self._max_connections.isdigit():
            self._max_connections = int(self._max_connections)

        if not (self._max_connections == "auto" or (isinstance(self._max_connections, int) and 1 <= self._max_connections <= 24)):
            msg = f"max_connections must be 'auto' or an integer between 1 and 24: {self._max_connections}"
            raise InvalidArgumentError(msg)

        if self._connection_speed <= 0:
            msg = f"connection_speed must be positive: {self._connection_speed}"
            raise InvalidArgumentError(msg)

        self._show_progress_bars: bool = show_progress_bars
        self._timeout: int | None = timeout

        # Create a dictionary with default headers and update it with custom headers
        self._custom_headers: dict[str, Any] = {
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        }

        if custom_headers:
            for key, value in custom_headers.items():
                if key.title() not in {"Accept-Encoding", "Range", "Connection"}:
                    self._custom_headers[key.title()] = value

        # Create a client with the custom headers and settings
        self._client: Client = Client(
            headers=self._custom_headers,
            follow_redirects=True,
            verify=True,
            limits=Limits(max_connections=48, max_keepalive_connections=24, keepalive_expiry=10),
            timeout=self._timeout,
        )

        # Initialize the output path to None
        self.output_path: str | None = None

    def _calculate_connections(self, file_size: int, connection_speed: float) -> int:
        """
        Calculate the optimal number of connections based on file size and connection speed.

        This method uses a sophisticated formula that considers:
        - Logarithmic scaling of file size
        - Square root scaling of connection speed
        - System resource optimization
        - Network overhead management

        Formula:
        conn = β * log2(1 + S / M) * sqrt(V / 100)

        Where:
        - S: File size in MB
        - V: Connection speed in Mbps
        - M: Base size factor (1 MB)
        - β: Dynamic coefficient (5.6)

        Args:
            file_size (int): The size of the file in bytes.
            connection_speed (float): Your connection speed in Mbps.

        Returns:
            int: The estimated optimal number of connections, capped between 2 and 24.
        """

        # Convert file size from bytes to megabytes
        file_size_mb = file_size / (1024 * 1024)

        # Use default connection speed if set to 'auto'
        speed = 80.0 if connection_speed == "auto" else float(connection_speed)

        # Dynamic coefficient for connection calculation
        beta = 5.6

        # Base size factor in MB
        base_size = 1.0

        # Calculate the number of connections using the formula
        conn_float = beta * log2(1 + file_size_mb / base_size) * sqrt(speed / 100)

        # Ensure the number of connections is within the allowed range
        return max(2, min(24, ceil(conn_float)))

    def _get_chunk_ranges(self, total_size: int) -> list[tuple[int, int]]:
        """
        Calculate the optimal chunk ranges for downloading a file.

        This method divides the total file size into optimal chunks based on the number of connections.
        It returns a list of tuples, where each tuple contains the start and end byte indices for a chunk.

        Args:
            total_size (int): The total size of the file in bytes.

        Returns:
            list[tuple[int, int]]: A list of tuples containing the start and end indices of each chunk.
        """

        # If the total size is zero, return a single range starting and ending at 0
        if total_size is None:
            return [(0, 0)]

        # Calculate the number of connections to use for the download
        if self._max_connections == "auto":
            self._max_connections = self._calculate_connections(total_size, self._connection_speed)
        self._max_connections = int(self._max_connections)

        # Calculate the size of each chunk
        chunk_size = ceil(total_size / self._max_connections)

        ranges = []
        start = 0

        # Create ranges for each chunk
        while total_size > 0:
            # Determine the size of the current chunk
            current_chunk = min(chunk_size, total_size)

            # Calculate the end index of the current chunk
            end = start + current_chunk - 1

            # Append the start and end indices as a tuple to the ranges list
            ranges.append((start, end))

            # Move the start index to the next chunk
            start = end + 1

            # Reduce the total size by the size of the current chunk
            total_size -= current_chunk

        return ranges

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=1, max=10), reraise=True)
    def _download_chunk(self, url: str, start: int, end: int, progress: Progress, task_id: int) -> bytes:
        """
        Download a chunk of a file from the provided URL.

        This method sends a GET request to the provided URL with the Range header set to the start and end indices of the chunk.
        It will retry the request up to 5 times if it fails.

        Args:
            url (str): The URL of the file to be downloaded.
            start (int): The start index of the chunk.
            end (int): The end index of the chunk.
            progress (Progress): The progress bar to update.
            task_id (int): The task ID of the progress bar.

        Returns:
            bytes: The downloaded chunk as bytes.

        Raises:
            DownloadError: If the request fails.
        """

        # Set the Range header to the start and end indices of the chunk
        headers = {**self._custom_headers}

        if end > 0:
            headers["Range"] = f"bytes={start}-{end}"

        try:
            # Send the request and get the response
            with self._client.stream("GET", url, headers=headers) as r:
                # Raise an exception if the response status code is not 200
                r.raise_for_status()

                # Initialize the chunk as an empty bytes object
                chunk = b""

                # Iterate over the response and update the progress bar for each chunk
                for data in r.iter_bytes(chunk_size=8192):
                    # Append the chunk to the result
                    chunk += data

                    # Update the progress bar
                    progress.update(TaskID(task_id), advance=len(data))

                # Return the downloaded chunk
                return chunk
        except HTTPStatusError as e:
            # Raise a DownloadError if the request fails
            msg = f"An error occurred while downloading chunk: {e}"
            raise DownloadError(msg) from e

    def _download_with_buffer(
        self, url: str, output_path: str | PathLike, total_size: int, progress: Progress, task_id: int
    ) -> None:
        """
        Download a file from the provided URL to the output file path using a buffer.

        This method downloads a file in chunks and writes each chunk to the output file as soon as it is downloaded.
        The chunks are written to the output file in order to avoid having to keep the entire file in memory.

        Args:
            url (str): The URL of the file to be downloaded.
            output_path (str | PathLike): The path to save the downloaded file to.
            total_size (int): The total size of the file in bytes.
            progress (Progress): The progress bar to update.
            task_id (int): The task ID of the progress bar.
        """

        def write_to_file(data: bytes, position: int) -> None:
            """
            Write data to the output file at the specified position.

            Args:
                data (bytes): The data to write to the file.
                position (int): The position in the file to write the data.
            """

            # Open the file in read and write binary mode
            with Path(output_path).open("r+b") as f:
                # Get the current size of the file
                current_size = f.seek(0, 2)

                # If the file is smaller than the total size, truncate the file to the total size
                if current_size < total_size:
                    ftruncate(f.fileno(), total_size)

                # Map the file to memory
                with mmap(f.fileno(), length=total_size, access=ACCESS_WRITE) as mm:
                    # Write the data to the memory map at the specified position
                    mm[position : position + len(data)] = data

                    # Flush the memory map to disk
                    mm.flush()

        def download_worker(
            chunk_buffers: dict[int, ChunkBuffer], write_positions: list[int], start: int, end: int, chunk_id: int
        ) -> None:
            """
            Download a chunk of a file from the provided URL.

            Args:
                start (int): The start index of the chunk.
                end (int): The end index of the chunk.
                chunk_id (int): The ID of the chunk.

            Raises:
                DownloadError: If the request fails.
            """

            # Initialize the chunk buffer
            chunk_buffers[chunk_id] = ChunkBuffer()

            # Set the range header
            headers = {**self._custom_headers}

            if end > 0:
                headers["Range"] = f"bytes={start}-{end}"

            try:
                # Download the file chunk by chunk
                with self._client.stream("GET", url, headers=headers) as r:
                    r.raise_for_status()

                    # Iterate over the response and update the progress bar for each chunk
                    for data in r.iter_bytes(chunk_size=1024 * 1024):
                        # Write the chunk to the buffer
                        if complete_chunk := chunk_buffers[chunk_id].write(data, total_size):
                            # Write the complete chunk to the file
                            write_to_file(complete_chunk, start + write_positions[chunk_id])

                            # Update the write position
                            write_positions[chunk_id] += len(complete_chunk)

                        # Update the progress bar
                        progress.update(TaskID(task_id), advance=len(data))

                    # Write any remaining data in the buffer to the file
                    if remaining := chunk_buffers[chunk_id].current_buffer.getvalue():
                        write_to_file(remaining, start + write_positions[chunk_id])
            except Exception as e:
                # Raise a DownloadError if the request fails
                msg = f"An error occurred while downloading chunk: {e}"
                raise DownloadError(msg) from e

        # Get the chunk ranges
        ranges = self._get_chunk_ranges(total_size)

        # Initialize buffers and write positions
        chunk_buffers: dict[int, ChunkBuffer] = {}
        write_positions = [0] * len(ranges)

        # Download the file
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            # Iterate over the chunk ranges
            for future in [
                executor.submit(download_worker, chunk_buffers, write_positions, start, end, i)
                for i, (start, end) in enumerate(ranges)
            ]:
                future.result()

    def _download_direct(self, url: str, output_path: str | PathLike, total_size: int, progress: Progress, task_id: int) -> None:
        """
        Download a file from the provided URL directly to the output file path.

        This method divides the file into chunks and downloads each chunk concurrently using multiple threads.
        The downloaded data is directly written to the specified output file path.

        Args:
            url (str): The URL of the file to be downloaded.
            output_path (str | PathLike): The path to save the downloaded file to.
            total_size (int): The total size of the file in bytes.
            progress (Progress): The progress bar to update.
            task_id (int): The task ID of the progress bar.
        """

        # Initialize a lock for writing to the file
        write_lock = Lock()

        # List to store future objects from the ThreadPoolExecutor
        futures = []

        def download_worker(start: int, end: int) -> None:
            """
            Download a chunk of the file and write it to the output file.

            This function is designed to be used with the ThreadPoolExecutor to download chunks concurrently.
            It will raise a DownloadError if any exception occurs during download.

            Args:
                start (int): The start byte index of the chunk.
                end (int): The end byte index of the chunk.

            Raises:
                DownloadError: If any exception occurs during download.
            """

            # Set the Range header for the request
            headers = {**self._custom_headers}

            if end > 0:
                headers["Range"] = f"bytes={start}-{end}"

            try:
                # Stream the file chunk from the server
                with self._client.stream("GET", url, headers=headers) as r:
                    # Raise an exception if the response status code is not 200
                    r.raise_for_status()

                    # Iterate over the response and write the chunk to the file
                    for data in r.iter_bytes(chunk_size=1024 * 1024):
                        chunk_len = len(data)

                        # Acquire the write lock and open the output file in read-write mode
                        with write_lock, Path(output_path).open("r+b") as fo:
                            # Seek to the start of the chunk and write the data
                            fo.seek(start)
                            fo.write(data)
                            start += chunk_len

                        # Update the progress bar
                        progress.update(TaskID(task_id), advance=chunk_len)
            except Exception as e:
                # Raise a DownloadError if any exception occurs during download
                msg = f"An error occurred while downloading chunk: {e}"
                raise DownloadError(msg) from e

        # Get the chunk ranges for the download
        ranges = self._get_chunk_ranges(total_size)

        # Use ThreadPoolExecutor to download chunks concurrently
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [executor.submit(download_worker, start, end) for start, end in ranges]

            # Wait for all futures to complete
            for future in futures:
                future.result()

    def download(
        self,
        url: str,
        output_path: str | PathLike | None = None,
        pre_allocate_space: bool = False,
        use_ram_buffer: bool | Literal["auto"] = "auto",
        overwrite: bool = True,
        expected_hash: str | None = None,
        hash_type: Literal[
            "md5",
            "sha1",
            "sha224",
            "sha256",
            "sha384",
            "sha512",
            "blake2b",
            "blake2s",
            "sha3_224",
            "sha3_256",
            "sha3_384",
            "sha3_512",
            "shake_128",
            "shake_256",
        ] = "md5",
    ) -> None:
        """
        Downloads a file from the provided URL to the output file path.

        Args:
            url (str): The download URL to download the file from. Defaults to None.
            output_path (str | PathLike | None): The path to save the downloaded file to. If the path is a directory, the file name will be generated from the server response. If the path is a file, the file will be saved with the provided name. If not provided, the file will be saved to the current working directory. Defaults to None.
            pre_allocate_space (bool): Whether to pre-allocate space for the file, useful to avoid disk fragmentation. Defaults to False.
            use_ram_buffer (bool | str | Literal["auto"]): Whether to use a RAM buffer to download the file. If True, the file will be downloaded with the help of a RAM buffer. If False, the file will be downloaded directly to the output file path. If 'auto', the RAM buffer will be used if the output path is not a RAM directory. Defaults to 'auto'.
            overwrite (bool): Overwrite the file if it already exists. Otherwise, a '_1', '_2', etc. suffix will be added. Defaults to True.
            expected_hash (str | None): The expected hash of the downloaded file. If not provided, the hash will not be checked. Defaults to None.
            hash_type (str | Literal['md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512', 'blake2b', 'blake2s', 'sha3_224', 'sha3_256', 'sha3_384', 'sha3_512', 'shake_128', 'shake_256']): The hash type to use for the hash verification. Defaults to 'md5'.

        Raises:
            InvalidArgumentError: If the URL is not provided or the use_ram_buffer is not a boolean or 'auto'.
            DownloadError: If an error occurs while downloading the file.
            HashVerificationError: If the hash of the downloaded file does not match the expected hash.
            InsufficientSpaceError: If there is not enough space to download the file.
            OnlineRequestError: If an error occurs while getting file info.
        """

        # Check if the URL is provided
        if not url:
            msg = "Missing URL value"
            raise InvalidArgumentError(msg)

        # Resolve the output path, defaulting to the current working directory if not provided
        output_path = Path.cwd() if output_path is None else Path(output_path).resolve()

        # Check if the use_ram_buffer is a boolean or 'auto'
        if not (use_ram_buffer == "auto" or isinstance(use_ram_buffer, bool)):
            msg = f"Invalid use_ram_buffer value: {use_ram_buffer}: expected 'auto' or boolean"
            raise InvalidArgumentError(msg)

        # Determine if the output path is a RAM directory
        is_ram_directory = looks_like_a_ram_directory(output_path)

        # Determine if RAM buffer should be used (if not provided)
        if use_ram_buffer == "auto":
            use_ram_buffer = not is_ram_directory

        # Get the file info from the URL
        file_info = fetch_file_info(url, self._client, self._custom_headers, self._timeout)

        # Handle the case where the file info is not available
        if file_info is None:
            has_unknown_info = True
            total_size = 0
            # mimetype = "application/octet-stream"  # TODO: Use it?
            suggested_filename = "unknown_file"
        else:
            has_unknown_info = False
            total_size = int(file_info["size"])
            # mimetype = str(file_info["mimetype"])  # TODO: Use it?
            suggested_filename = str(file_info["filename"])

        # Check if there is enough space to download the file
        if not has_unknown_info and not has_available_space(output_path, total_size):
            msg = f"Not enough space to download {total_size} bytes to '{output_path.as_posix()}'"
            raise InsufficientSpaceError(msg)

        try:
            # If output path is a directory, append suggested filename
            if output_path.is_dir():
                output_path = Path(output_path, suggested_filename)

            # Handle the case where output file already exists
            if not overwrite:
                base_name = output_path.stem
                extension = output_path.suffix
                counter = 1

                while output_path.exists():
                    output_path = Path(output_path.parent, f"{base_name}_{counter}{extension}")
                    counter += 1

            # Handle pre-allocation of space if requested
            if not has_unknown_info:
                if pre_allocate_space and total_size > 0:
                    with Progress(
                        SpinnerColumn(spinner_name="dots", style="bold cyan"),
                        TextColumn(f"[bold cyan]Pre-allocating space for {total_size} bytes...", justify="left"),
                        transient=True,
                        disable=not self._show_progress_bars,
                    ) as progress:
                        progress.add_task("", total=None)

                        if pre_allocate_space and total_size > 0:
                            with output_path.open("wb") as fo:
                                fo.truncate(total_size)
                else:
                    output_path.touch(exist_ok=True)
            else:
                output_path.touch(exist_ok=True)

            # Set the output path
            self.output_path = output_path.as_posix()

            # Set up the progress columns for display
            progress_columns = [
                TextColumn(f'Downloading "{suggested_filename}"', style="bold magenta"),
                BarColumn(style="bold white", complete_style="bold red", finished_style="bold green"),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                TimeElapsedColumn(),
                TextColumn("[bold][progress.percentage]{task.percentage:>3.0f}%"),
                TextColumn(
                    f"[bold white]({('with' if use_ram_buffer else 'without')} RAM buffer, writing to {('RAM' if is_ram_directory else 'DISK')})",
                    justify="right",
                ),
            ]

            # Perform the download
            with Progress(*progress_columns, disable=not self._show_progress_bars) as progress:
                task_id = progress.add_task("download", total=total_size or None, filename=output_path.name)

                # Determine download method based on buffer usage
                if total_size == 0:
                    Path(output_path).write_bytes(self._download_chunk(url, 0, 0, progress, task_id))
                elif use_ram_buffer:
                    self._download_with_buffer(url, output_path, total_size, progress, task_id)
                else:
                    self._download_direct(url, output_path, total_size, progress, task_id)
        except KeyboardInterrupt:
            # Handle download interruption by user
            Path(output_path).unlink(missing_ok=True)
            self.output_path = None
            return
        except Exception as e:
            # Handle any other download exceptions
            msg = f"An error occurred while downloading file: {e}"
            raise DownloadError(msg) from e

        # Verify the hash of the downloaded file if an expected hash is provided
        if expected_hash is not None:
            hasher = hashlib_new(hash_type)

            with Path(output_path).open("rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)

            file_hash = hasher.hexdigest()

            if file_hash != expected_hash:
                # Handle hash verification failure
                Path(output_path).unlink(missing_ok=True)
                self.output_path = None

                msg = f'Hash verification failed. Hash type: "{hash_type}". Actual hash: "{file_hash}". Expected hash: "{expected_hash}".'
                raise HashVerificationError(msg)
