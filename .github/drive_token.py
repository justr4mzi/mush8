# Print a Google Drive access token for the service account in $GDRIVE_SA_JSON
# (read-only), or the file id of a Drive link with --id <url>.
import json, os, re, sys

if len(sys.argv) == 3 and sys.argv[1] == "--id":
    m = re.search(r"/d/([\w-]{20,})", sys.argv[2]) or re.search(r"[?&]id=([\w-]{20,})", sys.argv[2])
    print(m.group(1) if m else "")
    sys.exit(0)
from google.oauth2 import service_account
import google.auth.transport.requests
info = json.loads(os.environ["GDRIVE_SA_JSON"])
creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
creds.refresh(google.auth.transport.requests.Request())
print(creds.token)
