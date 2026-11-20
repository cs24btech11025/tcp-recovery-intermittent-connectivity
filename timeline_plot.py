#!/usr/bin/env python3
"""Annotated timeline for one run_one.py run (iperf harness; needs its .pcap, .ss and .meta).

Usage: python3 timeline_plot.py data/cubic_buf125_out3.0_loss0.0_seed1 [t_from t_to]
       (times in seconds relative to the commanded cut; default: 2 s before to 3 s after recovery)
Writes <run>_timeline.png. Needs numpy, matplotlib and tcpdump.

Top panel: goodput (100 ms bins, unique bytes) with the commanded outage shaded, predicted
retransmission epochs (dashed; plain RTO doubling from the R in the ss log) and the arrival times
of retransmitted segments (red ticks; a segment counts as a retransmission when its sequence
number was already seen).
Bottom panel: cwnd (packets) and the RTO (s) from the periodic ss samples, with the backoff counter.
"""
import json
import re
import subprocess
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

base = sys.argv[1]
meta = json.load(open(base + ".meta"))
tc, tr = meta["t_cut_cmd"], meta["t_restore_cmd"]

RX = re.compile(r"^(\d+\.\d+) IP \d+\.\d+\.\d+\.\d+\.(\d+) > \d+\.\d+\.\d+\.\d+\.(\d+): "
                r".*?seq (\d+):(\d+).*?length (\d+)")
txt = subprocess.run(["tcpdump", "-nn", "-tt", "-r", base + ".pcap", "tcp and src port 5201"],
                     capture_output=True, text=True).stdout
seen, T, B, retx = set(), [], [], []
for line in txt.splitlines():
    m = RX.match(line)
    if not m or int(m.group(6)) < 100:
        continue
    key = (m.group(3), m.group(4))
    t = float(m.group(1))
    if key in seen:
        retx.append(t)
        T.append(t)
        B.append(0)
    else:
        seen.add(key)
        T.append(t)
        B.append(int(m.group(6)))
T, B, retx = np.array(T), np.array(B), np.array(retx)

# ss samples (data connection rows only)
st, cw, rto, bo = [], [], [], []
t_cur = None
for line in open(base + ".ss"):
    if re.fullmatch(r"\d+\.\d+\s*", line):
        t_cur = float(line)
    elif "notsent" in line and t_cur:
        a, b, c = (re.search(p, line) for p in (r"\bcwnd:(\d+)", r"\brto:(\d+)", r"backoff:(\d+)"))
        if a and b:
            st.append(t_cur)
            cw.append(int(a.group(1)))
            rto.append(int(b.group(1)) / 1000)
            bo.append(int(c.group(1)) if c else 0)
st, cw, rto, bo = map(np.array, (st, cw, rto, bo))

# predicted epochs: t_ack = start of the data silence + one-way delay; R from the first backoff row
d = np.diff(T)
cand = np.where((d > 0.05) & (T[:-1] >= tc - 0.2) & (T[:-1] <= tc + 0.6))[0]
epochs = []
if len(cand) and (bo > 0).any():
    gs = T[cand[np.argmax(d[cand])]]
    j = np.argmax(bo > 0)
    R = rto[j] / 2 ** bo[j]
    epochs = [gs + 0.05 + R * (2 ** k - 1) for k in range(1, 10)]
    print(f"R from ss log: {R:.3f} s; predicted epochs after the cut: "
          + ", ".join(f"{e - tc:.2f}" for e in epochs[:6]))

after = [e for e in epochs if e >= tr]
t_from = tc + (float(sys.argv[2]) if len(sys.argv) > 2 else -2.0)
t_to = tc + (float(sys.argv[3]) if len(sys.argv) > 3 else
             (after[0] - tc + 3.0 if after else tr - tc + 5.0))
edges = np.arange(t_from, t_to, 0.1)
gp = np.histogram(T, bins=edges, weights=B)[0] * 8 / 0.1 / 1e6

fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
x = edges[:-1] - tc
a1.step(x, gp, where="post", lw=1)
a1.axvspan(0, tr - tc, color="grey", alpha=0.25, label="commanded outage")
for k, e in enumerate(epochs):
    if t_from <= e <= t_to:
        a1.axvline(e - tc, ls="--", c="C1", lw=0.8, label="predicted RTO epoch" if k == 0 else None)
rr = retx[(retx >= t_from) & (retx <= t_to)] - tc
a1.plot(rr, np.full(len(rr), -0.3), "|", c="red", ms=10, label="retransmission arrivals")
a1.set_ylabel("goodput (Mbps)")
a1.legend(fontsize=8, loc="upper right")
a1.set_title(base.split("/")[-1])

sel = (st >= t_from) & (st <= t_to)
a2.plot(st[sel] - tc, cw[sel], "C0.-", label="cwnd (packets)")
a2.set_ylabel("cwnd (packets)")
a3 = a2.twinx()
a3.plot(st[sel] - tc, rto[sel], "C3.-", label="RTO (s)")
a3.set_ylabel("RTO (s)")
for ti, b_ in zip(st[sel], bo[sel]):
    if b_ > 0:
        a3.annotate(str(b_), (ti - tc, rto[st == ti][0]), fontsize=6, ha="center", va="bottom")
a2.set_xlabel("seconds since commanded cut")
fig.tight_layout()
out = base + "_timeline.png"
fig.savefig(out, dpi=150)
print("saved", out)
