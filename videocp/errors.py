class VideoCpError(RuntimeError):
    """Base error for videocp."""


class ExtractionError(VideoCpError):
    """Raised when page extraction fails."""


class DownloadError(VideoCpError):
    """Raised when media download fails."""

    def __init__(self, message: str, attempts: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.attempts = attempts or []
