"""The control plane: sign-in by email and password, an account per invitation, the console's API, and the console itself.

    uvicorn hosted.app:from_env --factory   (configured by environment, see hosted/README.md)

Signing up with an invitation creates the account: its own database, its limits, its container on
Coolify, and the fleet example's events in a tenant `demo`, so the console has a graph to show at once.
The console never holds a customer's key: the control plane calls the account's container with that
container's own secret. The control plane never imports a customer's handlers.py: loading it would run their
code here, so a cartridge is checked as YAML and runs only in its container.
"""

# no `from __future__ import annotations`: FastAPI resolves the request models defined in build_app from real
# annotations (as in keel/http.py).
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from hosted import agent as agents
from hosted.provision import Databases, Provisioner
from keel.accounts import Quotas, set_account
from keel.cartridge import revision
from keel.envelope import parse_iso, utcnow
from keel.keys import check_account, check_tenant, create_key, revoke_key
from keel.store import open_store
from keel.usage import report, today

log = logging.getLogger("keel.hosted")
EXAMPLES = ("fleet", "crm", "clinics", "finance")
TOOLS = ("context", "history", "search", "annotate", "related")
TABLES = (
    """CREATE TABLE IF NOT EXISTS hosted_accounts (
  account VARCHAR(64) NOT NULL PRIMARY KEY, email VARCHAR(191) NOT NULL, app VARCHAR(64), endpoint VARCHAR(191),
  db TEXT NOT NULL, created_at VARCHAR(40) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS hosted_sessions (
  token VARCHAR(64) NOT NULL PRIMARY KEY, email VARCHAR(191) NOT NULL, account VARCHAR(64) NOT NULL, expires_at VARCHAR(40) NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS hosted_agents (
  account VARCHAR(64) NOT NULL, name VARCHAR(100) NOT NULL, config TEXT NOT NULL, updated_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (account, name))""",
)
# columns added after the first accounts existed (2026-10-18); ADD COLUMN fails harmlessly once they are there
COLUMNS = ("password VARCHAR(200)", "status VARCHAR(16)", "error TEXT", "secret VARCHAR(100)", "cartridge TEXT", "example VARCHAR(32)",
           "handlers TEXT", "cdc_map TEXT", "notice TEXT", "semantic VARCHAR(8)")
PUBLIC_MAIL = {"gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "icloud", "me", "proton", "protonmail", "aol", "gmx", "yandex"}
STEPS = (("database", "Database and container created"), ("container", "Keel starting"), ("example", "Example data loaded"), ("ready", "Ready"))
STATUS_STEP = {"creating": 0, "starting": 1, "loading": 2, "ready": 4}
UPDATE_STEPS = (("cartridge", "New cartridge sent"), ("container", "Keel restarting with it"), ("ready", "Ready"))  # status "updating"
GENERATOR = """You write Keel cartridges. Keel turns a product's domain events into a bitemporal context graph per tenant
that AI agents read through five tools. A cartridge is two files:

- cartridge.yaml: `name`, `entities` (kind -> description), `relations` (name -> from/to kinds), `events` (dotted
  lowercase type -> subject kind, owner, payload with short type hints: string, int, number, bool, date, datetime,
  any, `string | null` for optional, `[low, normal, high]` for an enum, `string[]` for a list, `{{key: hint}}` nested),
  `rules` (named conditions over current facts that tools evaluate), and `memory`.
- handlers.py: `registry = HandlerRegistry()` and one small function per event type, decorated with
  `@registry.on("<type>")`, mapping the payload to store primitives only: upsert_entity, link, unlink, open_fact,
  close_fact. Every call takes `event.tenant` first. Facts are opened with `valid_from=event.occurred_at`,
  `source=event.source` and `evidence=event.id`; they are closed, never deleted.

Rules: pointers, not copies (an entity keeps `ref` and the few attributes a tool needs to label it); keep handlers
about ten lines; business logic goes in `rules`, never in handlers; no network, no randomness, no imports beyond
keel's. Declare every event type you are given, with a handler for each. Subjects are `<kind>:<id>`.

A complete example (a fleet product):

```yaml
{example_yaml}
```

```python
{example_handlers}
```

Answer with exactly two fenced blocks: first ```yaml with cartridge.yaml, then ```python with handlers.py. No other
text."""


def _files_in(text: str) -> dict[str, str]:
    """The cartridge.yaml and handlers.py blocks of a model's answer."""
    blocks = re.findall(r"```(yaml|yml|python|py)\s*\n(.*?)```", text, re.DOTALL)
    out = {}
    for lang, body in blocks:
        name = "cartridge.yaml" if lang.startswith("y") else "handlers.py"
        out.setdefault(name, body.strip() + "\n")
    return out


@dataclass
class Settings:
    invite_codes: set[str] = field(default_factory=set)  # empty: no new accounts
    rps: int = 10
    events_month: int = 100_000
    examples_dir: str = "examples"
    secure_cookies: bool = False  # true behind https
    static_dir: str | None = None  # the console's build (console/dist)
    agent_messages_day: int = 50  # playground messages per account and day on the control plane's model key
    models: tuple[str, ...] = ("anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-5.5", "openai/gpt-6-luna", "google/gemini-3.5-flash")
    ofox_key: str | None = None
    session_days: int = 30
    ready_timeout: float = 300  # seconds for a new container to answer /health (restarted once halfway)
    generations_day: int = 10  # cartridges generated per account and day
    public_url: str = "https://keel.angelkurten.com"
    embed_url: str | None = None  # how account containers reach the embeddings proxy; set it to the control plane's internal address to skip the CDN
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_tokens_day: int = 2_000_000  # per account: an account's own code can call the proxy too


def check_cartridge(files: dict[str, str]) -> str:
    """The cartridge's name, after checking its YAML shape. handlers.py is never imported here."""
    if set(files) != {"cartridge.yaml", "handlers.py"}:
        raise ValueError("a cartridge is cartridge.yaml and handlers.py")
    try:
        doc = yaml.safe_load(files["cartridge.yaml"])
    except yaml.YAMLError as exc:
        raise ValueError(f"cartridge.yaml is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not doc.get("name") or not doc.get("entities") or not doc.get("events"):
        raise ValueError("cartridge.yaml needs a name, entities and events")
    if len(files["cartridge.yaml"]) + len(files["handlers.py"]) > 512_000:
        raise ValueError("the cartridge is larger than 512 KB")
    return str(doc["name"])


def static_check(files: dict[str, str], events: list[dict[str, Any]] | None = None) -> list[str]:
    """What `keel validate` would say, without importing handlers.py: the handled event types are read from its
    `@registry.on("...")` decorators with `ast`, which runs nothing. With `events`, each is also checked against the
    catalog. A cartridge whose handlers register types some other way gets "no handler" problems it may ignore."""
    import ast
    import tempfile

    from keel.cartridge import load_cartridge
    from keel.envelope import Event

    try:
        tree = ast.parse(files.get("handlers.py", ""))
    except SyntaxError as exc:
        return [f"handlers.py line {exc.lineno}: {exc.msg}"]
    handled = {d.args[0].value for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for d in node.decorator_list
               if isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "on" and d.args and isinstance(d.args[0], ast.Constant)}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "cartridge.yaml").write_text(files["cartridge.yaml"], encoding="utf-8")
        try:
            c = load_cartridge(tmp, handlers=False)
        except Exception as exc:  # noqa: BLE001 - any shape error in the YAML is the customer's to fix
            return [f"cartridge.yaml: {exc}"]
    for etype in handled:
        c.registry.on(str(etype))(lambda _event, _store: None)
    problems = c.validate()
    for i, raw in enumerate(events or []):
        try:
            problems += [f"event {i + 1}: {p}" for p in c.check_event(Event.model_validate({"tenant": "check", **raw}))]
        except ValueError as exc:
            problems.append(f"event {i + 1}: not a valid envelope ({str(exc).splitlines()[0]})")
    return problems


def revisions(files: dict[str, str]) -> set[str]:
    """The revision a container will report for these files: Coolify drops a file mount's trailing newline, so both forms."""
    return {revision(files["cartridge.yaml"], files["handlers.py"]),
            revision(files["cartridge.yaml"].rstrip("\n"), files["handlers.py"].rstrip("\n"))}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("a password of at least 10 characters")
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored or stored.count("$") != 2:
        return False
    _, salt, digest = stored.split("$")
    return secrets.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), digest)


def _email(email: str) -> str:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 191:
        raise ValueError("an email address, please")
    return email


class ControlPlane:
    def __init__(self, control: Any, databases: Databases, provisioner: Provisioner, settings: Settings, http: Any = None) -> None:
        import httpx

        self.control, self.databases, self.provisioner, self.settings = control, databases, provisioner, settings
        self.http = http or httpx.Client(timeout=30)
        for sql in TABLES:
            control._exec(sql)
        for col in COLUMNS:
            with contextlib.suppress(Exception):  # already there
                control._exec(f"ALTER TABLE hosted_accounts ADD COLUMN {col}")

    def heal(self) -> None:
        """Complete accounts created before the console (no secret kept): their container's secret and cartridge are
        read back from the provisioner. Best effort, at start-up."""
        for row in self.control._rows("SELECT account, app FROM hosted_accounts WHERE app IS NOT NULL AND (secret IS NULL OR secret = '')"):
            try:
                files, env = self.provisioner.current(row["app"])
                if env.get("KEEL_SECRET"):
                    self.control._exec("UPDATE hosted_accounts SET secret = ?, cartridge = ?, handlers = ?, status = ? WHERE account = ?",
                                       (env["KEEL_SECRET"], files.get("cartridge.yaml", ""), files.get("handlers.py", ""), "ready", row["account"]))
                    log.warning("completed account %s from its container", row["account"])
            except Exception:
                log.exception("could not complete account %s", row["account"])

    # ---- accounts --------------------------------------------------------------------------

    def _row(self, account: str) -> Any:
        return self.control._one("SELECT * FROM hosted_accounts WHERE account = ?", (account,))

    def available(self, account: str) -> str | None:
        """Why the slug cannot be used, or None."""
        try:
            check_account(account)
        except ValueError as exc:
            return str(exc)
        if self._row(account) or self.control.account(account):
            return f"'{account}' is taken"
        return None

    def propose(self, email: str) -> str:
        """An account slug from the email's domain (`angel@acme.com` -> `acme`), or its name on a public mail domain."""
        local, _, domain = _email(email).partition("@")
        first = domain.split(".")[0]
        base = re.sub(r"[^a-z0-9]+", "-", local if first in PUBLIC_MAIL else first).strip("-")[:28] or "keel"
        if not base[0].isalnum():
            base = "k" + base
        slug, n = base, 2
        while self.available(slug) is not None:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def signup(self, email: str, account: str, password: str, invite: str, example: str = "fleet") -> str:
        """Create the account, start its provisioning in the background, and return a session."""
        if not invite or invite not in self.settings.invite_codes:
            raise PermissionError("Keel is invite-only during the pilots: write to angel@angelkurten.com")
        email, account = _email(email), account.strip()
        if (why := self.available(account)) is not None:
            raise ValueError(why)
        if self.control._one("SELECT account FROM hosted_accounts WHERE email = ?", (email,)):
            raise ValueError("this email already has an account: sign in")
        pw = hash_password(password)
        base = Path(self.settings.examples_dir) / example
        files = {n: (base / n).read_text(encoding="utf-8") for n in ("cartridge.yaml", "handlers.py")}
        check_cartridge(files)
        self.control._exec("INSERT INTO hosted_accounts (account, email, password, db, created_at, status, cartridge, handlers, example) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (account, email, pw, "", utcnow().isoformat(), "creating", files["cartridge.yaml"], files["handlers.py"], example))
        threading.Thread(target=self._provision, args=(account, files, example), daemon=True).start()
        return self._session(email, account)

    def _status(self, account: str, status: str, error: str | None = None) -> None:
        self.control._exec("UPDATE hosted_accounts SET status = ?, error = ? WHERE account = ?", (status, error, account))

    def _provision(self, account: str, files: dict[str, str], example: str) -> None:
        try:
            db = self.databases.create(account)
            try:
                set_account(self.control, account, rps=self.settings.rps, events_month=self.settings.events_month)
                set_account(open_store(db), account, rps=self.settings.rps, events_month=self.settings.events_month)
                secret = secrets.token_urlsafe(32)
                app_id, endpoint = self.provisioner.create(account, files, {"KEEL_DB": db, "KEEL_ACCOUNT": account, "KEEL_SECRET": secret})
            except Exception:
                self.databases.drop(account)
                raise
            self.control._exec("UPDATE hosted_accounts SET app = ?, endpoint = ?, db = ?, secret = ? WHERE account = ?",
                               (app_id, endpoint, db, secret, account))
            self._status(account, "starting")
            self._wait_ready(app_id, endpoint)
            self._status(account, "loading")
            self._load_example(account, endpoint, secret, example)
            self._status(account, "ready")
        except Exception as exc:  # shown to the customer in the console
            log.exception("provisioning %s failed", account)
            self._status(account, "failed", str(exc)[:500])

    def _wait_ready(self, app_id: str, endpoint: str, rev: set[str] | None = None) -> None:
        """Poll /health; a container that has not answered halfway through is restarted once (a first start
        can end unhealthy, and Coolify keeps no logs of a stopped container)."""
        start, restarted = time.monotonic(), False
        while (spent := time.monotonic() - start) < self.settings.ready_timeout:
            with contextlib.suppress(Exception):  # not up yet
                r = self.http.get(f"{endpoint}/health", timeout=5)
                if r.status_code == 200 and (rev is None or r.json().get("revision") in rev):  # the old container may still answer
                    return
            if not restarted and spent > self.settings.ready_timeout / 2 and hasattr(self.provisioner, "restart"):
                self.provisioner.restart(app_id)
                restarted = True
            time.sleep(min(3, self.settings.ready_timeout / 20))
        raise RuntimeError("the Keel container did not start in time")

    def _load_example(self, account: str, endpoint: str, secret: str, example: str) -> None:
        lines = (Path(self.settings.examples_dir) / example / "events.jsonl").read_text(encoding="utf-8").splitlines()
        batch = [{**json.loads(line), "tenant": f"{account}/demo"} for line in lines if line.strip() and not line.lstrip().startswith("#")]
        for i in range(0, len(batch), 500):
            r = self.http.post(f"{endpoint}/v1/events", json=batch[i : i + 500], headers={"Authorization": f"Bearer {secret}"}, timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"loading the example answered {r.status_code}: {r.text[:200]}")

    def me(self, email: str, account: str) -> dict[str, Any]:
        row = self._row(account)
        status = (row["status"] if row else None) or "ready"  # accounts from before the console were ready
        if status == "updating":
            return {"email": email, "account": account, "endpoint": row["endpoint"], "status": status, "error": None, "notice": None,
                    "steps": [{"id": sid, "label": label, "state": "done" if i == 0 else "active" if i == 1 else "pending"}
                              for i, (sid, label) in enumerate(UPDATE_STEPS)]}
        at = STATUS_STEP.get(status, 0)
        failed_at = at if status == "failed" else None
        if status == "failed":
            at = 0 if not row["endpoint"] else 1  # roughly where it stopped
            failed_at = at
        steps = [{"id": sid, "label": label, "state": "failed" if i == failed_at else "done" if i < at or status == "ready" else "active" if i == at else "pending"}
                 for i, (sid, label) in enumerate(STEPS)]
        return {"email": email, "account": account, "endpoint": row["endpoint"] if row else None, "status": status,
                "error": row["error"] if row else None, "notice": row["notice"] if row else None, "steps": steps}

    def cartridge(self, account: str) -> dict[str, Any]:
        row = self._row(account)
        doc = yaml.safe_load(row["cartridge"]) if row and row["cartridge"] else {}
        return {**(doc if isinstance(doc, dict) else {}), "example": bool(row and row["example"]),
                "files": {"cartridge.yaml": row["cartridge"] or "", "handlers.py": row["handlers"] or ""} if row else {}}

    # ---- the account's cartridge -----------------------------------------------------------------

    def replace_cartridge(self, account: str, files: dict[str, str] | None = None, example: str | None = None) -> list[str]:
        """Deploy another cartridge to the account's container, in the background. Its problems (static_check) are
        returned; a cartridge that does not even parse is refused. If the new container does not start, the previous
        cartridge goes back and `notice` says why. Leaving the example forgets its tenant `demo`. An account whose last
        change failed may deploy again: that is the way out."""
        row = self._row(account)
        if row is None or not row["endpoint"] or not row["secret"] or (row["status"] or "ready") not in ("ready", "failed"):
            raise LookupError("your Keel is not ready yet")
        if example:
            if example not in EXAMPLES:
                raise ValueError(f"unknown example '{example}'")
            base = Path(self.settings.examples_dir) / example
            files = {n: (base / n).read_text(encoding="utf-8") for n in ("cartridge.yaml", "handlers.py")}
        if not files:
            raise ValueError("send cartridge.yaml and handlers.py, or pick an example")
        check_cartridge(files)
        problems = static_check(files)
        if any(p.startswith(("handlers.py line", "cartridge.yaml:")) for p in problems):
            raise ValueError("; ".join(problems))
        old = {"cartridge.yaml": row["cartridge"] or "", "handlers.py": row["handlers"] or ""}
        self._status(account, "updating")
        self.control._exec("UPDATE hosted_accounts SET notice = NULL WHERE account = ?", (account,))
        threading.Thread(target=self._redeploy, args=(account, files, example, old, row["example"]), daemon=True).start()
        return problems

    def _redeploy(self, account: str, files: dict[str, str], example: str | None, old: dict[str, str], old_example: str | None) -> None:
        row = self._row(account)
        try:
            self.provisioner.update(row["app"], files=files)
            self._wait_ready(row["app"], row["endpoint"], revisions(files))
        except Exception as exc:  # put the previous cartridge back
            log.exception("new cartridge for %s did not start", account)
            try:
                if old["cartridge.yaml"]:
                    self.provisioner.update(row["app"], files=old)
                    self._wait_ready(row["app"], row["endpoint"], revisions(old))
                self.control._exec("UPDATE hosted_accounts SET notice = ? WHERE account = ?",
                                   (f"Your new cartridge did not start ({exc}); the previous one is back.", account))
                self._status(account, "ready")
            except Exception as again:  # noqa: BLE001
                self._status(account, "failed", f"the new cartridge did not start and the previous one did not come back: {again}"[:500])
            return
        self.control._exec("UPDATE hosted_accounts SET cartridge = ?, handlers = ?, example = ? WHERE account = ?",
                           (files["cartridge.yaml"], files["handlers.py"], example, account))
        try:
            if old_example and example != old_example:
                open_store(row["db"]).forget(f"{account}/demo")  # the example's data has no meaning under another cartridge
            if example and example != old_example:
                self._status(account, "loading")
                self._load_example(account, row["endpoint"], row["secret"], example)
            self._status(account, "ready")
        except Exception as exc:  # noqa: BLE001
            self._status(account, "ready")
            self.control._exec("UPDATE hosted_accounts SET notice = ? WHERE account = ?", (f"The cartridge is deployed, but: {exc}", account))

    def generate(self, account: str, events: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        """A cartridge proposed by a model from a few real events (and a line about the product), checked with
        static_check against those events; one more attempt with the problems when there are any. Never deployed here."""
        self._ready(account)
        if not events or len(events) > 20:
            raise ValueError("paste between 1 and 20 example events")
        if not self.settings.ofox_key:
            raise LookupError("no model is configured here")
        day = today()
        used = sum(n for t, d, item, n in self.control.usage(day, day + "~") if t == f"{account}/__console__" and item == "cartridge_generations")
        if used >= self.settings.generations_day:
            raise OverflowError(f"today's {self.settings.generations_day} generations are used: come back tomorrow")
        self.control.meter([(f"{account}/__console__", day, "cartridge_generations", 1)])
        chat = agents.chat_api(self.http, "ofox", self.settings.ofox_key, self.settings.models[0])
        base = Path(self.settings.examples_dir) / "fleet"
        prompt = GENERATOR.format(example_yaml=(base / "cartridge.yaml").read_text(encoding="utf-8"),
                                  example_handlers=(base / "handlers.py").read_text(encoding="utf-8"))
        ask = f"The product: {description or '(not described)'}\n\nIts events:\n{json.dumps(events, indent=2)[:30000]}"
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": ask}]
        files, problems = {}, ["no answer"]
        for attempt in (1, 2):
            text = chat(messages, [])["content"]
            files = _files_in(text)
            problems = ["the answer did not hold both files"] if set(files) != {"cartridge.yaml", "handlers.py"} else static_check(files, events)
            if not problems or attempt == 2:
                break
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Fix these problems and answer with both files again:\n- " + "\n- ".join(problems)}]
        return {"cartridge_yaml": files.get("cartridge.yaml", ""), "handlers_py": files.get("handlers.py", ""), "problems": problems,
                "attempts": attempt, "allowance": {"used": used + 1, "limit": self.settings.generations_day}}

    # ---- semantic search ---------------------------------------------------------------------------

    def enable_semantic(self, account: str) -> dict[str, Any]:
        """search(mode="semantic") for the account: a volume for the index, the container's embedder pointed at this
        control plane's proxy (the container's own secret is its key: the gateway's key never enters a container that runs
        customer code), and the index brought up to date every 10 minutes inside the server (keel serve --index-every)."""
        row = self._ready(account)
        self.provisioner.volume(row["app"], f"keel-{account}-data", "/data")
        self.provisioner.update(row["app"], env={
            "KEEL_INDEX": "chroma:///data/vectors", "KEEL_INDEX_CURSOR_DIR": "/data", "KEEL_INDEX_EVERY": "600", "KEEL_EMBEDDER": "openai",
            "KEEL_EMBEDDINGS_BASE_URL": self.settings.embed_url or f"{self.settings.public_url.rstrip('/')}/embed/v1", "KEEL_EMBEDDINGS_API_KEY": row["secret"],
            "KEEL_EMBEDDINGS_MODEL": self.settings.embedding_model})
        self.control._exec("UPDATE hosted_accounts SET semantic = ? WHERE account = ?", ("on", account))
        return {"semantic": True}

    def embed(self, secret: str, body: dict[str, Any]) -> dict[str, Any]:
        """The embeddings proxy: an account's container, known by its secret, gets the gateway's embeddings under a
        daily token cap; the model is fixed."""
        row = self.control._one("SELECT account FROM hosted_accounts WHERE secret = ?", (secret,)) if secret else None
        if row is None:
            raise PermissionError("unknown container secret")
        account, day = row["account"], today()
        used = sum(n for t, d, item, n in self.control.usage(day, day + "~") if t == f"{account}/__console__" and item == "embedding_tokens")
        if used >= self.settings.embedding_tokens_day:
            raise OverflowError(f"account '{account}' used today's embeddings")
        if not self.settings.ofox_key:
            raise LookupError("no embeddings gateway configured")
        r = self.http.post("https://api.ofox.ai/v1/embeddings", json={"model": self.settings.embedding_model, "input": body.get("input", [])},
                           headers={"Authorization": f"Bearer {self.settings.ofox_key}"}, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"the gateway answered {r.status_code}")
        out = r.json()
        self.control.meter([(f"{account}/__console__", day, "embedding_tokens", int((out.get("usage") or {}).get("total_tokens") or 0))])
        return out

    def cdc(self, account: str) -> dict[str, Any]:
        row = self._ready(account)
        status: Any = None
        with contextlib.suppress(Exception):
            status = self.http.get(f"{row['endpoint']}/health", timeout=5).json().get("cdc")
        tables = sorted((yaml.safe_load(row["cdc_map"]) or {}).keys()) if row["cdc_map"] else []
        return {"configured": bool(row["cdc_map"]), "map": row["cdc_map"] or "", "tables": tables, "status": status}

    def set_cdc(self, account: str, dsn: str, map_yaml: str) -> dict[str, Any]:
        """Make the account's container follow Postgres (keel serve --cdc). The DSN goes to the container's environment
        only; the control plane keeps the map."""
        row = self._ready(account)
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("a Postgres URL: postgresql://user:pass@host:5432/db")
        try:
            tables = yaml.safe_load(map_yaml)
        except yaml.YAMLError as exc:
            raise ValueError(f"the map is not valid YAML: {exc}") from exc
        if not isinstance(tables, dict) or not tables:
            raise ValueError("the map is `table: {kind, id, tenant, columns?}` per table")
        events = (yaml.safe_load(row["cartridge"]) or {}).get("events") or {}
        missing = sorted(f"{m.get('kind')}.{op}" for m in tables.values() if isinstance(m, dict) for op in ("created", "updated", "deleted")
                         if f"{m.get('kind')}.{op}" not in events)
        if missing:
            raise ValueError(f"the cartridge does not declare {', '.join(missing)}; declare them (a handler may do nothing)")
        for t, m in tables.items():
            if not isinstance(m, dict) or not m.get("kind") or not m.get("id") or not m.get("tenant"):
                raise ValueError(f"table '{t}': give it kind, id and a tenant column")
        self.provisioner.update(row["app"], files={"cdc.yaml": map_yaml}, env={"KEEL_CDC_MAP": "/cartridge/cdc.yaml", "KEEL_PG_DSN": dsn})
        self.control._exec("UPDATE hosted_accounts SET cdc_map = ? WHERE account = ?", (map_yaml, account))
        return self.cdc(account)

    def clear_cdc(self, account: str) -> None:
        row = self._ready(account)
        self.provisioner.update(row["app"], drop_env=("KEEL_CDC_MAP", "KEEL_PG_DSN"))
        self.control._exec("UPDATE hosted_accounts SET cdc_map = NULL WHERE account = ?", (account,))

    # ---- sign-in -----------------------------------------------------------------------------

    def login(self, email: str, password: str) -> str:
        row = self.control._one("SELECT account, password FROM hosted_accounts WHERE email = ?", (email.strip().lower(),))
        if row is None or not check_password(password, row["password"]):
            raise PermissionError("wrong email or password")
        return self._session(email.strip().lower(), row["account"])

    def _session(self, email: str, account: str) -> str:
        session = secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(days=self.settings.session_days)
        self.control._exec("INSERT INTO hosted_sessions (token, email, account, expires_at) VALUES (?, ?, ?, ?)",
                           (_hash(session), email, account, expires.isoformat()))
        return session

    def session(self, token: str | None) -> tuple[str, str] | None:
        if not token:
            return None
        row = self.control._one("SELECT email, account, expires_at FROM hosted_sessions WHERE token = ?", (_hash(token),))
        if row is None or parse_iso(row["expires_at"]) < utcnow():
            return None
        return row["email"], row["account"]

    def signout(self, token: str) -> None:
        self.control._exec("DELETE FROM hosted_sessions WHERE token = ?", (_hash(token),))

    # ---- the account's Keel ------------------------------------------------------------------

    def _ready(self, account: str) -> Any:
        row = self._row(account)
        if row is None or not row["endpoint"] or not row["secret"] or (row["status"] or "ready") != "ready":
            raise LookupError("your Keel is not ready yet")
        return row

    def _keel(self, account: str, method: str, path: str, tenant: str | None = None, agent: str | None = None, body: Any = None) -> tuple[int, Any]:
        """A request to the account's container, with its secret and the tenant under the account."""
        row = self._ready(account)
        headers = {"Authorization": f"Bearer {row['secret']}"}
        if tenant is not None:
            headers["X-Keel-Tenant"] = f"{account}/{check_tenant(tenant)}"
        if agent:
            headers["X-Keel-Agent"] = agent
        r = self.http.request(method, f"{row['endpoint']}{path}", json=body, headers=headers, timeout=60)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"detail": r.text[:300]}

    def call(self, account: str, tool: str, tenant: str, agent: str | None, body: dict[str, Any]) -> tuple[int, Any]:
        if tool not in TOOLS:
            raise LookupError(f"unknown tool '{tool}'")
        return self._keel(account, "POST", f"/v1/{tool}", tenant, agent, body)

    def recent(self, account: str, tenant: str) -> tuple[int, Any]:
        return self._keel(account, "GET", "/v1/events/recent", tenant)

    def check_events(self, account: str, tenant: str, envelopes: list[dict[str, Any]]) -> tuple[int, Any]:
        return self._keel(account, "POST", "/v1/events/check", tenant, body=[{**e, "tenant": f"{account}/{tenant}"} for e in envelopes])

    def ingest(self, account: str, tenant: str, envelopes: list[dict[str, Any]]) -> tuple[int, Any]:
        """Envelopes uploaded in the console, all into the chosen tenant. The secret skips the container's quota,
        so the monthly event limit is checked here, against the account's own counters."""
        if len(envelopes) > 500:
            raise ValueError("at most 500 envelopes per request")
        row = self._ready(account)
        check_tenant(tenant)
        if (wait := Quotas(open_store(row["db"])).events(account, len(envelopes))) is not None:
            raise OverflowError(f"account '{account}' has used its events for this month (resets in {int(wait // 3600)} h)")
        status, out = self._keel(account, "POST", "/v1/events", tenant, body=[{**e, "tenant": f"{account}/{tenant}"} for e in envelopes])
        if isinstance(out, dict):
            for item in out.get("forgotten", []) + out.get("retained", []):
                item["tenant"] = str(item.get("tenant", "")).removeprefix(f"{account}/")
        return status, out

    def sdk(self, account: str, language: str) -> tuple[int, Any]:
        return self._keel(account, "GET", f"/v1/sdk/{language}")

    def new_key(self, account: str, tenant: str | None = None, agent: str | None = None, glob: bool = False) -> dict[str, Any]:
        row = self._ready(account)
        if tenant:
            check_tenant(tenant)
        full, key = create_key(self.control, account, tenant=tenant or None, agent=agent or None, glob=glob)  # the master copy
        open_store(row["db"]).put_key(key)  # the copy the account's container checks
        return {"key": full, "id": key.id, "tenant": tenant, "agent": agent, "global": glob, "endpoint": row["endpoint"], "mcp": f"{row['endpoint']}/mcp"}

    def keys(self, account: str) -> list[dict[str, Any]]:
        return [{"id": k.id, "tenant": k.tenant, "agent": k.agent, "global": k.glob, "created_at": k.created_at.isoformat() if k.created_at else None,
                 "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None} for k in self.control.api_keys(account)]

    def revoke(self, account: str, key_id: str) -> None:
        if key_id not in {k.id for k in self.control.api_keys(account)}:
            raise LookupError(f"no key '{key_id}' in this account")
        revoke_key(self.control, key_id)
        revoke_key(open_store(self._ready(account)["db"]), key_id)  # the copy the container checks

    def usage(self, account: str, month: str | None = None) -> dict[str, Any]:
        row = self._ready(account)
        limits = self.control.account(account)
        return {"account": account, "endpoint": row["endpoint"], "rows": report(open_store(row["db"]), month, account),
                "limits": {"rps": limits.rps if limits else None, "events_month": limits.events_month if limits else None}}

    # ---- the playground's agent --------------------------------------------------------------

    def allowance(self, account: str) -> dict[str, int]:
        day = today()
        used = sum(n for t, d, item, n in self.control.usage(day, day + "~") if t == f"{account}/__console__" and item == "agent_messages")
        return {"used": used, "limit": self.settings.agent_messages_day}

    def chat(self, account: str, model: str, system: str, tools: list[str], tenant: str, agent: str | None,
             messages: list[dict[str, Any]], own_key: dict[str, str] | None = None) -> dict[str, Any]:
        self._ready(account)
        if own_key and own_key.get("key"):
            provider, key = own_key.get("provider", ""), own_key["key"]
            if provider not in ("openai", "anthropic"):
                raise ValueError("your own key is for openai or anthropic")
        else:
            if model not in self.settings.models:
                raise ValueError(f"unknown model '{model}'")
            if not self.settings.ofox_key:
                raise LookupError("no model is configured here: use your own key")
            allow = self.allowance(account)
            if allow["used"] >= allow["limit"]:
                raise OverflowError(f"today's {allow['limit']} messages are used: come back tomorrow or use your own key")
            provider, key = "ofox", self.settings.ofox_key
            self.control.meter([(f"{account}/__console__", today(), "agent_messages", 1)])

        def call_tool(name: str, args: dict[str, Any]) -> Any:
            status, out = self.call(account, name, tenant, agent, args)
            if status >= 400:
                raise RuntimeError(out.get("detail") if isinstance(out, dict) else out)
            return out

        new, trace = agents.run(agents.chat_api(self.http, provider, key, model), call_tool, system, messages, tools)
        return {"messages": new, "trace": trace, "allowance": self.allowance(account)}

    def agents_of(self, account: str) -> list[dict[str, Any]]:
        rows = self.control._rows("SELECT name, config FROM hosted_agents WHERE account = ? ORDER BY name", (account,))
        return [{"name": r["name"], "config": json.loads(r["config"])} for r in rows]

    def save_agent(self, account: str, name: str, config: dict[str, Any]) -> None:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("an agent needs a name of up to 100 characters")
        text = json.dumps(config)
        if len(text) > 100_000:
            raise ValueError("the agent's configuration is larger than 100 KB")
        with self.control.transaction():
            self.control._exec("DELETE FROM hosted_agents WHERE account = ? AND name = ?", (account, name))
            self.control._exec("INSERT INTO hosted_agents (account, name, config, updated_at) VALUES (?, ?, ?, ?)",
                               (account, name, text, utcnow().isoformat()))

    def delete_agent(self, account: str, name: str) -> None:
        self.control._exec("DELETE FROM hosted_agents WHERE account = ? AND name = ?", (account, name))


COOKIE = "keel_session"


def build_app(plane: ControlPlane) -> Any:
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from pydantic import BaseModel, Field

    BODY = Body(...)  # any JSON object, passed through
    app = FastAPI(title="keel-hosted", docs_url=None, redoc_url=None, openapi_url=None)
    secure = plane.settings.secure_cookies

    @app.exception_handler(PermissionError)
    def _forbidden(_r: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(ValueError)
    def _bad(_r: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    def _missing(_r: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(OverflowError)
    def _over(_r: Request, exc: OverflowError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=429)

    def who(request: Request) -> tuple[str, str]:
        s = plane.session(request.cookies.get(COOKIE))
        if s is None:
            raise HTTPException(401, "sign in first")
        return s

    class SignupIn(BaseModel):
        email: str
        account: str
        password: str
        invite: str

    class LoginIn(BaseModel):
        email: str
        password: str

    def signed_in(session: str) -> JSONResponse:
        r = JSONResponse({"ok": True})
        r.set_cookie(COOKIE, session, max_age=plane.settings.session_days * 86400, httponly=True, secure=secure, samesite="lax")
        return r

    class KeyIn(BaseModel):
        tenant: str | None = None
        agent: str | None = None
        glob: bool = Field(False, alias="global")

    class ChatIn(BaseModel):
        model: str
        system: str = ""
        tools: list[str] = list(TOOLS)
        tenant: str = "demo"
        agent: str | None = None
        messages: list[dict[str, Any]]
        own_key: dict[str, str] | None = None

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/slug")
    def slug(email: str | None = None, account: str | None = None) -> dict[str, Any]:
        if account is None:
            if not email:
                raise HTTPException(400, "email or account")
            account = plane.propose(email)
        why = plane.available(account)
        return {"account": account, "available": why is None, "error": why}

    @app.post("/api/signup")
    def signup(body: SignupIn) -> Any:
        return signed_in(plane.signup(body.email, body.account, body.password, body.invite))

    @app.post("/api/login")
    def login(body: LoginIn) -> Any:
        return signed_in(plane.login(body.email, body.password))

    @app.post("/api/signout")
    def signout(request: Request) -> Any:
        if token := request.cookies.get(COOKIE):
            plane.signout(token)
        r = JSONResponse({"ok": True})
        r.delete_cookie(COOKIE)
        return r

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        return plane.me(*who(request))

    @app.get("/api/cartridge")
    def cartridge(request: Request) -> dict[str, Any]:
        return plane.cartridge(who(request)[1])

    @app.post("/api/v1/{tool}")
    def tool(tool: str, request: Request, body: dict[str, Any] = BODY) -> Any:
        _, account = who(request)
        return answer(plane.call(account, tool, tenant_of(request), request.headers.get("x-keel-agent"), body))

    def tenant_of(request: Request) -> str:
        return request.headers.get("x-keel-tenant") or "demo"

    def answer(result: tuple[int, Any]) -> JSONResponse:
        return JSONResponse(result[1], status_code=result[0])

    @app.post("/api/keys")
    def new_key(body: KeyIn, request: Request) -> dict[str, Any]:
        return plane.new_key(who(request)[1], body.tenant, body.agent, body.glob)

    @app.get("/api/keys")
    def keys(request: Request) -> list[dict[str, Any]]:
        return plane.keys(who(request)[1])

    @app.delete("/api/keys/{key_id}")
    def revoke(key_id: str, request: Request) -> dict[str, bool]:
        plane.revoke(who(request)[1], key_id)
        return {"ok": True}

    @app.get("/api/events/recent")
    def recent(request: Request) -> Any:
        return answer(plane.recent(who(request)[1], tenant_of(request)))

    @app.post("/api/events/check")
    def check_events(request: Request, body: list[dict[str, Any]] = BODY) -> Any:
        return answer(plane.check_events(who(request)[1], tenant_of(request), body))

    @app.post("/api/events")
    def ingest(request: Request, body: list[dict[str, Any]] = BODY) -> Any:
        return answer(plane.ingest(who(request)[1], tenant_of(request), body))

    @app.get("/api/usage")
    def usage(request: Request, month: str | None = None) -> dict[str, Any]:
        return plane.usage(who(request)[1], month)

    class CartridgeIn(BaseModel):
        cartridge_yaml: str | None = None
        handlers_py: str | None = None
        example: str | None = None

    class GenerateIn(BaseModel):
        events: list[dict[str, Any]]
        description: str = ""

    class CdcIn(BaseModel):
        dsn: str
        map: str

    @app.put("/api/cartridge")
    def replace_cartridge(body: CartridgeIn, request: Request) -> dict[str, Any]:
        files = {"cartridge.yaml": body.cartridge_yaml, "handlers.py": body.handlers_py or ""} if body.cartridge_yaml else None
        return {"ok": True, "problems": plane.replace_cartridge(who(request)[1], files, body.example)}

    @app.post("/api/cartridge/check")
    def check(body: CartridgeIn) -> dict[str, Any]:
        files = {"cartridge.yaml": body.cartridge_yaml or "", "handlers.py": body.handlers_py or ""}
        return {"problems": static_check(files)}

    @app.post("/api/cartridge/generate")
    def generate(body: GenerateIn, request: Request) -> dict[str, Any]:
        return plane.generate(who(request)[1], body.events, body.description)

    @app.get("/api/cdc")
    def cdc(request: Request) -> dict[str, Any]:
        return plane.cdc(who(request)[1])

    @app.put("/api/cdc")
    def set_cdc(body: CdcIn, request: Request) -> dict[str, Any]:
        return plane.set_cdc(who(request)[1], body.dsn, body.map)

    @app.delete("/api/cdc")
    def clear_cdc(request: Request) -> dict[str, bool]:
        plane.clear_cdc(who(request)[1])
        return {"ok": True}

    @app.put("/api/semantic")
    def semantic(request: Request) -> dict[str, Any]:
        return plane.enable_semantic(who(request)[1])

    @app.post("/embed/v1/embeddings")
    def embeddings(request: Request, body: dict[str, Any] = BODY) -> Any:
        auth = request.headers.get("authorization", "")
        return plane.embed(auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else "", body)

    @app.get("/api/sdk/{language}")
    def sdk(language: str, request: Request) -> Any:
        return answer(plane.sdk(who(request)[1], language))

    @app.get("/api/agent/models")
    def models(request: Request) -> dict[str, Any]:
        _, account = who(request)
        return {"models": [{"id": m, "label": m.split("/", 1)[-1]} for m in plane.settings.models], "default": plane.settings.models[0],
                "allowance": plane.allowance(account)}

    @app.post("/api/agent/chat")
    def chat(body: ChatIn, request: Request) -> dict[str, Any]:
        _, account = who(request)
        return plane.chat(account, body.model, body.system, body.tools, body.tenant, body.agent, body.messages, body.own_key)

    @app.get("/api/agents")
    def list_agents(request: Request) -> list[dict[str, Any]]:
        return plane.agents_of(who(request)[1])

    @app.put("/api/agents/{name}")
    def save_agent(name: str, request: Request, body: dict[str, Any] = BODY) -> dict[str, bool]:
        plane.save_agent(who(request)[1], name, body.get("config", body))
        return {"ok": True}

    @app.delete("/api/agents/{name}")
    def delete_agent(name: str, request: Request) -> dict[str, bool]:
        plane.delete_agent(who(request)[1], name)
        return {"ok": True}

    dist = Path(plane.settings.static_dir).resolve() if plane.settings.static_dir else None

    @app.get("/{path:path}", include_in_schema=False)
    def console(path: str) -> Any:
        """The console's build; any other path is one of its routes, so it gets index.html."""
        if path.startswith(("api/", "embed/")):
            raise HTTPException(404)
        if dist is None or not (dist / "index.html").is_file():
            return HTMLResponse("<!doctype html><title>Keel</title><p>The console is not built here (console/: npm run build).</p>")
        f = (dist / path).resolve()
        if path and f.is_file() and f.is_relative_to(dist):
            return FileResponse(f)
        return FileResponse(dist / "index.html")

    return app


def from_env() -> Any:
    """The app as uvicorn serves it, configured by environment variables (hosted/README.md)."""
    import httpx

    from hosted.provision import AccountDatabases, CoolifyProvisioner

    env = os.environ
    http = httpx.Client(timeout=30)
    settings = Settings(
        invite_codes={c for c in env.get("HOSTED_INVITE_CODES", "").split(",") if c}, examples_dir=env.get("HOSTED_EXAMPLES_DIR", "examples"),
        rps=int(env.get("HOSTED_DEFAULT_RPS", "10")), events_month=int(env.get("HOSTED_DEFAULT_EVENTS_MONTH", "100000")),
        secure_cookies=env.get("HOSTED_SECURE_COOKIES", "1") == "1", static_dir=env.get("HOSTED_CONSOLE_DIR"),
        agent_messages_day=int(env.get("HOSTED_AGENT_MESSAGES_DAY", "50")), ofox_key=env.get("OFOX_API_KEY") or None,
        public_url=env.get("HOSTED_PUBLIC_URL", "https://keel.angelkurten.com"), embed_url=env.get("HOSTED_EMBED_URL") or None,
    )
    if env.get("HOSTED_MODELS"):
        settings.models = tuple(m for m in env["HOSTED_MODELS"].split(",") if m)
    plane = ControlPlane(
        open_store(env["HOSTED_CONTROL_DB"]),
        AccountDatabases(env["HOSTED_MYSQL_ADMIN_URL"], env.get("HOSTED_MYSQL_HOST_FOR_ACCOUNTS")),
        CoolifyProvisioner(env["COOLIFY_URL"], env["COOLIFY_TOKEN"], env["COOLIFY_PROJECT_UUID"], env["COOLIFY_SERVER_UUID"],
                           env["HOSTED_BASE_DOMAIN"], env.get("KEEL_IMAGE_TAG", "latest")),
        settings, http,
    )
    plane.heal()
    return build_app(plane)
