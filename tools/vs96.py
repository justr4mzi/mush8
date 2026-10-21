# NX_AUTOINPUT for the "all tracks" run, from the button sequence played on an
# iPhone (VS race, 150cc, courses in order, 96 races) plus smart steering and
# auto accelerate turned on in the vehicle screen. The CI Mac runs the game about
# half as fast as the phone and a press can be missed during a menu animation,
# so waits are long and presses last 0.6 s. After the menus: A every 10 s (results, next race).
# Usage: python3 tools/vs96.py <seconds>
import sys

secs = int(sys.argv[1])
SLOW, FAST = 30.0, 4.0
# (menus wrap around, so no "push to the top": the phone's sequence as is)
seq = ([(45, "A"),                       # title (accepted later on the Mac)
        (SLOW, "A"),                     # Single Player
        (FAST, "DOWN"), (FAST, "DOWN"), (SLOW, "A"),   # VS race
        (SLOW, "A"),                     # 150cc
        (FAST, "A"),                     # character
        # vehicle screen (needs ~20 s to be ready on the Mac): + opens the panel,
        # L = smart steering, R = auto accelerate (seen toggling on the probe), + closes
        (20.0, "PLUS"), (8.0, "L"), (8.0, "R"), (8.0, "PLUS"), (8.0, "A"),
        (SLOW, "RIGHT"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"),
        (FAST, "RIGHT"), (FAST, "DOWN"), (FAST, "LEFT"), (FAST, "A"),           # rules
        (SLOW, "A"), (FAST, "A"), (FAST, "A")])
ev, t = [], 0.0
def press(t, b):
    ev.extend(f"{t + k / 10:.1f}:{b}" for k in range(6))
for wait, b in seq:
    t += wait
    press(t, b)
t += SLOW
# races, every 10 s: A (results, next race), then B held 1.5 s with the stick to
# one side (reverse out of a wall), then the stick to the other side for 1 s
import random
rnd = random.Random(7)
k = t
while k < secs:
    press(k, "A")
    side = rnd.choice(["SLEFT", "SRIGHT"])
    other = "SRIGHT" if side == "SLEFT" else "SLEFT"
    if k >= t + 60:   # not before the race has started (B goes back in the menus)
        ev.extend(f"{k + 3 + j / 10:.1f}:B" for j in range(15))
    ev.extend(f"{k + 3 + j / 10:.1f}:{side}" for j in range(15))
    ev.extend(f"{k + 6 + j / 10:.1f}:{other}" for j in range(10))
    k += 10
print(",".join(ev))
