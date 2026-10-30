import sys
from .api import issue_token, serve

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "token":
        import os
        secret = os.environ.get("ROSTER_SECRET", "")
        if len(secret) < 32:
            raise SystemExit("Set ROSTER_SECRET to at least 32 characters")
        print(issue_token(secret, sys.argv[2], sys.argv[3]))
    else:
        serve()
