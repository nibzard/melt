# 4. Little Hours café

Build a real café dashboard with saved controls, Python calculations, and published results.
The example uses synthetic data and the experimental `melt/semantic-draft-1` language.

From the repository root, run:

```bash
uv run --locked python3 examples/04-cafe/run.py --output /tmp/little-hours
```

Use a new or empty output directory.
Follow the [shared setup](../README.md#setup) first.

The example imports a 30-day cost model and registers the included calculation.
It explicitly grants execution permission to that calculation, runs it, and publishes four outputs together.
Every harness call uses a fresh process, so later reads also exercise restoration.

The workflow verifies:

1. The baseline monthly profit is €3,280.00.
2. Set bean costs to +30% and close Mondays.
3. Run the saved action. Monthly profit becomes €2,836.00.
4. Add a break-even view over its existing output without running the calculation again.
5. Reopen with the same controls, values, layout, and results.

The output directory contains the plot, summary, daily table, command receipts, and durable workspace.
`add-break-even.json` records the view patch.
`Little Hours.meltapp` points to the workspace through its absolute local data root.

The launcher contains no data copy or execution permission.
Keep the workspace directory in place.
It is a local launcher, not a portable app package.

## Open the native host

On macOS, build the [native host](../../native/README.md), then open the saved reference:

```bash
open -a "$PWD/build/native/Melt.app" "/tmp/little-hours/Little Hours.meltapp"
```

Use the parameter fields and **Run scenario** control to calculate another scenario.
Use **Save to Desktop** to save another reference to the same workspace.
Closing and opening the host must restore results without running the calculation.

The native host source has not been compiled or exercised in the Linux development environment.
The example and its service boundary run on Linux.

## Calculation assumptions

Amounts use integer euro cents. Bean increases round to the nearest cent for each open day.
The fixture has four Mondays and 26 other days.
Each baseline Monday loses €76.20; the bean increase makes that loss €105.00.
Closing Mondays avoids their assigned costs and revenue, improving monthly profit by €420.00.

The model assumes that all costs assigned to a closed day are avoided.
It excludes taxes, fixed costs on closed days, and changes in demand.
The reported break-even value is total scenario costs divided by the number of open days.
