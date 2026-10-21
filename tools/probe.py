# Probe script: the vs96 menus up to the vehicle screen, then one action every
# 20 s so each step is on its own screenshot. Usage: python3 tools/probe.py <steps>
# steps: comma separated buttons after the character is chosen, e.g. PLUS,L,R,PLUS,A
import sys
SLOW, FAST = 30.0, 4.0
seq = [(45, "A"), (SLOW, "A"), (FAST, "DOWN"), (FAST, "DOWN"), (SLOW, "A"), (SLOW, "A"), (FAST, "A")]
seq += [(20.0, b) for b in sys.argv[1].split(",") if b]
ev, t = [], 0.0
for wait, b in seq:
    t += wait
    ev.extend(f"{t + k / 10:.1f}:{b}" for k in range(6))
print(",".join(ev))
