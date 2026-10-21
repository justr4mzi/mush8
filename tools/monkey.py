# NX_AUTOINPUT script for unattended play: mostly A held ~0.8 s (menus go on,
# the kart accelerates), sometimes a direction (other characters, karts, cups)
# and rarely B. Usage: python3 tools/monkey.py <seed> <seconds>
import random, sys

seed, secs = int(sys.argv[1]), int(sys.argv[2])
r = random.Random(seed)
ev, t = [], 12.0
while t < secs:
    x = r.random()
    b, dur = (("A", 0.8) if x < 0.72 else ("RIGHT", 0.15) if x < 0.80 else ("DOWN", 0.15) if x < 0.86
              else ("LEFT", 0.15) if x < 0.91 else ("UP", 0.15) if x < 0.95 else ("B", 0.15) if x < 0.97 else ("A", 0.15))
    k = 0.0
    while k < dur:
        ev.append(f"{t + k:.1f}:{b}")
        k += 0.1
    t += 1.0
print(",".join(ev))
