"""Execution: registry, generations, jobs, and publication (SPEC.md section 13).

Backlog tasks T013 through T017 implement registration, scheduling,
invocation snapshots, and atomic publication of output groups.

Two boundaries of this package exist today:

- The project process environment (SPEC.md sections 13.2 and 15.1):
  model credentials never reach a project process. Every launch site
  builds its child environment through
  :func:`project_process_environment` and guards it with
  :func:`assert_no_model_credentials`.
- The launch harness (SPEC.md sections 13.1 and 13.2): one fresh
  process per run, no package installation, a credential-free child
  environment, the private staging directory as working directory, and
  bounded run logs. :func:`launch_run` writes the versioned manifest,
  materializes the captured script, and spawns ``python -m
  melt.runner``. Scheduling and admission belong to the workspace
  service (backlog task T016).
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
from runtime.execution.launcher import (
    MANIFEST_NAME,
    MeltLaunchError,
    SCRIPT_NAME,
    RunLaunchResult,
    STDERR_LOG_NAME,
    STDOUT_LOG_NAME,
    launch_run,
    materialize_script,
    runner_environment,
    write_run_manifest,
)

__all__ = [
    "CREDENTIAL_SUFFIXES",
    "MANIFEST_NAME",
    "MODEL_CREDENTIAL_VARIABLES",
    "MeltLaunchError",
    "ProjectEnvironmentError",
    "SCRIPT_NAME",
    "RunLaunchResult",
    "STDERR_LOG_NAME",
    "STDOUT_LOG_NAME",
    "assert_no_model_credentials",
    "find_credential_variables",
    "is_credential_variable",
    "launch_run",
    "materialize_script",
    "project_process_environment",
    "runner_environment",
    "write_run_manifest",
]
