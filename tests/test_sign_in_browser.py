"""The sign-in page and the header's Sign out, driven in headless system Chrome. The rules of signing in are tested in
test_sign_in.py."""

from conftest import TEST_PASSWORD, TEST_USER
from playwright.sync_api import expect


def test_signing_in_shows_the_users_name_and_sign_out_goes_back_to_sign_in(live_page):
    page = live_page(signed_in=False)

    page.get_by_label("User name").fill(TEST_USER)
    page.get_by_label("Password").fill(TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator(".topbar")).to_contain_text(TEST_USER)

    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_label("Password")).to_be_visible()
