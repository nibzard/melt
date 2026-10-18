"""Environments of project processes (SPEC.md sections 13.2 and 15.1).

A project run executes with the user's operating-system authority. A
virtual environment contains neither filesystem nor network access, and
this milestone does not promise safe execution of hostile code. The one
hard boundary the service keeps on every project process is
credentials: model credentials and other provider secrets never reach
project code (SPEC.md section 15.1).

The service holds model credentials for itself; a project process
receives its environment through :func:`project_process_environment`,
which removes every credential-bearing variable before launch, and the
launcher guards the result with :func:`assert_no_model_credentials`.
The runner and the scheduling paths of backlog tasks T015 and T016
build every project process through this module, so no launch site can
hand credentials through inheritance by accident.
"""

from __future__ import annotations

import os
from typing import Mapping

MODEL_CREDENTIAL_VARIABLES = frozenset(
    {
        # Anthropic and Claude identifiers.
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY_FILE",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # OpenAI and Azure OpenAI identifiers.
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_AD_TOKEN",
        # Google and Gemini identifiers.
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        # Cloud provider credentials of every provider a model adapter
        # may read.
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_SHARED_CREDENTIALS_FILE",
        # Further model provider identifiers the pinned stack reads.
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "TOGETHER_API_KEY",
        "FIREWORKS_API_KEY",
        "COHERE_API_KEY",
        "PERPLEXITY_API_KEY",
        "REPLICATE_API_TOKEN",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    }
)
"""Variables that always carry model or provider credentials."""

CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_API_KEY_FILE",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_SESSION_TOKEN",
    "_TOKEN_FILE",
    "_CREDENTIALS",
    "_CREDENTIALS_FILE",
    "_SECRET_ACCESS_KEY",
)
"""Name suffixes that mark a variable as credential material.

The suffix rules catch credentials of providers this list does not name
yet. They keep non-credential tokens that other tooling expects, such
as a plain ``GITHUB_TOKEN``, untouched.
"""


class ProjectEnvironmentError(RuntimeError):
    """Raised when an environment for a project process holds credentials."""


def is_credential_variable(name: str) -> bool:
    """Report whether one variable name carries credential material."""

    return name in MODEL_CREDENTIAL_VARIABLES or name.endswith(
        CREDENTIAL_SUFFIXES
    )


def find_credential_variables(
    environ: "Mapping[str, str] | None" = None,
) -> list[str]:
    """Return the sorted credential variables one environment holds."""

    source = os.environ if environ is None else environ
    return sorted(name for name in source if is_credential_variable(name))


def project_process_environment(
    environ: "Mapping[str, str] | None" = None,
) -> dict[str, str]:
    """Return the environment one project process receives.

    Every credential-bearing variable is removed. The remaining
    operating-system environment stays, so the resolved interpreter,
    the virtual-environment pointers, and the locale keep working.
    """

    source = os.environ if environ is None else environ
    return {
        name: value for name, value in source.items()
        if not is_credential_variable(name)
    }


def assert_no_model_credentials(environ: Mapping[str, str]) -> None:
    """Refuse an environment that still carries credentials.

    The launcher calls this right before it spawns a project process,
    so a leak fails loudly at the launch site instead of silently
    reaching project code (SPEC.md section 15.1).
    """

    leaked = find_credential_variables(environ)
    if leaked:
        raise ProjectEnvironmentError(
            "the project process environment still carries model"
            " credentials: " + ", ".join(leaked)
        )


__all__ = [
    "CREDENTIAL_SUFFIXES",
    "MODEL_CREDENTIAL_VARIABLES",
    "ProjectEnvironmentError",
    "assert_no_model_credentials",
    "find_credential_variables",
    "is_credential_variable",
    "project_process_environment",
]
