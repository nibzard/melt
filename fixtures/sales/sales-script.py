"""The registered sales script of the hero workflow (SPEC.md 13.2, 18.2).

The command-line harness registers this file through
``action.register`` and runs it through the workspace service. It is
the example script of SPEC.md section 13.2 word for word, except that
the figure closes after both outputs are staged.
"""

import pandas as pd
import matplotlib.pyplot as plt
from melt import ui

sales = pd.read_parquet(ui.input_path("sales"))
selected = sales[sales["revenue"] >= ui.params["minimum"]]
monthly = selected.groupby("month", sort=True)["revenue"].sum()
fig, ax = plt.subplots()
monthly.plot(ax=ax)
ax.set_ylabel("Revenue")
ui.output("filtered", selected)
ui.output("monthly", fig)
plt.close(fig)
