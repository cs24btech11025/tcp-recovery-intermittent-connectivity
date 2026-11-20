#!/usr/bin/env python3
"""Model vs measurement for the wait between link restore and first data arriving.

Usage: python3 model_vs_data.py [data_dir=data] [buf=125] [loss=0.0]
Run analyze.py first (it writes data/summary_buf{BUF}_loss{LOSS}.csv).
Needs pandas, numpy, matplotlib.

Model (plain RFC 6298 exponential backoff; NO TLP, RACK or F-RTO):
  R      = initial RTO at the cut, read from the run's ss log:
           first data-connection row showing "rto:X backoff:b"  ->  R = X / 2**b
  t_ack  = gap_start + OWD          last ACK reaches the sender one way-delay after the
                                    last data packet reached the client (OWD = 0.05 s here)
  S_k    = t_ack + R*(2**k - 1)     k-th retransmission send time, k = 1, 2, ...
  first useful retransmission = first S_k >= t_restore_cmd (it must be sent after the link is back)
  predicted gap_end   = S_k + OWD
  predicted wait      = predicted gap_end - t_restore_cmd
Residual = measured wait - predicted wait. Where it is not near zero, the model is missing
something (TLP shifting the first timeout, RACK, queue drain, command latency of tc ...):
that disagreement analysis is the interesting part for the report.
"""
import json, os, re, sys
import numpy as np
import pandas as pd

D = sys.argv[1] if len(sys.argv) > 1 else "data"
BUF = sys.argv[2] if len(sys.argv) > 2 else "125"
LOSS = sys.argv[3] if len(sys.argv) > 3 else "0.0"
OWD = 0.05
RX = re.compile(r"rto:(\d+) backoff:(\d+)")


def read_R(ss_path):
    try:
        for line in open(ss_path):
            if "notsent" not in line:  # data connection only (not iperf3 control)
                continue
            m = RX.search(line)
            if m:
                return int(m.group(1)) / (2 ** int(m.group(2))) / 1000.0
    except OSError:
        pass
    return np.nan


csv = os.path.join(D, f"summary_buf{BUF}_loss{LOSS}.csv")
df = pd.read_csv(csv)
df = df[df.gap_s > 0.2].copy()  # runs with a real silence only

rows = []
for r in df.itertuples():
    tag = f"{r.algo}_buf{BUF}_out{r.outage_cmd}_loss{LOSS}_seed{r.seed}"
    base = os.path.join(D, tag)
    if not os.path.exists(base + ".meta"):
        continue
    tr = json.load(open(base + ".meta"))["t_restore_cmd"]
    R = read_R(base + ".ss")
    if np.isnan(R):
        continue
    ge = tr + r.wait_after_restore_s      # measured gap end (absolute)
    gs = ge - r.gap_s                     # measured gap start
    t_ack = gs + OWD
    k = 1
    while t_ack + R * (2 ** k - 1) < tr and k < 12:
        k += 1
    pred_ge = t_ack + R * (2 ** k - 1) + OWD
    rows.append(dict(algo=r.algo, outage=r.outage_cmd, seed=r.seed, R=R, k=k,
                     measured_wait=r.wait_after_restore_s, predicted_wait=pred_ge - tr))

m = pd.DataFrame(rows)
if m.empty:
    sys.exit("no runs with a backoff row in the ss log (outages too short?)")
m["residual"] = m.measured_wait - m.predicted_wait
m.to_csv(os.path.join(D, f"model_vs_data_buf{BUF}_loss{LOSS}.csv"), index=False)

tab = m.groupby(["algo", "outage"]).agg(
    n=("seed", "count"), R_med=("R", "median"), k_med=("k", "median"),
    meas=("measured_wait", "median"), pred=("predicted_wait", "median"),
    resid_med=("residual", "median"),
    within_0p15=("residual", lambda s: float((s.abs() <= 0.15).mean())))
print(tab.round(2).to_string())
print("\noverall: median residual %.3f s, IQR [%.3f, %.3f], %.0f%% of runs within +/-0.15 s" % (
    m.residual.median(), m.residual.quantile(.25), m.residual.quantile(.75),
    100 * (m.residual.abs() <= 0.15).mean()))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5, 5))
    for algo, s in m.groupby("algo"):
        ax.scatter(s.predicted_wait, s.measured_wait, s=14, alpha=.6, label=algo)
    hi = max(m.predicted_wait.max(), m.measured_wait.max()) * 1.05
    ax.plot([0, hi], [0, hi], "k:", lw=.8)
    ax.set_xlabel("predicted wait after restore (s)")
    ax.set_ylabel("measured wait after restore (s)")
    ax.set_title(f"RTO-backoff model, buf {BUF} KB, loss {LOSS}%")
    ax.legend()
    fig.tight_layout()
    out = os.path.join(D, f"model_vs_data_buf{BUF}_loss{LOSS}.png")
    fig.savefig(out, dpi=150)
    print("saved", out)
except ImportError:
    pass
