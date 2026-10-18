"""Model credentials never reach project processes (T014, SPEC.md 15.1).

A project run executes with the user's operating-system authority, but
the service keeps one hard boundary: model credentials and other
provider secrets never enter a project process environment. The launch
sites of backlog tasks T015 and T016 build every child environment
through `runtime.execution.project_process_environment` and guard it
with `assert_no_model_credentials`.
"""

from __future__ import annotations

import pytest

from runtime.execution import (
    MODEL_CREDENTIAL_VARIABLES,
    ProjectEnvironmentError,
    assert_no_model_credentials,
    find_credential_variables,
    is_credential_variable,
    project_process_environment,
)


def test_known_model_credentials_are_credential_variables():
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "HF_TOKEN",
    ):
        assert name in MODEL_CREDENTIAL_VARIABLES
        assert is_credential_variable(name)


def test_credential_suffix_rules_catch_unlisted_providers():
    for name in (
        "NEW_PROVIDER_API_KEY",
        "NEW_PROVIDER_API_KEY_FILE",
        "NEW_PROVIDER_AUTH_TOKEN",
        "NEW_PROVIDER_ACCESS_TOKEN",
        "NEW_PROVIDER_TOKEN_FILE",
        "NEW_PROVIDER_CREDENTIALS",
        "NEW_PROVIDER_CREDENTIALS_FILE",
        "NEW_PROVIDER_SECRET_ACCESS_KEY",
        "NEW_PROVIDER_SESSION_TOKEN",
    ):
        assert is_credential_variable(name), name


def test_non_credential_variables_pass_through():
    for name in (
        "PATH",
        "HOME",
        "LANG",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "XDG_DATA_HOME",
        # A source-control token is not a model credential; the rule
        # keeps it so project tooling that expects it keeps working.
        "GITHUB_TOKEN",
    ):
        assert not is_credential_variable(name), name


def test_project_process_environment_strips_credentials_and_keeps_the_rest():
    environment = project_process_environment(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/agent",
            "VIRTUAL_ENV": "/opt/venv",
            "LANG": "C.UTF-8",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "OPENAI_API_KEY": "sk-openai-secret",
            "AWS_SESSION_TOKEN": "aws-secret",
            "FUTURE_PROVIDER_API_KEY": "future-secret",
        }
    )
    assert environment == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/agent",
        "VIRTUAL_ENV": "/opt/venv",
        "LANG": "C.UTF-8",
    }


def test_find_credential_variables_names_every_leak_sorted():
    leaked = find_credential_variables(
        {"OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b", "PATH": "/bin"}
    )
    assert leaked == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]


def test_assert_no_model_credentials_accepts_clean_environments():
    assert_no_model_credentials({"PATH": "/usr/bin", "HOME": "/home/a"})
    assert_no_model_credentials(project_process_environment({"X": "1"}))


def test_assert_no_model_credentials_refuses_a_leak_by_name():
    with pytest.raises(ProjectEnvironmentError) as error:
        assert_no_model_credentials(
            {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk", "GEMINI_API_KEY": "g"}
        )
    message = str(error.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "GEMINI_API_KEY" in message
    assert "sk" not in message, "the guard names variables, never values"


def test_the_default_environment_path_is_guarded_too():
    # The default reads os.environ: whatever it holds, the sanitized
    # result is clean by construction and the guard accepts it.
    sanitized = project_process_environment()
    assert_no_model_credentials(sanitized)
    assert find_credential_variables(sanitized) == []
