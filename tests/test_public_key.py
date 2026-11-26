"""The Settings page, where a User keeps their one Public Key."""

import logging
import re

import pyrage
import pytest
from conftest import TEST_USER, ScriptedLLM, new_client, sign_in, sign_in_for_test

# `ssh-keygen -t ed25519` public keys and what `ssh-keygen -lf` prints for them.
ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVkpHbM/10oHsniG0E+zeM6Gmhm+dNRdkRwWZCvO6QH alice@laptop"
ED25519_FINGERPRINT = "SHA256:jg7efI25tSLIqE6UE4EJyk5cxY9YF2K3oZSXqFqolLA"


RSA_BLOB = (
    "AAAAB3NzaC1yc2EAAAADAQABAAABAQCqgUTNsPdQH2TEQcOov/bVGqy/VAmwPexB4rlW85y/y89H7CWert9PSDxA88PJpMCCRME7"
    "PM4QQ08wse31SwOMFy13NzbZ/N4TpjvL9fCfr9Y0v6e2V8GXB5EBPOTKpuxyR/GolHhodrzJef59SXQklpANjQiPZ7CpHWU3d03I"
    "q0Lh0URT4QorUFYOvCY6rkPVAgrq2aWjel1QKLuGJSY9SBlORBM3OsBs10C8kfPfJ8tX7rh5w5Epz0tDU8a1L0l3/2NKIwiDKDPN"
    "7M2K9ThpDczY5xcO9DVuQd2rRE3uboGcq0L96vcGpCR1hSvXcEtTOaSPBDKzL7nG9ppUheN9"
)
RSA = f"ssh-rsa {RSA_BLOB} alice rsa key"
RSA_FINGERPRINT = "SHA256:G21+3znikonPFKTL/j1yDEtABeGQOTveVwwcjZI/8ac"
AGE = "age1dngmpasxgdn2msfekef88tmgc5f5m8jyk2lx7x34kzm54340s4xsekvknz"
AGE_FINGERPRINT = "age1dngmpa…ekvknz"  # its first ten and last six characters
# Well-formed keys that are not accepted: RSA one bit short, and a type age cannot encrypt for.
RSA_2047 = (
    "ssh-rsa "
    "AAAAB3NzaC1yc2EAAAADAQABAAABAHAD90xg3h4Q5SbC6embUrFAuhBI7qe8Wyz1uPAK9nEhtelU5CCVA3ULo7FMk3C/B6SMD8NB"
    "Oe/+B6COJqH7HjOiSExEMtNO50GeOcmR8+be1Ox8J0LXddNo/b0+qimRdC88RG4+twKyyMvQNOHLUtoLD64uchUf7uRNMc6d2lDY"
    "AgUs12Muh1odBUsXYhsmrE4kKwpZ437/7R2J4bYetV+R3PaqheX5z3i1609++EDzbdElFmvMSvuC2A5PSqHembs5rwx/lVTI1nUt"
    "IPFSs4sS5ouzgX4+hvGlebgeQmqKKgsj/BntiVAJlXa6wVpMIvZqbjFg/hUT58efBMdnh50="
    " short2047"
)
ECDSA = (
    "ecdsa-sha2-nistp256 "
    "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBJZlUU0aWhSqHZ7CijhhTY+YqGa7CLamgM/UgZrnR3SEg9A7"
    "OvDW9zXnlTa7W8/XUudgK17OT6vphjIk0RU0Dpk="
    " ec"
)


@pytest.fixture
def agent(build_agent):
    return build_agent(ScriptedLLM())


@pytest.fixture
def client(web_client, agent):
    return web_client(agent)


def settings_text(client) -> str:
    """The Settings page's main content as text: tags dropped, whitespace collapsed."""
    page = client.get("/settings")
    assert page.status_code == 200
    main = page.text.split("<main", 1)[1].split("</main>", 1)[0].split(">", 1)[1]
    return " ".join(re.sub(r"<[^>]+>", " ", main).split())


def upload(client, text: str = "", file: bytes | None = None):
    """Post the Settings page's key form, with pasted `text`, an uploaded `.pub` file, or both."""
    files = {"file": ("key.pub", file)} if file is not None else None
    return client.post("/settings/public-key", data={"key": text}, files=files, follow_redirects=False)


def test_the_header_links_to_settings_next_to_sign_out(client):
    header = client.get("/").text.split('class="signed-in"', 1)[1].split("</form>", 1)[0]
    assert '<a href="/settings">Settings</a>' in header and ">Sign out</button>" in header


def test_the_settings_page_says_when_no_public_key_is_on_file(client):
    assert "No public key on file" in settings_text(client)


@pytest.mark.parametrize("key,shown", [
    (AGE, f"Type age Fingerprint {AGE_FINGERPRINT} Comment (none)"),
    (ED25519, f"Type ssh-ed25519 Fingerprint {ED25519_FINGERPRINT} Comment alice@laptop"),
    (RSA, f"Type ssh-rsa Fingerprint {RSA_FINGERPRINT} Comment alice rsa key"),
], ids=["age", "ed25519", "rsa"])
@pytest.mark.parametrize("how", ["pasted", "uploaded"])
def test_an_accepted_key_is_shown_with_its_type_fingerprint_and_comment(client, key, shown, how):
    response = upload(client, f"  {key}\r\n") if how == "pasted" else upload(client, file=f"{key}\n".encode())
    assert (response.status_code, response.headers["location"]) == (303, "/settings")
    text = settings_text(client)
    assert shown in text and "No public key on file" not in text


ACCEPTED = "age public key (age1…), or an OpenSSH ssh-ed25519 or ssh-rsa (2048 to 4096 bits) public key"


@pytest.mark.parametrize("text", [
    RSA_2047,
    ECDSA,
    "hello",
    "",
    "age1notakey",
    f"{AGE} laptop",
    "ssh-ed25519",
    "ssh-ed25519 not-base64 alice",
    f"{ED25519}\n{RSA}",
    f"{AGE}\n{AGE}",
], ids=["short rsa", "ecdsa", "junk", "nothing", "bad age", "age with a comment", "no key data", "bad key data",
        "two ssh keys", "two age keys"])
def test_anything_but_one_accepted_key_is_refused_with_the_accepted_types(client, text):
    upload(client, ED25519)
    response = upload(client, text)
    assert response.status_code == 422 and ACCEPTED in response.text
    assert ED25519_FINGERPRINT in settings_text(client)  # the key on file stays


@pytest.mark.parametrize("file", [b"\x00\xff\xfe binary", ED25519.encode() + b"\n" + b" " * 100_000 + AGE.encode()],
                         ids=["binary", "huge"])
def test_an_upload_that_is_not_a_key_file_is_refused(client, file):
    response = upload(client, file=file)
    assert response.status_code == 422 and ACCEPTED in response.text


def test_a_pasted_key_and_an_uploaded_one_together_are_two_keys(client):
    response = upload(client, AGE, file=ED25519.encode())
    assert response.status_code == 422 and ACCEPTED in response.text
    assert "No public key on file" in settings_text(client)


SECRET = "c2VjcmV0IGtleSBtYXRlcmlhbCB0aGF0IG11c3Qgbm90IGJlIGtlcHQ"  # stands for a private key's body


def pem(label: str) -> str:
    return f"-----BEGIN {label}PRIVATE KEY-----\n{SECRET}\n-----END {label}PRIVATE KEY-----\n"


AGE_IDENTITY = str(pyrage.x25519.Identity.generate())


@pytest.mark.parametrize("text,secret", [
    (AGE_IDENTITY, AGE_IDENTITY[16:]),
    (f"# created: 2026-11-26\n# public key: {AGE}\n{AGE_IDENTITY}\n", AGE_IDENTITY[16:]),
    (AGE_IDENTITY.lower(), AGE_IDENTITY[16:].lower()),
    (pem("OPENSSH "), SECRET),
    (pem("RSA "), SECRET),
    (pem(""), SECRET),
    (pem("ENCRYPTED "), SECRET),
    (f"{ED25519}\n{pem('OPENSSH ')}", SECRET),
    (" " * 20_000 + pem("OPENSSH "), SECRET),
], ids=["age", "age-keygen file", "lower-case age", "openssh", "rsa pem", "pkcs8", "encrypted pem",
        "beside a public key", "far into the text"])
@pytest.mark.parametrize("how", ["pasted", "uploaded"])
def test_a_private_key_is_refused_with_a_warning_and_neither_stored_nor_logged(
        client, tmp_path, app_log, text, secret, how):
    app_log.set_level(logging.DEBUG)
    response = upload(client, text) if how == "pasted" else upload(client, file=text.encode())
    assert response.status_code == 422
    assert "private key" in response.text and "should not be shared" in response.text and "replace it" in response.text
    assert secret not in response.text and secret not in app_log.text
    assert not [f for f in (tmp_path / "users").rglob("*") if f.is_file() and secret.encode() in f.read_bytes()]
    assert "No public key on file" in settings_text(client)


def remove(client):
    return client.post("/settings/public-key/remove", follow_redirects=False)


def test_a_second_upload_replaces_the_key(client):
    upload(client, ED25519)
    upload(client, AGE)
    text = settings_text(client)
    assert AGE_FINGERPRINT in text and ED25519_FINGERPRINT not in text


def test_removing_the_key_leaves_none_on_file(client):
    upload(client, ED25519)
    response = remove(client)
    assert (response.status_code, response.headers["location"]) == (303, "/settings")
    assert "No public key on file" in settings_text(client)


def test_remove_asks_for_confirmation_in_a_dialog(client):
    assert "remove-key-dialog" not in client.get("/settings").text  # nothing to remove yet
    upload(client, ED25519)
    page = client.get("/settings").text
    dialog = page.split('<dialog id="remove-key-dialog"', 1)[1].split("</dialog>", 1)[0]
    # The only form that removes the key is the one inside the dialog.
    assert page.count('action="/settings/public-key/remove"') == 1 and 'action="/settings/public-key/remove"' in dialog


def test_each_user_has_their_own_key(build_agent, web_client):
    users = (TEST_USER, "other")
    agent = build_agent(ScriptedLLM(), users=users)
    config = sign_in_for_test(users=users)
    mine = web_client(agent, sign_in_config=config, signed_in=False)
    theirs = new_client(mine.app)  # a second browser on the same app
    for client, user in zip((mine, theirs), users):
        assert sign_in(client, user).status_code == 303

    upload(mine, ED25519)
    assert "No public key on file" in settings_text(theirs)
    upload(theirs, AGE)
    assert ED25519_FINGERPRINT in settings_text(mine) and AGE_FINGERPRINT not in settings_text(mine)
    assert AGE_FINGERPRINT in settings_text(theirs) and ED25519_FINGERPRINT not in settings_text(theirs)
    remove(theirs)
    assert ED25519_FINGERPRINT in settings_text(mine)
