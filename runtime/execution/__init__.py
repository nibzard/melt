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
  bounded run logs. :func:`spawn_run` writes the versioned manifest,
  materializes the captured script, and spawns ``python -m
  melt.runner`` as the leader of its own session, returning the
  :class:`OwnedRunProcess` handle whose ``terminate`` signals the
  owned process group with ``SIGTERM`` and escalates to ``SIGKILL``
  (SPEC.md section 13.5); :func:`launch_run` is the blocking shape of
  the same spawn. Scheduling, admission, and the pre-launch rechecks
  belong to the workspace service (backlog task T016).
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
    CANCEL_GRACE_SECONDS,
    MANIFEST_NAME,
    MeltLaunchError,
    SCRIPT_NAME,
    RunLaunchResult,
    STDERR_LOG_NAME,
    STDOUT_LOG_NAME,
    OwnedRunProcess,
    launch_run,
    materialize_script,
    runner_environment,
    spawn_run,
    write_run_manifest,
)

__all__ = [
    "CANCEL_GRACE_SECONDS",
    "CREDENTIAL_SUFFIXES",
    "MANIFEST_NAME",
    "MODEL_CREDENTIAL_VARIABLES",
    "MeltLaunchError",
    "OwnedRunProcess",
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
    "spawn_run",
    "write_run_manifest",
]
