# Copy stdin to stdout with the download links (secrets) hidden.
import os, sys
secrets = [os.environ.get(k, "") for k in ("KEYS_URL", "BASE_URL", "UPDATE_URL", "FIRMWARE_URL", "SAVE_URL", "DLC_URL")]
secrets = [s for s in secrets if len(s) > 8]
for line in sys.stdin:
    for s in secrets:
        line = line.replace(s, "***")
    sys.stdout.write(line)
    sys.stdout.flush()
