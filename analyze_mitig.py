#!/usr/bin/env python3
"""Crossover analysis for the mitigation runs.

Usage: python3 analyze_mitig.py [data_dir=data] [loss=0.0]
Needs pandas, numpy, matplotlib.

For each run: total = time to download the whole file (None if it did not finish).
excess = total - median total of the plain, no-outage runs with the same congestion control.
That is the time lost to the outage, including any reconnect handshake and slow start, so it is
directly comparable across strategies. The line y = outage length marks "no worse than just
waiting out the outage".
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

D = sys.argv[1] if len(sys.argv) > 1 else "data"
LOSS = sys.argv[2] if len(sys.argv) > 2 else "0.0"

rows = []
for mp in glob.glob(os.path.join(D, f"m_*_loss{LOSS}_*.meta")):
    jp = mp[:-5] + ".json"
    if not os.path.exists(jp):
        continue
    meta = json.load(open(mp))
    c = json.load(open(jp))
    rows.append(dict(label=meta["label"], cc=meta["cc"], outage=meta["outage"], seed=meta["seed"],
                     total=c["total_time"] if c.get("complete") else np.nan,
                     reconnects=sum(1 for e in c["events"] if e[0] == "reconnect")))
df = pd.DataFrame(rows)
if df.empty:
    sys.exit("no mitigation runs found (check data dir / loss argument)")

base = df[(df.outage == 0) & df.label.isin(["cubic", "bbr"])].groupby("cc").total.median()
print("baseline (no outage) download time per cc:\n", base.round(2).to_string(), "\n")
df["excess"] = df.total - df.cc.map(base)
df.to_csv(os.path.join(D, f"mitig_summary_loss{LOSS}.csv"), index=False)

q25 = lambda s: s.quantile(.25)
q75 = lambda s: s.quantile(.75)
tab = df[df.outage > 0].groupby(["label", "outage"]).agg(
    n=("seed", "count"), excess_med=("excess", "median"), q25=("excess", q25), q75=("excess", q75),
    reconnects=("reconnects", "median"), unfinished=("total", lambda s: int(s.isna().sum())))
print(tab.round(2).to_string())

# crossover: outage lengths where a strategy beats vanilla cubic (median excess)
# extra delay beyond the outage itself (excess - outage), averaged over the lengths that
# cubic, watchdog_N1 and oracle were all run at, so the grids are comparable
_sets = [set(tab.loc[l].index) for l in ("cubic", "watchdog_N1", "oracle")
         if l in tab.index.get_level_values(0)]
if len(_sets) == 3:
    common = sorted(set.intersection(*_sets))
    print(f"\nmean extra delay beyond the outage over {len(common)} common lengths "
          f"({common[0]:g}-{common[-1]:g} s):")
    for lab in sorted(set(tab.index.get_level_values(0))):
        t = tab.loc[lab]["excess_med"]
        vals = [t[o] - o for o in common if o in t.index]
        if len(vals) == len(common):
            print(f"  {lab:18s} mean {np.mean(vals):.2f} s   max {np.max(vals):.2f} s")

ref = tab.loc["cubic"]["excess_med"] if "cubic" in tab.index.get_level_values(0) else None
if ref is not None:
    print("\noutage lengths where median excess < vanilla cubic:")
    for lab in sorted(set(tab.index.get_level_values(0)) - {"cubic"}):
        t = tab.loc[lab]["excess_med"]
        wins = [f"{o:g}" for o in t.index if o in ref.index and t[o] < ref[o]]
        print(f"  {lab:18s}", ", ".join(wins) if wins else "none")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for lab, s in tab.reset_index().groupby("label"):
        ax.errorbar(s.outage, s.excess_med,
                    yerr=[(s.excess_med - s.q25).clip(lower=0), (s.q75 - s.excess_med).clip(lower=0)],
                    marker="o", capsize=2, label=lab)
    xs = sorted(df[df.outage > 0].outage.unique())
    ax.plot(xs, xs, "k:", lw=.8, label="outage length")
    ax.set_xlabel("outage length (s)")
    ax.set_ylabel("extra download time vs no outage (s), median/IQR")
    ax.legend(fontsize=8)
    fig.tight_layout()
    out = os.path.join(D, f"crossover_loss{LOSS}.png")
    fig.savefig(out, dpi=150)
    print("saved", out)
except ImportError:
    pass
