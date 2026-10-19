"""Operator commands. `python -m hosted password <account>`: set an account's password (no reset by email yet)."""

import getpass
import os
import sys

from hosted.app import hash_password
from keel.store import open_store

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "password":
        sys.exit("usage: python -m hosted password <account>   (HOSTED_CONTROL_DB names the control database)")
    pw = getpass.getpass("New password: ")
    if open_store(os.environ["HOSTED_CONTROL_DB"])._exec("UPDATE hosted_accounts SET password = ? WHERE account = ?", (hash_password(pw), sys.argv[2])) != 1:
        sys.exit(f"no account '{sys.argv[2]}'")
    print("password set")
