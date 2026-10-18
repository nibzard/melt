"""The Linux command-line harness of round 1 (SPEC.md sections 2.2, 18.2).

The harness is the stand-in for the native host while development runs
on Linux. It represents one local user: every command opens the
workspace service, drives one request through the same framed channel
and the same service handlers the later native host uses, prints the
answer as JSON, and closes the service again. Because each command
reopens the durable store, the loop also proves restart recovery one
invocation at a time.

The full loop rides the command line:

```bash
python3 -m runtime.harness --root $DATA_ROOT --workspace sales-demo \
    create --document fixtures/sales/workspace.json
python3 -m runtime.harness ... import --source sales-input \
    --file fixtures/sales/sales.parquet --label sales.csv
python3 -m runtime.harness ... register \
    --script fixtures/sales/sales-script.py \
    --contract fixtures/sales/contract.json
python3 -m runtime.harness ... apply \
    --operations fixtures/sales/definitions.json --registration <id>
python3 -m runtime.harness ... set --parameter minimum --value 200 \
    --dispatch --debounce-wait 400 --launch --publish
python3 -m runtime.harness ... approve --registration <id>
python3 -m runtime.harness ... invoke --action analyze-sales \
    --launch --publish
python3 -m runtime.harness ... table --source analyze-sales-filtered
python3 -m runtime.harness ... artifact --source analyze-sales-monthly \
    --output monthly.png
```

Event origin is part of the contract (SPEC.md sections 2.2 and 9.1).
The harness speaks for the local user by default; the ``--origin
programmatic`` flag of the event-producing commands routes the same
request through the agent-adapter path, so an admitted intent records
a programmatic origin instead of a user origin.
"""

from __future__ import annotations

from runtime.harness.cli import build_parser, main

__all__ = ["build_parser", "main"]
