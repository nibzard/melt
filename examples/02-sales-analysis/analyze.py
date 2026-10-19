"""Filter sales and plot monthly revenue for the captured minimum."""

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
