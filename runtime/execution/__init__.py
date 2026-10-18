"""Execution: registry, generations, jobs, and publication (SPEC.md section 13).

Backlog tasks T013 through T017 implement registration, scheduling,
invocation snapshots, and atomic publication of output groups.

The one boundary this package owns today is the project process
environment (SPEC.md sections 13.2 and 15.1): model credentials never
reach a project process. Every launch site builds its child
environment through :func:`project_process_environment` and guards it
with :func:`assert_no_model_credentials`.
"""

from __future__ import annotations

from runtime.execution.environment import (
    CREDENTIAL_SUFFIXES,
    MODEL_CREDENTIAL_VARIABLES,
    ProjectEnvironmentError,
    assert_no_model_credentials,
    find_credential_variables,
    is_credential_variable,
    project_process_environment,
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
