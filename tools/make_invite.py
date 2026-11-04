"""Administrator tool: make an invite code for a server connection. The person pastes it in NextPull (Settings -> Other servers -> Add a server) and only has to
type the password. The code holds the address, optionally the user name, the access key and a name. It never holds the password.

    py -3 tools\\make_invite.py pull.example.com ben X-Pull-Key <the key> "Home server"
    py -3 tools\\make_invite.py pull.example.com                 # address only
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connections import make_invite  # noqa: E402

args = sys.argv[1:]
if not args or args[0] in ("-h", "--help") or len(args) in (3,):
    sys.exit(__doc__)
server = args[0]
user = args[1] if len(args) > 1 else ""
name, value = (args[2], args[3]) if len(args) > 3 else ("", "")
label = args[4] if len(args) > 4 else ""
print(make_invite(server, user, name, value, label))
