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
# races: A every 10 s (results, next race) and the stick pushed left or right for
# 0.7 s every 2.5 s so a kart stuck against a wall gets free (sideways stick does
# not move the cursor of the vertical menus between races)
import random
rnd = random.Random(7)
next_a, k = t, t
while k < secs:
    if k >= next_a:
        press(k, "A")
        next_a += 10
    else:
        b = rnd.choice(["SLEFT", "SRIGHT"])
        ev.extend(f"{k + j / 10:.1f}:{b}" for j in range(7))
    k += 2.5
print(",".join(ev))
