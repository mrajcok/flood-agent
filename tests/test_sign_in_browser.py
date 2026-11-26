"""Signing in and out, driven in headless system Chrome."""

import re

from conftest import TEST_PASSWORD, TEST_USER
from playwright.sync_api import expect


def test_signing_in_shows_the_users_name_and_sign_out_goes_back_to_sign_in(live_page):
    page = live_page(signed_in=False)
    expect(page).to_have_url(re.compile(r"/sign-in\?next=/$"))

    page.get_by_label("User name").fill(TEST_USER)
    page.get_by_label("Password").fill("wrong guess")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_have_text("User name or password is wrong")

    page.get_by_label("Password").fill(TEST_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"/n/\w+$"))
    expect(page.locator(".topbar")).to_contain_text(TEST_USER)

    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(re.compile(r"/sign-in$"))
    page.go_back()
    page.reload()
    expect(page.get_by_label("Password")).to_be_visible()
