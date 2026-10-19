"""Calculate one synthetic café scenario from captured inputs."""
from decimal import Decimal, ROUND_HALF_UP

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from melt import ui

days = pd.read_parquet(ui.input_path("days"))
increase = Decimal(str(ui.params["bean_increase"])) / 100
rows = []
for row in days.itertuples(index=False):
    count = int(row.days) if ui.params["open_mondays"] or row.day != "monday" else 0
    beans = int((Decimal(int(row.bean_cents)) * (1 + increase)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    cost = beans + int(row.other_cost_cents)
    revenue = int(row.revenue_cents) * count
    rows.append({"day": row.day, "open_days": count, "revenue_cents": revenue,
                 "cost_cents": cost * count, "profit_cents": revenue - cost * count})
daily = pd.DataFrame(rows)
revenue, cost, profit = (int(daily[key].sum()) for key in ("revenue_cents", "cost_cents", "profit_cents"))
open_days = int(daily["open_days"].sum())


def euros(cents):
    return f"€{Decimal(cents) / 100:,.2f}"


summary = (f"# Little Hours\n\nMonthly revenue: {euros(revenue)}\n\n"
           f"Monthly profit: {euros(profit)}\n\nProfit margin: {100 * profit / revenue:.1f}%\n\n"
           f"Bean costs: +{ui.params['bean_increase']}%\n\n"
           f"Mondays: {'open' if ui.params['open_mondays'] else 'closed'}\n\n"
           "Synthetic data. Closing a day avoids all costs assigned to that day. "
           "The model excludes taxes and changes in demand.\n")
break_even = (f"# Break-even\n\n{euros(Decimal(cost) / open_days)} per open day\n\n"
              f"Current revenue: {euros(Decimal(revenue) / open_days)} per open day\n\n"
              f"{open_days} open days. Based on the captured cost assumptions.\n")
fig, ax = plt.subplots(figsize=(8, 4), layout="constrained")
values = [row["profit_cents"] / row["open_days"] / 100 if row["open_days"] else 0 for row in rows]
ax.bar([row["day"][:3].title() for row in rows], values,
       color=["#b58474" if value < 0 else "#247460" for value in values])
ax.axhline(0, color="#a4aaa7", linewidth=.8)
ax.set_ylabel("Profit per open day (€)")
ax.set_title("A typical week")
ax.spines[["top", "right"]].set_visible(False)
ui.output("daily", daily)
ui.output("profit", fig)
ui.output("summary", summary)
ui.output("break_even", break_even)
plt.close(fig)
