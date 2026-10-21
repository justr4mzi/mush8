# Probe scripts, one action every 20 s so each step is on its own screenshot.
#   python3 tools/probe.py PLUS,L,R      -> menus up to the vehicle screen, then the steps
#   python3 tools/probe.py race:PLUS,DOWN,A -> the vs96 menus, 90 s of race, then the steps
import subprocess, sys
arg = sys.argv[1]
if arg.startswith("race:"):
    ev = subprocess.run([sys.executable, __file__.replace("probe.py", "vs96.py"), "340"], capture_output=True, text=True).stdout.strip().split(",")
    ev = [e for e in ev if float(e.split(":")[0]) < 350]
    t = 350 + 90.0
    for b in [x for x in arg[5:].split(",") if x]:
        ev += [f"{t + k / 10:.1f}:{b}" for k in range(6)]
        t += 20
    print(",".join(ev))
    sys.exit(0)
SLOW, FAST = 30.0, 4.0
seq = [(45, "A"), (SLOW, "A"), (FAST, "DOWN"), (FAST, "DOWN"), (SLOW, "A"), (SLOW, "A"), (FAST, "A")]
seq += [(20.0, b) for b in arg.split(",") if b]
ev, t = [], 0.0
for wait, b in seq:
    t += wait
    ev.extend(f"{t + k / 10:.1f}:{b}" for k in range(6))
print(",".join(ev))
