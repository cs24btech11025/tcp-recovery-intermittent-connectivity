#!/usr/bin/env python3
"""Phase B analysis. Needs pandas, numpy, outage_schedule.py, and data/mitig_summary_loss*.csv
(written by analyze_mitig.py).

PREDICT (before running Phase B):
  ~/cnenv/bin/python analyze_phaseb.py --predict-only --seeds 6
Prediction method: for each outage in a seed's schedule, take the median extra download time of
that strategy for a single outage of that length (interpolated from the Phase A mitigation runs;
(0, 0) anchored; beyond the longest measured length it is extrapolated as +1 s per s), sum over
the schedule, and predict goodput = baseline * (1 - lost_time / run_len). This tests whether
single-outage costs ADD UP over a multi-outage run.

MEASURED:
  ~/cnenv/bin/python analyze_phaseb.py [--mode fitted|fixed] [--loss 0.0]
Prints per strategy: median/IQR goodput, paired differences vs vanilla CUBIC (same seed =
identical outage sequence), and measured vs predicted goodput.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

from outage_schedule import make_schedule

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data")
ap.add_argument("--speed", default="350")
ap.add_argument("--mode", default="fitted")
ap.add_argument("--loss", default="0.0")
ap.add_argument("--predict-only", action="store_true")
ap.add_argument("--seeds", type=int, default=6)
ap.add_argument("--base", type=float, default=9.5, help="baseline goodput (Mbps) for --predict-only")
ap.add_argument("--run-len", type=float, default=150.0)
a = ap.parse_args()

ms = pd.read_csv(os.path.join(a.data, f"mitig_summary_loss{a.loss}.csv"))
tf = {}
for lab, g in ms[ms.outage > 0].groupby("label"):
    tf[lab] = g.groupby("outage").excess.median().dropna()


def lost_time(label, durs):
    s = tf[label]
    xs, ys = np.r_[0.0, s.index.values], np.r_[0.0, s.values]
    tot = 0.0
    for d in durs:
        tot += np.interp(d, xs, ys) if d <= xs[-1] else ys[-1] + (d - xs[-1])
    return tot


if a.predict_only:
    rows = []
    for seed in range(1, a.seeds + 1):
        durs = [e["dur"] for e in make_schedule(seed, a.speed, a.mode, a.run_len)]
        for lab in tf:
            lo = lost_time(lab, durs)
            rows.append(dict(seed=seed, label=lab, outages=len(durs), outage_s=sum(durs),
                             lost_s=lo, pred_gp=a.base * (1 - lo / a.run_len)))
    p = pd.DataFrame(rows)
    print(f"schedules (seeds 1-{a.seeds}): median outages/run "
          f"{p.groupby('seed').outages.first().median():.0f}, "
          f"median commanded outage {p.groupby('seed').outage_s.first().median():.1f} s")
    print(p.groupby("label")[["lost_s", "pred_gp"]].median().round(2).to_string())
    sys.exit(0)

rows, skipped = [], []
for mode in (a.mode, "none"):
    for mp in glob.glob(os.path.join(a.data, f"b_*_{a.speed}_{mode}_loss{a.loss}_*.meta")):
        jp = mp[:-5] + ".json"
        if not os.path.exists(jp):
            continue
        meta, c = json.load(open(mp)), json.load(open(jp))
        tl = c.get("timeline") or [[a.run_len, 0]]
        t_end = max(tl[-1][0], 1.0)
        if abs(t_end - a.run_len) > 3.0:  # host suspend / clock jump: run is not valid
            skipped.append((os.path.basename(mp)[:-5], round(t_end, 1)))
            continue
        rows.append(dict(label=meta["label"], cc=meta["cc"], mode=mode, seed=meta["seed"],
                         gp=c.get("got", 0) * 8 / t_end / 1e6,
                         durs=[e["dur"] for e in meta["events"]],
                         reconnects=sum(1 for e in c["events"] if e[0] == "reconnect")))
if skipped:
    print("EXCLUDED (client time axis != run length; rerun these):", skipped)
df = pd.DataFrame(rows)
if df.empty:
    sys.exit("no Phase B runs found")
base = df[df["mode"] == "none"].groupby("cc").gp.median()
print("baseline goodput (no outages), Mbps:", base.round(2).to_dict())
d = df[df["mode"] == a.mode].copy()
d["pred_gp"] = [base[r.cc] * (1 - lost_time(r.label, r.durs) / a.run_len)
                if r.label in tf and r.cc in base.index else np.nan for r in d.itertuples()]
d["err"] = d.gp - d.pred_gp
q25, q75 = (lambda s: s.quantile(.25)), (lambda s: s.quantile(.75))
tab = d.groupby("label").agg(n=("seed", "count"), gp_med=("gp", "median"), q25=("gp", q25),
                             q75=("gp", q75), reconnects=("reconnects", "median"),
                             pred_med=("pred_gp", "median"), err_med=("err", "median"))
print("\n" + tab.round(2).to_string())

print("\nper-seed error, measured - predicted goodput (Mbps):")
print(d.pivot_table(index="seed", columns="label", values="err").round(2).to_string())

piv = d.pivot_table(index="seed", columns="label", values="gp")
if "cubic" in piv.columns:
    print("\npaired difference vs vanilla CUBIC (Mbps), same outage sequence per seed:")
    for lab in piv.columns:
        if lab == "cubic":
            continue
        diff = (piv[lab] - piv["cubic"]).dropna()
        if len(diff):
            print(f"  {lab:14s} median {diff.median():+.2f}  range [{diff.min():+.2f}, {diff.max():+.2f}]"
                  f"  better in {int((diff > 0).sum())}/{len(diff)} seeds")

# Ideal switcher (prediction only): per outage, take the cheaper of waiting (vanilla cubic) or
# reconnecting (oracle), using the single-outage transfer functions. Shows the headroom that a
# policy deciding WHEN to reconnect could have over always-reconnect (oracle) or a fixed threshold.
if "cubic" in tf and "oracle" in tf and "cubic" in base.index:
    def _lt(lab, dur):
        s = tf[lab]
        xs, ys = np.r_[0.0, s.index.values], np.r_[0.0, s.values]
        return np.interp(dur, xs, ys) if dur <= xs[-1] else ys[-1] + (dur - xs[-1])
    ideal = [base["cubic"] * (1 - sum(min(_lt("cubic", x), _lt("oracle", x)) for x in r.durs) / a.run_len)
             for r in d[d.label == "cubic"].itertuples()]
    print(f"\nideal switcher (reconnect only when cheaper than waiting), PREDICTED median goodput: "
          f"{np.median(ideal):.2f} Mbps  (not measured)")
