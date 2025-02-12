# Built-in imports
from mimetypes import guess_extension as guess_mimetype_extension
from os import PathLike
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# Third-party imports
from httpx import Client, HTTPError, RemoteProtocolError
from psutil import disk_partitions, disk_usage
from tenacity import retry, stop_after_attempt, wait_exponential

# Local imports
from .exceptions import OnlineRequestError


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=6), reraise=True)
def fetch_file_info(
    url: str, httpx_client: Client, headers: dict[str, Any], timeout: int | None = None
) -> dict[str, str | int] | None:
    """
    Get information about the file to be downloaded.

    This method sends a HEAD request to the provided URL and retrieves the file size, mimetype, and filename from the response headers.
    It will retry the request up to 3 times if it fails.

    Args:
        url (str): The URL of the file to be downloaded.
        httpx_client (Client): The HTTPX client to use for the request.
        headers (dict[str, Any]): The headers to include in the request.
        timeout (int | None): The timeout in seconds for the request. Or None for no timeout. Default to None.

    Returns:
        dict[str, str | int] | None: A dictionary containing the file size, mimetype, and filename, or None if the request fails.

    Raises:
        OnlineRequestError: If the request fails due to an HTTP error.
    """

    try:
        # Send a HEAD request to the URL to get the file information
        r = httpx_client.head(url, headers=headers, timeout=timeout)
    except RemoteProtocolError:
        # If the request fails due to a remote protocol error, return None
        return None
    except HTTPError as e:
        # If the request fails due to an HTTP error, raise a OnlineRequestError
        raise OnlineRequestError(f"An error occurred while getting file info: {str(e)}") from e

    # Get the headers from the response
    r_headers = r.headers

    # Get the content length from the headers
    content_length = int(r_headers.get("content-length", 0))

    # Get the content type from the headers
    content_type = r_headers.get("content-type", "application/octet-stream").split(";")[0].strip()

    # Get the filename from the content disposition header
    content_disposition = r_headers.get("content-disposition")
    filename = None

    if content_disposition:
        if "filename*=" in content_disposition:
            filename = content_disposition.split("filename*=")[-1].split("'")[-1]
        elif "filename=" in content_disposition:
            filename = content_disposition.split("filename=")[-1].strip("\"'")

    if not filename:
        # If filename is not found, use the URL path as the filename
        filename = Path(unquote(urlparse(url).path)).name or f"unknown_file{guess_mimetype_extension(content_type) or ''}"

    # Return the file information
    return {"size": content_length, "mimetype": content_type, "filename": filename}


def get_filesystem_type(path: str | Path) -> str | None:
    """
    Get the type of filesystem at the given path.

    Args:
        path (str | Path): The path to get the filesystem type for.

    Returns:
        str | None: The type of filesystem at the path, or None if the path is invalid.
    """

    # Convert path to Path object
    path = Path(path).resolve()

    # Find the partition that the path is on, based on the mountpoint
    best_part = max(
        (part for part in disk_partitions(all=True) if path.as_posix().startswith(part.mountpoint)),
        key=lambda part: len(part.mountpoint),
        default=None,
    )

    # Return the filesystem type of the partition
    return best_part.fstype if best_part else None


def has_available_space(path: str | PathLike, required_size: int, minimum_space: int = 1) -> bool:
    """
    Check if there is sufficient space available at the specified path.

    Args:
        path (str | PathLike): The file or directory path to check for available space.
        required_size (int): The size of the file or data to be stored, in bytes.
        minimum_space (int): The minimum additional space to ensure, in gigabytes. Defaults to 1.

    Returns:
        bool: True if there is enough available space, False otherwise.
    """

    # Convert path to Path object
    path = Path(path)

    # Calculate the total required space including the minimum space buffer
    required_space = required_size + (minimum_space * 1024 * 1024 * 1024)

    # Get the disk usage statistics for the appropriate path (parent if it's a file or doesn't exist)
    disk_usage_obj = disk_usage(path.parent.as_posix() if path.is_file() or not path.exists() else path.as_posix())

    # Return True if there is enough free space, False otherwise
    return bool(disk_usage_obj.free >= required_space)


def looks_like_a_ram_directory(path: str | Path) -> bool:
    """
    Check if a path is a temporary RAM-backed filesystem.

    Args:
        path (str | Path): The path to check.

    Returns:
        bool: True if the path is a temporary RAM-backed filesystem, False otherwise.
    """

    # List of known RAM-backed filesystems
    ram_filesystems = {"tmpfs", "ramfs", "devtmpfs"}

    # Get the filesystem type of the path
    filesystem_type = get_filesystem_type(path)

    # Check if the filesystem type is a known RAM-backed filesystem
    return filesystem_type in ram_filesystems
