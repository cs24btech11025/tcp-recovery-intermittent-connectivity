#!/usr/bin/env python3
"""Pcap-based analysis of run_one.py output (replaces the iperf3-interval version).

Usage: python3 analyze.py [data_dir=data] [buf=125] [loss=0.0]
Needs: pandas, numpy, matplotlib, and tcpdump (reads the .pcap files as text).

Per run, from packets that ARRIVE at the client (capture is on veth-c):
  gap_start / gap_end   : last data packet before / first after the first silence > 50 ms
                          that begins near the commanded cut (plan: boundaries from the capture)
  gap_s                 : measured silence = gap_end - gap_start (x-axis for short outages)
  wait_after_restore_s  : gap_end - t_restore_cmd. How long TCP sat idle after the link was
                          already back = the RTO-schedule wait the analytic model predicts.
  recov_after_gap_s     : time from gap_end until goodput stays >= 90% of the pre-outage mean
                          for 1 s (100 ms bins, unique bytes only)
  recov_from_restore_s  : same endpoint, measured from the commanded restore time
                          (the plan's "link coming back" definition; tc time is approximate,
                          tens of ms of command latency)
  degraded_s            : gap_start -> recovered
Runs with no silence > 50 ms are reported with all zeros ("invisible to TCP").
"""
import glob, json, os, re, subprocess, sys
import numpy as np
import pandas as pd

D = sys.argv[1] if len(sys.argv) > 1 else "data"
BUF = sys.argv[2] if len(sys.argv) > 2 else "125"
LOSS = sys.argv[3] if len(sys.argv) > 3 else "0.0"

DT = 0.1
GAP_MIN = 0.05
RX = re.compile(r"^(\d+\.\d+) IP (\d+\.\d+\.\d+\.\d+)\.(\d+) > (\d+\.\d+\.\d+\.\d+)\.(\d+): "
                r".*?seq (\d+):(\d+).*?length (\d+)")


def arrivals(pcap):
    """Return (times, unique_bytes) of data segments from the server arriving at the client."""
    out = subprocess.run(["tcpdump", "-nn", "-tt", "-r", pcap, "tcp and src port 5201"],
                         capture_output=True, text=True).stdout
    seen = set()
    T, B = [], []
    for line in out.splitlines():
        m = RX.match(line)
        if not m:
            continue
        ln = int(m.group(8))
        if ln < 100:  # skip iperf3 control-channel chatter
            continue
        key = (m.group(5), m.group(6))
        new = key not in seen
        seen.add(key)
        T.append(float(m.group(1)))
        B.append(ln if new else 0)  # duplicate retransmissions are arrivals but not goodput
    return np.array(T), np.array(B)


def goodput(T, B, lo, hi):
    edges = np.arange(lo, hi + 1e-9, DT)
    if len(edges) < 2:
        return np.array([])
    return np.histogram(T, bins=edges, weights=B)[0] * 8 / DT


def analyze(base):
    meta = json.load(open(base + ".meta"))
    T, B = arrivals(base + ".pcap")
    if len(T) < 100:
        return None
    tc, tr = meta["t_cut_cmd"], meta["t_restore_cmd"]
    row = dict(algo=meta["algo"], outage_cmd=meta["outage_cmd_s"], seed=meta["seed"])
    d = np.diff(T)
    cand = np.where((d > GAP_MIN) & (T[:-1] >= tc - 0.2) & (T[:-1] <= tc + 0.6))[0]
    idx = cand[[np.argmax(d[cand])]] if len(cand) else cand
    if len(idx) == 0:
        return dict(row, gap_s=0.0, wait_after_restore_s=0.0, recov_after_gap_s=0.0,
                    recov_from_restore_s=0.0, degraded_s=0.0)
    i = idx[0]
    gs, ge = T[i], T[i + 1]

    lo = max(T[0] + 1.0, gs - 4.0)
    hi = gs - 0.3
    pre = goodput(T, B, lo, hi)
    if len(pre) < 5:
        return None
    thr = 0.9 * pre.mean()

    g = goodput(T, B, ge, T[-1])
    w = int(round(1.0 / DT))
    t_rec = np.nan
    for k in range(0, len(g) - w + 1):
        if (g[k:k + w] >= thr).all():
            t_rec = ge + k * DT
            break
    post = goodput(T, B, tr, min(tr + 10.0, T[-1]))
    post10 = post.mean() / 1e6 if len(post) else np.nan
    return dict(row, gap_s=ge - gs, wait_after_restore_s=ge - tr,
                recov_after_gap_s=t_rec - ge, recov_from_restore_s=t_rec - tr,
                degraded_s=t_rec - gs, pre_mbps=pre.mean() / 1e6, post10_mbps=post10)


rows = []
files = sorted(glob.glob(os.path.join(D, f"*_buf{BUF}_*_loss{LOSS}_*.meta")))
for n, m in enumerate(files, 1):
    r = analyze(m[:-5])
    if r:
        rows.append(r)
    if n % 10 == 0:
        print(f"  parsed {n}/{len(files)}", flush=True)
df = pd.DataFrame(rows)
if df.empty:
    sys.exit("no usable runs found (check data dir / buf / loss args)")
df.to_csv(os.path.join(D, f"summary_buf{BUF}_loss{LOSS}.csv"), index=False)

q25 = lambda s: s.quantile(.25)
q75 = lambda s: s.quantile(.75)
tab = df.groupby(["algo", "outage_cmd"]).agg(
    n=("seed", "count"),
    gap_med=("gap_s", "median"),
    wait_med=("wait_after_restore_s", "median"),
    rec_gap_med=("recov_after_gap_s", "median"),
    rec_rest_med=("recov_from_restore_s", "median"),
    rec_rest_q25=("recov_from_restore_s", q25),
    rec_rest_q75=("recov_from_restore_s", q75),
    pre_mbps=("pre_mbps", "median"),
    post10_mbps=("post10_mbps", "median"),
    unrecovered=("recov_from_restore_s", lambda s: int(s.isna().sum())))
print(tab.round(2).to_string())

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    for algo, sub in tab.reset_index().groupby("algo"):
        ax.errorbar(sub["outage_cmd"], sub["rec_rest_med"],
                    yerr=[(sub["rec_rest_med"] - sub["rec_rest_q25"]).clip(lower=0),
                          (sub["rec_rest_q75"] - sub["rec_rest_med"]).clip(lower=0)],
                    marker="o", capsize=3, label=algo)
    ax.plot([0, 5], [0, 0], "k:", lw=0.8)
    ax.set_xlabel("commanded outage (s)")
    ax.set_ylabel("recovery time after link restored (s), median/IQR")
    ax.set_title(f"buffer {BUF} KB, base loss {LOSS}%")
    ax.legend()
    fig.tight_layout()
    out = os.path.join(D, f"recovery_buf{BUF}_loss{LOSS}.png")
    fig.savefig(out, dpi=150)
    print("saved", out)
except ImportError:
    print("matplotlib not installed; table only")
