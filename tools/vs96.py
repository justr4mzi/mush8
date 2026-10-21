# NX_AUTOINPUT for the "all tracks" run, from the button sequence played on an
# iPhone (VS race, 150cc, courses in order, 96 races, auto accelerate and smart
# steering on). Waits are stretched (the CI Mac runs the game about half as fast
# as the phone); after the menus, A every 10 s (results screen, next race).
# Usage: python3 tools/vs96.py <seconds>
import sys

secs = int(sys.argv[1])
SLOW, FAST = 20.0, 3.0
seq = [(25, "A"), (SLOW, "A"), (FAST, "DOWN"), (FAST, "DOWN"), (SLOW, "A"), (SLOW, "A"), (FAST, "A"), (FAST, "A"),
       (SLOW, "RIGHT"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "RIGHT"),
       (FAST, "DOWN"), (FAST, "LEFT"), (FAST, "A"), (SLOW, "A"), (FAST, "A"), (FAST, "A")]
ev, t = [], 0.0
def press(t, b):
    ev.extend(f"{t + k / 10:.1f}:{b}" for k in range(3))
for wait, b in seq:
    t += wait
    press(t, b)
t += SLOW
while t < secs:
    press(t, "A")
    t += 10
print(",".join(ev))
