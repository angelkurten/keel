"""Operator commands, run in the control plane's container.

    python -m hosted password <account>              asks for a new password
    python -m hosted password <account> --hash <h>   sets one hashed elsewhere (hosted.app.hash_password), so the
                                                     password itself never travels to the server
"""

import getpass
import os
import sys

from hosted.app import hash_password
from keel.store import open_store

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) not in (2, 4) or args[0] != "password" or (len(args) == 4 and args[2] != "--hash"):
        sys.exit(__doc__)
    hashed = args[3] if len(args) == 4 else hash_password(getpass.getpass("New password: "))
    if not hashed.startswith("scrypt$"):
        sys.exit("--hash takes the output of hosted.app.hash_password")
    if open_store(os.environ["HOSTED_CONTROL_DB"])._exec("UPDATE hosted_accounts SET password = ? WHERE account = ?", (hashed, args[1])) != 1:
        sys.exit(f"no account '{args[1]}'")
    print("password set")
