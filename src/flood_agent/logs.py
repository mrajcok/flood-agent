"""Terminal log lines in uvicorn's style, with a time: `10:44:48 INFO:     message`."""

import logging
import sys

DATE_FORMAT = "%H:%M:%S"


class Formatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelprefix)s%(message)s", DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        record.levelprefix = f"{record.levelname}:".ljust(10)
        return super().format(record)


def handler() -> logging.Handler:
    """A stderr handler using the format above."""
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(Formatter())
    return h


def configure_root(level: int = logging.INFO) -> None:
    """Send every logger's records to stderr in this format: for the MCP server process, whose
    library would otherwise log bare messages with no level."""
    logging.basicConfig(level=level, handlers=[handler()], force=True)
