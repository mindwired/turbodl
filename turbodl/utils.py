# Built-in imports
from io import BytesIO
from typing import Optional


class ChunkBuffer:
    """
    A class for buffering chunks of data.
    """

    def __init__(self, chunk_size_mb: int = 128) -> None:
        """
        Initialize the ChunkBuffer class.

        Args:
            chunk_size_mb: The size of each chunk in megabytes.
        """

        self.chunk_size = chunk_size_mb * 1024 * 1024
        self.current_buffer = BytesIO()
        self.current_size = 0

    def write(self, data: bytes) -> Optional[bytes]:
        """
        Write data to the buffer.

        Args:
            data: The data to write to the buffer.

        Returns:
            The data that was written to the buffer.~
        """

        self.current_buffer.write(data)
        self.current_size += len(data)

        if self.current_size >= self.chunk_size:
            chunk_data = self.current_buffer.getvalue()

            self.current_buffer = BytesIO()
            self.current_size = 0

            return chunk_data

        return None
