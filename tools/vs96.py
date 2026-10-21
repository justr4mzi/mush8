# NX_AUTOINPUT for the "all tracks" run, from the button sequence played on an
# iPhone (VS race, 150cc, courses in order, 96 races) plus smart steering and
# auto accelerate turned on in the vehicle screen. The CI Mac runs the game about
# half as fast as the phone and a press can be missed during a menu animation,
# so waits are long, presses last 0.6 s, and the cursor is first pushed to the
# top of the main menu and of the mode menu (UP x4) so a missed press does not
# shift everything. After the menus: A every 10 s (results, next race).
# Usage: python3 tools/vs96.py <seconds>
import sys

secs = int(sys.argv[1])
SLOW, FAST = 30.0, 4.0
top = [(FAST, "UP")] * 4
seq = ([(25, "A")]                       # title
       + [(SLOW, "UP")] + top[1:] + [(FAST, "A")]   # main menu: Single Player
       + [(SLOW, "UP")] + top[1:] + [(FAST, "DOWN"), (FAST, "DOWN"), (FAST, "A")]  # VS race
       + [(SLOW, "A"),                    # 150cc
          (SLOW, "A"),                    # character
          (SLOW, "PLUS"), (FAST, "L"), (FAST, "R"), (FAST, "PLUS"), (FAST, "A"),  # vehicle + assists
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
while t < secs:
    press(t, "A")
    t += 10
print(",".join(ev))
