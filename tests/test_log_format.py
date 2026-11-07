"""The terminal log lines start with an HH:MM:SS time."""

import logging
import logging.config
import re

from flood_agent.__main__ import log_config


def test_log_lines_start_with_a_time_before_the_level():
    config = log_config()
    config["handlers"]["default"]["stream"] = "ext://sys.stdout"
    logging.config.dictConfig(config)
    formatter = logging.getLogger("uvicorn").handlers[0].formatter
    assert formatter is not None
    line = formatter.format(logging.LogRecord("uvicorn", logging.INFO, "", 0, "Started", None, None))
    assert re.fullmatch(r"\d\d:\d\d:\d\d INFO:\s+Started", line.replace("\x1b[32m", "").replace("\x1b[0m", ""))


def test_the_mcp_server_process_logs_its_requests_with_a_time_and_level():
    import subprocess
    import sys

    code = (
        "import logging; from flood_agent import logs; logs.configure_root();"
        "logging.getLogger('mcp.server.lowlevel.server').info('Processing request of type ListToolsRequest')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stderr
    assert re.fullmatch(r"\d\d:\d\d:\d\d INFO:\s+Processing request of type ListToolsRequest\n", out)
