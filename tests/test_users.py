"""The configured Users: the USERS setting, its password hashes, and the command that makes one."""

import pytest
from conftest import TEST_PASSWORD_HASH as HASH
from pydantic import ValidationError

from flood_agent.__main__ import build_app
from flood_agent.config import Settings
from flood_agent.users import main, parse_users


def run_command(monkeypatch, *passwords, argv=("alice",)) -> int:
    """The hash command, with `passwords` typed at its no-echo prompts in turn."""
    typed = iter(passwords)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(typed))
    return main(list(argv))


def test_the_hash_commands_output_verifies_against_its_password_and_no_other(monkeypatch, capsys):
    assert run_command(monkeypatch, "s3cret pass", "s3cret pass") == 0

    printed = capsys.readouterr().out
    alice = parse_users(printed.strip())["alice"]
    assert alice.verify("s3cret pass")
    assert not alice.verify("s3cret pas") and not alice.verify("")
    assert "s3cret" not in printed


def test_the_hash_command_salts_each_hash(monkeypatch, capsys):
    entries = []
    for _ in range(2):
        run_command(monkeypatch, "same", "same")
        entries.append(capsys.readouterr().out)

    assert entries[0] != entries[1]


@pytest.mark.parametrize(
    "argv, passwords, problem",
    [
        (["alice"], ["one", "two"], "do not match"),
        (["alice"], ["", ""], "empty"),
        (["Alice"], [], "'Alice'"),
    ],
)
def test_the_hash_command_prints_no_entry_for_a_bad_name_or_password(monkeypatch, capsys, argv, passwords, problem):
    assert run_command(monkeypatch, *passwords, argv=argv) != 0

    out, err = capsys.readouterr()
    assert out == "" and problem in err


def settings_from_env_file(tmp_path, *lines) -> Settings:
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{line}\n" for line in lines))
    return Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("line", [None, "USERS=", "USERS=  ", "USERS=,"])
def test_the_app_refuses_to_start_without_users(tmp_path, line):
    with pytest.raises((ValueError, RuntimeError), match="USERS is (not set|empty)"):
        build_app(settings_from_env_file(tmp_path, *([line] if line else [])))


@pytest.mark.parametrize(
    "users, problem",
    [
        (f"Alice:{HASH}", "invalid User name 'Alice'"),
        (f"1bob:{HASH}", "invalid User name '1bob'"),
        (f"{'a' * 32}:{HASH}", "invalid User name"),
        (f"al/ice:{HASH}", "invalid User name 'al/ice'"),
        (f":{HASH}", "invalid User name ''"),
        (f"alice:{HASH},bob:{HASH},alice:{HASH}", "'alice' appears twice"),
        ("hunter2", "entry 1 is not name:hash"),
        ("alice:", "malformed password hash for User 'alice'"),
        ("alice:hunter2", "malformed password hash for User 'alice'"),
        (f"alice:{HASH.replace('scrypt', 'md5')}", "malformed password hash"),
        (f"alice:{HASH.replace('$16$', '$17$')}", "malformed password hash"),  # n is not a power of two
        (f"alice:{HASH.replace('$16$', '$1073741824$')}", "malformed password hash"),  # needs 1 TiB
        (f"alice:{HASH[:-3]}", "malformed password hash"),  # the digest is cut off
        (f"alice:{HASH},bob:scrypt$16$8$1$x", "malformed password hash for User 'bob'"),
    ],
)
def test_settings_refuse_a_bad_users_list_naming_the_problem_but_not_the_value(tmp_path, users, problem):
    with pytest.raises(ValidationError, match=problem) as refused:
        settings_from_env_file(tmp_path, f"USERS={users}")

    assert "hunter2" not in str(refused.value) and HASH not in str(refused.value)


def test_users_are_read_literally_from_the_env_file(tmp_path):
    settings = settings_from_env_file(tmp_path, f"USERS=alice:{HASH}, bob_2:{HASH}")

    assert list(settings.sign_in_config().users) == ["alice", "bob_2"]


def test_the_session_cookie_is_secure_except_under_the_dev_server(tmp_path):
    settings = settings_from_env_file(tmp_path, f"USERS=alice:{HASH}")

    assert settings.sign_in_config().cookie_secure
    assert not settings.sign_in_config(dev=True).cookie_secure
    assert settings.sign_in_config().idle_timeout_ms == 12 * 3_600_000
