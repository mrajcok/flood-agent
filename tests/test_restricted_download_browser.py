"""A table Display's Download: straight away for an unrestricted table, through a dialog for a restricted one."""

import re
from urllib.parse import urljoin

import pytest
from conftest import ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect
from test_notebook_browser import run_in_new_notebook
from test_public_key import ED25519, ED25519_FINGERPRINT
from test_restricted_download import REAL, RESTRICTED, UNKNOWN, UNRESTRICTED

from flood_agent.llm import TextChunk

pytestmark = pytest.mark.generated_data


@pytest.fixture
def semantic_layer_path():
    return REAL


def shown(sql: str) -> list:
    """The two steps of an AI Cell's script that run `sql` and show its Result Table."""
    return [
        [tool_call("run_query", sql=sql, title="t")],
        lambda messages: [tool_call("show_table", result_table=last_tool_result(messages)["result_table"])],
    ]


def test_download_asks_in_a_dialog_only_for_a_restricted_table(live_page, build_agent):
    steps = [*shown(UNRESTRICTED), *shown(RESTRICTED), *shown(UNKNOWN), [TextChunk("Here you go.")]]
    page = live_page(agent=build_agent(ScriptedLLM(*steps)))
    cell = run_in_new_notebook(page, "show them")
    expect(cell.locator(".reply", has_text="Here you go.")).to_be_visible()
    plain, restricted, unknown = (cell.locator(".output .display").nth(i) for i in range(3))
    dialog = page.get_by_role("dialog", name="Restricted table")
    masked = dialog.get_by_role("button", name="Download masked")
    encrypted = dialog.get_by_role("button", name="Download encrypted")

    expect(plain.locator(".table-name .name")).to_have_text(re.compile("^rt_"))
    name = plain.locator(".table-name .name").text_content()
    with page.expect_download() as download:
        plain.get_by_role("link", name="Download PSV").click()
    assert download.value.suggested_filename == f"{name}.psv"
    expect(dialog).to_have_count(0)

    restricted.get_by_role("link", name="Download PSV").click()
    expect(dialog).to_contain_text("Restricted Columns: imsi")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    restricted.get_by_role("link", name="Download PSV").click()
    with page.expect_download() as download:
        masked.click()
    expect(restricted.locator(".table-name .name")).to_have_text(download.value.suggested_filename.split(".")[0])
    assert download.value.suggested_filename.endswith(".masked.psv")
    expect(dialog).to_have_count(0)

    unknown.get_by_role("link", name="Download PSV").click()
    expect(dialog).to_contain_text("it can only be downloaded encrypted")
    expect(masked).to_be_hidden()
    no_key = dialog.get_by_text("No Public Key on file. Add one in Settings")
    expect(no_key).to_be_visible()
    expect(dialog.get_by_role("link", name="Settings")).to_have_attribute("href", "/settings")
    expect(encrypted).to_be_hidden()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)

    saved = page.request.post(urljoin(page.url, "/settings/public-key"), form={"key": ED25519},
                              headers={"origin": urljoin(page.url, "/").rstrip("/")}, max_redirects=0)
    assert saved.status == 303
    unknown.get_by_role("link", name="Download PSV").click()
    expect(dialog).to_contain_text(f"ssh-ed25519 Public Key {ED25519_FINGERPRINT}")
    expect(no_key).to_be_hidden()
    expect(masked).to_be_hidden()
    with page.expect_download() as download:
        encrypted.click()
    expect(unknown.locator(".table-name .name")).to_have_text(download.value.suggested_filename.split(".")[0])
    assert download.value.suggested_filename.endswith(".psv.age")
    expect(dialog).to_have_count(0)
