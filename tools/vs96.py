# NX_AUTOINPUT for the "all tracks" run, from the button sequence played on an
# iPhone (VS race, 150cc, courses in order, 96 races) plus smart steering and
# auto accelerate turned on in the vehicle screen. The CI Mac runs the game about
# half as fast as the phone and a press can be missed during a menu animation,
# so waits are long and presses last 0.6 s. After the menus: A every 10 s (results, next race).
# Usage: python3 tools/vs96.py <seconds> [variant]
# variant 0, 1, 2...: each one waits longer between the rules presses and holds
# the directions a bit longer (the Mac workflow tries the next variant when the
# tracks don't come in order, without leaving the Mac).
import sys

secs = int(sys.argv[1])
variant = int(sys.argv[2]) if len(sys.argv) > 2 else 0
# third argument: RIGHT presses on the cup screen before choosing, to start further in the list
# (a second session starts where the first one stopped; "in order" goes on from the chosen cup)
cups_right = int(sys.argv[3]) if len(sys.argv) > 3 else 0
SLOW, FAST = 30.0 * (1 + 0.5 * variant), 4.0 + 3.0 * variant
# (menus wrap around, so no "push to the top": the phone's sequence as is)
seq = ([(45, "A"),                       # title (accepted later on the Mac)
        (SLOW, "A"),                     # Single Player
        (15.0, "DOWN"), (6.0, "DOWN"), (SLOW, "A"),   # VS race (the mode menu needs time to appear)
        (SLOW, "A"),                     # character (cc is chosen later, in the rules)
        (10.0, "A"),                     # vehicle: body -> tires
        # vehicle screen (needs ~20 s to be ready on the Mac): + opens the panel,
        # L = smart steering, R = auto accelerate (seen toggling on the probe), + closes
        (20.0, "PLUS"), (8.0, "L"), (8.0, "R"), (8.0, "PLUS"),
        (8.0, "A"), (8.0, "A"),          # tires -> glider -> OK (seen on the Mac: 3 A on this screen)
        (SLOW, "RIGHT"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"), (FAST, "DOWN"),
        (FAST, "RIGHT"), (FAST, "DOWN"), (FAST, "LEFT"), (FAST, "A"),           # rules
        *([(SLOW, "RIGHT")] + [(FAST, "RIGHT")] * (cups_right - 1) + [(FAST, "A")] if cups_right else [(SLOW, "A")]),
        (FAST, "A"), (FAST, "A")])
ev, t = [], 0.0
def press(t, b):
    # A/B/L/R/PLUS: held 0.6 s so a press during a menu animation isn't lost.
    # Directions: a short 0.2 s tap, a longer hold auto-repeats in the menus
    # (seen on the Mac: 5 DOWN taps moved the rules cursor 3 to 6 rows).
    n = (2 + variant % 3) if b in ("UP", "DOWN", "LEFT", "RIGHT") else 6
    ev.extend(f"{t + k / 10:.1f}:{b}" for k in range(n))
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
