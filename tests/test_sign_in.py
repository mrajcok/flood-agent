"""Signing in to the web app: who gets in, how a session ends, and which requests are refused."""

from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import (
    ORIGIN,
    TEST_PASSWORD,
    TEST_USER,
    Clock,
    ScriptedLLM,
    new_client,
    sign_in,
    sign_in_for_test,
)

HOUR = 3_600_000  # ms
WRONG = "User name or password is wrong"


@pytest.fixture
def agent(build_agent):
    return build_agent(ScriptedLLM(), clock=Clock())


@pytest.fixture
def client(web_client, agent):
    """A browser that has not signed in."""
    return web_client(agent, signed_in=False)


def get(client, path, **headers):
    return client.get(path, headers=headers, follow_redirects=False)


def signed_in(client) -> bool:
    return get(client, "/api/tables").status_code == 200


# --- without a session -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/n/abc", "/n/abc?cell=1&x=a%20b"])
def test_a_page_redirects_to_sign_in_and_remembers_where_it_was_going(client, agent, path):
    response = get(client, path)

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/sign-in"
    assert parse_qs(location.query)["next"] == [path]
    assert agent.notebooks.list_notebooks() == []


def test_a_form_posted_without_a_session_goes_to_sign_in(client, agent):
    response = client.post("/notebooks", headers={"accept": "text/html"}, follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, "/sign-in")
    assert agent.notebooks.list_notebooks() == []


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/tables"),
        ("GET", "/api/results/rt_x/psv"),
        ("DELETE", "/api/tables/lookup_x"),
        ("POST", "/api/lookup-tables"),
        ("GET", "/n/abc/stale"),  # fetched by the page's script: a redirect would hand it the sign-in page
        ("GET", "/n/abc/cells/def/stream"),
        ("POST", "/n/abc/cells/def/run"),
        ("GET", "/openapi.json"),
        ("GET", "/nothing/here"),
    ],
)
def test_an_api_request_gets_401(client, method, path):
    response = client.request(method, path, follow_redirects=False)

    assert response.status_code == 401
    assert "location" not in response.headers and "hx-redirect" not in response.headers


@pytest.mark.parametrize("method, path", [("POST", "/n/abc/cells"), ("GET", "/n/abc")])
def test_an_htmx_request_gets_401_and_sends_the_browser_to_sign_in(client, method, path):
    response = client.request(method, path, headers={"HX-Request": "true"}, follow_redirects=False)

    assert (response.status_code, response.headers["hx-redirect"]) == (401, "/sign-in")


def test_the_sign_in_page_and_static_assets_need_no_session(client):
    page = get(client, "/sign-in")

    assert page.status_code == 200 and 'type="password"' in page.text
    assert get(client, "/static/format.js").status_code == 200


def test_a_made_up_session_cookie_is_no_session(client):
    client.cookies.set("canopy_session", "made-up")

    assert not signed_in(client)


# --- signing in --------------------------------------------------------------------------------------


def test_signing_in_sets_a_session_cookie_and_goes_to_the_notebook_list(client):
    response = sign_in(client)

    assert (response.status_code, response.headers["location"]) == (303, "/")
    name_value, *attributes = [part.strip() for part in response.headers["set-cookie"].split(";")]
    assert len(name_value.partition("=")[2]) >= 43  # 256 bits in base64
    assert sorted(a.lower() for a in attributes) == ["httponly", "path=/", "samesite=lax"]  # no Max-Age; not Secure here
    assert signed_in(client)


def test_the_session_cookie_is_secure_when_the_setting_is_on(web_client, agent):
    client = web_client(agent, signed_in=False, sign_in_config=sign_in_for_test(cookie_secure=True))

    assert "secure" in sign_in(client).headers["set-cookie"].lower()


def test_signing_in_again_starts_a_new_session_with_a_new_token(client):
    first, second = (sign_in(client).headers["set-cookie"].split(";")[0] for _ in range(2))

    assert first != second


@pytest.mark.parametrize("target", ["/n/abc", "/n/abc?cell=1", "/n/a%20b"])
def test_after_signing_in_the_user_goes_to_the_page_asked_for(client, target):
    form = client.get("/sign-in", params={"next": target}).text

    assert f'name="next" value="{target}"' in form
    assert sign_in(client, next=target).headers["location"] == target


@pytest.mark.parametrize(
    "target",
    ["", "https://evil.example/x", "//evil.example", "/\\evil.example", "/\t/evil.example", "evil.example",
     "javascript:alert(1)", " /n/abc"],
)
def test_a_next_that_would_leave_the_app_goes_to_the_notebook_list_instead(client, target):
    assert sign_in(client, next=target).headers["location"] == "/"


@pytest.mark.parametrize("name, password", [(TEST_USER, "wrong guess"), ("nobody", TEST_PASSWORD), ("a\nb", "x y z")])
def test_a_wrong_password_and_an_unknown_name_get_the_same_message_and_a_log_line(client, app_log, name, password):
    response = sign_in(client, name, password, next="/n/abc")

    assert response.status_code == 401 and WRONG in response.text
    assert "set-cookie" not in response.headers and not signed_in(client)
    assert 'value="/n/abc"' in response.text  # a second try still goes there
    (line,) = {r.getMessage() for r in app_log.records}
    assert repr(name) in line and "\n" not in line
    assert password not in line + response.text


# --- how a session ends ------------------------------------------------------------------------------


def test_a_session_idle_past_the_timeout_is_gone_and_each_request_extends_it(web_client, agent):
    client = web_client(agent, sign_in_config=sign_in_for_test(idle_timeout_ms=12 * HOUR))
    clock = agent.config.clock

    for _ in range(3):
        clock.ms += 12 * HOUR
        assert signed_in(client)
    clock.ms += 12 * HOUR + 1
    assert not signed_in(client)
    clock.ms -= HOUR
    assert not signed_in(client)


def test_signing_out_ends_only_this_session(client):
    other = new_client(client.app)
    sign_in(client)
    sign_in(other)
    token = client.cookies["canopy_session"]

    response = client.post("/sign-out", follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, "/sign-in")
    assert "canopy_session" not in client.cookies and not signed_in(client)
    client.cookies.set("canopy_session", token)  # discarded on the server too, not just in this browser
    assert not signed_in(client)
    assert signed_in(other)


def test_the_header_shows_the_users_name_and_a_sign_out_control(client):
    sign_in(client)

    header = client.get("/").text.split('class="topbar"')[1].split("</form>")[0]

    assert f">{TEST_USER}<" in header
    assert 'action="/sign-out"' in header and ">Sign out</button>" in header


# --- requests from another site ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "http://evil.example"},
        {"origin": "https://testserver"},
        {"origin": "http://testserver:81"},
        {"origin": "http://testserver.evil.example"},
        {"origin": "null"},
        {"origin": "http://[bad"},
        {"origin": "http://evil.example", "referer": ORIGIN + "/n/abc"},  # Origin decides when present
        {"referer": "http://evil.example/" + ORIGIN},
    ],
)
def test_a_change_from_another_origin_or_from_nowhere_gets_403(client, agent, headers):
    sign_in(client)
    del client.headers["origin"]

    response = client.post("/notebooks", headers=headers, follow_redirects=False)

    assert response.status_code == 403
    assert agent.notebooks.list_notebooks() == []


def test_a_same_origin_referer_will_do_without_an_origin(client):
    sign_in(client)
    del client.headers["origin"]

    response = client.post("/notebooks", headers={"referer": ORIGIN + "/n/abc"}, follow_redirects=False)

    assert response.status_code == 303


@pytest.mark.parametrize("method, path", [("PUT", "/api/lookup-tables/x"), ("DELETE", "/api/tables/x"),
                                          ("PATCH", "/api/tables/x"), ("POST", "/sign-in"), ("POST", "/sign-out")])
def test_every_method_that_changes_something_is_checked(client, method, path):
    sign_in(client)

    assert client.request(method, path, headers={"origin": "http://evil.example"}).status_code == 403
    assert signed_in(client)


def test_reading_needs_no_origin(client):
    sign_in(client)

    assert get(client, "/api/tables", origin="http://evil.example").status_code == 200
    assert client.head("/static/format.js", headers={"origin": "http://evil.example"}).status_code == 200
