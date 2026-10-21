# Keys on the cup screen to start a VS "in order" session at the cup after the
# tracks already done (argument: number of different tracks already played).
# The order the game plays them: Mushroom, Flower, Star, Special (row 1, columns 1-4),
# Shell, Banana, Leaf, Lightning (row 2, columns 1-4), Egg, Crossing (row 1, 5-6),
# Triforce, Bell (row 2, 5-6), then the Booster Course Pass cups (next page).
import sys
done = int(sys.argv[1]) if len(sys.argv) > 1 else 0
cup = done // 4
grid = [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0), (1, 1), (1, 2), (1, 3), (0, 4), (0, 5), (1, 4), (1, 5)]
if cup < len(grid):
    row, col = grid[cup]
else:   # Booster Course Pass: next page(s), 2 rows of 6, assumed reached by going right
    k = cup - 12
    row, col = (k // 6) % 2, 6 + k % 6 + 6 * (k // 12)
keys = ["DOWN"] * row + ["RIGHT"] * col
print("+".join(keys))
