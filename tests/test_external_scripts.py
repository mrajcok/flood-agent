"""The page's CDN scripts actually load. Needs internet access, so it only runs on request:

    uv run pytest -m network
"""

import re

import httpx
import pytest
from conftest import ScriptedLLM

pytestmark = pytest.mark.network


def test_every_external_script_on_the_page_loads(build_agent, web_client):
    html = web_client(build_agent(ScriptedLLM())).get("/").text
    urls = re.findall(r'<script[^>]*\ssrc="(https?://[^"]+)"', html)
    assert urls, "expected the page to load its libraries from a CDN"

    with httpx.Client(follow_redirects=True, timeout=30) as http:
        broken = {url: status for url in urls if (status := http.get(url).status_code) != 200}

    assert broken == {}
