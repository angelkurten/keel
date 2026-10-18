"""The control plane's web app: invite-only sign-up and the usage panel.

    uvicorn hosted.app:app   (configured by environment, see hosted/README.md)

Sign-up creates the account in the control database (limits and a master copy of its first key, so the
panel can recognise it), creates the account's own database and writes the same key and limits there,
then asks the provisioner for its container. The control plane never imports a customer's handlers.py:
loading it would run their code here, so the cartridge is checked as YAML and runs only in its container.
"""

# no `from __future__ import annotations`: FastAPI resolves the form parameters of the routes defined in
# build_app from real annotations (as in keel/http.py).
import html
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hosted.provision import Databases, Provisioner
from keel.accounts import set_account
from keel.envelope import utcnow
from keel.keys import check_account, create_key, resolve
from keel.store import open_store
from keel.usage import report

EXAMPLES = ("fleet", "crm", "clinics", "finance")
REGISTRY = """CREATE TABLE IF NOT EXISTS hosted_accounts (
  account VARCHAR(64) NOT NULL PRIMARY KEY, email VARCHAR(191) NOT NULL, app VARCHAR(64), endpoint VARCHAR(191),
  db TEXT NOT NULL, created_at VARCHAR(40) NOT NULL)"""


@dataclass
class Settings:
    invite_codes: set[str] = field(default_factory=set)  # empty: sign-up closed
    rps: int = 10
    events_month: int = 100_000
    examples_dir: str = "examples"


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


class ControlPlane:
    def __init__(self, control: Any, databases: Databases, provisioner: Provisioner, settings: Settings) -> None:
        self.control, self.databases, self.provisioner, self.settings = control, databases, provisioner, settings
        control._exec(REGISTRY)

    def signup(self, account: str, email: str, invite: str, files: dict[str, str]) -> dict[str, Any]:
        if not self.settings.invite_codes or invite not in self.settings.invite_codes:
            raise PermissionError("sign-up is by invitation during the pilots: write to angel@angelkurten.com")
        check_account(account)
        if "@" not in email or len(email) > 191:
            raise ValueError("an email address, please")
        check_cartridge(files)
        if self.control._one("SELECT account FROM hosted_accounts WHERE account = ?", (account,)) or self.control.account(account):
            raise ValueError(f"account '{account}' is taken")
        db = self.databases.create(account)
        try:
            set_account(self.control, account, rps=self.settings.rps, events_month=self.settings.events_month)
            full, key = create_key(self.control, account)  # the master copy: the panel recognises the key
            own = open_store(db)
            own.put_key(key)  # the same record (hash only): the account's container checks keys here
            set_account(own, account, rps=self.settings.rps, events_month=self.settings.events_month)
            env = {"KEEL_DB": db, "KEEL_ACCOUNT": account, "KEEL_SECRET": secrets.token_urlsafe(32)}
            app_id, endpoint = self.provisioner.create(account, files, env)
        except Exception:
            self.databases.drop(account)
            raise
        self.control._exec("INSERT INTO hosted_accounts (account, email, app, endpoint, db, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (account, email, app_id, endpoint, db, utcnow().isoformat()))
        return {"account": account, "key": full, "endpoint": endpoint}

    def usage(self, full_key: str) -> dict[str, Any]:
        key = resolve(self.control, full_key)
        if key is None:
            raise PermissionError("unknown or revoked key")
        row = self.control._one("SELECT endpoint, db FROM hosted_accounts WHERE account = ?", (key.account,))
        if row is None:
            raise PermissionError("this key belongs to no hosted account")
        limits = self.control.account(key.account)
        return {"account": key.account, "endpoint": row["endpoint"], "rows": report(open_store(row["db"]), account=key.account),
                "rps": limits.rps if limits else None, "events_month": limits.events_month if limits else None}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Keel hosted</title><style>
:root{{--bg:#f4f6f7;--fg:#13212c;--muted:#5b6b77;--line:#d4dde3;--accent:#1f5a8c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0e161d;--fg:#e3ebf0;--muted:#93a3b0;--line:#26343f;--accent:#7cb2e2}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;padding:32px max(16px,env(safe-area-inset-left))}}
main{{max-width:640px;margin:0 auto;display:grid;gap:20px}} h1{{margin:0;font-size:28px}} p{{margin:0;color:var(--muted)}}
form{{display:grid;gap:12px}} label{{display:grid;gap:4px;font-weight:600}} input,select,textarea{{font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:transparent;color:inherit}}
button{{font:inherit;padding:10px;border:0;border-radius:6px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}}
pre,code{{font:13px ui-monospace,Menlo,monospace}} pre{{padding:12px;border:1px solid var(--line);border-radius:6px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:6px;border-bottom:1px solid var(--line);text-align:left;font-variant-numeric:tabular-nums}}
.err{{color:#b3261e}} a{{color:var(--accent)}}
</style></head><body><main>{body}</main></body></html>"""


def build_app(plane: ControlPlane) -> Any:
    from fastapi import FastAPI, Form, UploadFile
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="keel-hosted", docs_url=None, redoc_url=None)
    e = html.escape

    def page(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(PAGE.format(body=body), status_code=status)

    def form(err: str = "") -> str:
        opts = "".join(f'<option value="{x}">{x} (example)</option>' for x in EXAMPLES)
        return f"""<h1>Keel hosted</h1><p>One Keel per account: your cartridge, your database, your endpoint. By invitation during the pilots.
        Guide: <a href="https://angelkurten.github.io/keel/pilot/">your first pilot in a day</a>.</p>
        {f'<p class="err">{e(err)}</p>' if err else ''}
        <form method="post" action="/signup" enctype="multipart/form-data">
        <label>Account <input id="account" name="account" required pattern="[a-z0-9][a-z0-9-]{{0,31}}" placeholder="acme"></label>
        <label>Email <input id="email" name="email" type="email" required></label>
        <label>Invitation code <input id="invite" name="invite" required></label>
        <label>Cartridge <select id="example" name="example"><option value="">my own (upload below)</option>{opts}</select></label>
        <label>cartridge.yaml <input id="yaml" name="cartridge_yaml" type="file" accept=".yaml,.yml"></label>
        <label>handlers.py <input id="handlers" name="handlers_py" type="file" accept=".py"></label>
        <button>Create my Keel</button></form>
        <p>Already have a key? <a href="/panel">See your usage</a>.</p>"""

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        return page(form())

    @app.post("/signup", response_class=HTMLResponse)
    async def signup(account: str = Form(...), email: str = Form(...), invite: str = Form(...), example: str = Form(""),
                     cartridge_yaml: UploadFile | None = None, handlers_py: UploadFile | None = None) -> HTMLResponse:
        try:
            if example:
                if example not in EXAMPLES:
                    raise ValueError("unknown example")
                base = Path(plane.settings.examples_dir) / example
                files = {n: (base / n).read_text(encoding="utf-8") for n in ("cartridge.yaml", "handlers.py")}
            else:
                if not (cartridge_yaml and cartridge_yaml.filename and handlers_py and handlers_py.filename):
                    raise ValueError("upload cartridge.yaml and handlers.py, or pick an example")
                files = {"cartridge.yaml": (await cartridge_yaml.read()).decode(), "handlers.py": (await handlers_py.read()).decode()}
            out = plane.signup(account.strip(), email.strip(), invite.strip(), files)
        except (ValueError, PermissionError, UnicodeDecodeError) as exc:
            return page(form(str(exc)), 400)
        mcp = f'{{"mcpServers": {{"keel": {{"url": "{out["endpoint"]}/mcp", "headers": {{"Authorization": "Bearer {out["key"]}"}}}}}}}}'
        return page(f"""<h1>Your Keel is on its way</h1>
        <p>It takes a minute or two to start. Copy the key now: it is shown once, and only its hash is kept.</p>
        <label>Key <pre>{e(out["key"])}</pre></label><label>Endpoint <pre>{e(out["endpoint"])}</pre></label>
        <label>Try it <pre>curl {e(out["endpoint"])}/v1/search -H "Authorization: Bearer {e(out["key"])}" -H "X-Keel-Tenant: demo" -H "Content-Type: application/json" -d '{{"text": ""}}'</pre></label>
        <label>MCP client (streamable HTTP; start the server with a tenant for /mcp) <pre>{e(mcp)}</pre></label>
        <p>Next: <a href="https://angelkurten.github.io/keel/pilot/">send your events and connect your agents</a>. Usage: <a href="/panel">panel</a>.</p>""")

    @app.get("/panel", response_class=HTMLResponse)
    def panel_form() -> HTMLResponse:
        return page("""<h1>Usage</h1><form method="post" action="/panel"><label>Your key <input id="key" name="key" type="password" required></label>
        <button>Show</button></form>""")

    @app.post("/panel", response_class=HTMLResponse)
    def panel(key: str = Form(...)) -> HTMLResponse:
        try:
            u = plane.usage(key.strip())
        except PermissionError as exc:
            return page(f'<h1>Usage</h1><p class="err">{e(str(exc))}</p><p><a href="/panel">Try again</a></p>', 403)
        rows = "".join(f"<tr><td>{e(r['month'])}</td><td>{e(r['tenant'])}</td><td>{r['events']:,}</td><td>{sum(r['calls'].values()):,}</td></tr>" for r in u["rows"])
        limits = f"{u['events_month']:,} events a month, {u['rps']} requests a second" if u["events_month"] else "no limits"
        return page(f"""<h1>{e(u['account'])}</h1><p>Endpoint <code>{e(u['endpoint'])}</code> · {e(limits)}</p>
        <table><tr><th>Month</th><th>Tenant</th><th>Events</th><th>Tool calls</th></tr>{rows or '<tr><td colspan="4">Nothing yet: send your first events.</td></tr>'}</table>""")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    return app


def from_env() -> Any:
    """The app as uvicorn serves it, configured by environment variables (hosted/README.md)."""
    from hosted.provision import AccountDatabases, CoolifyProvisioner

    env = os.environ
    plane = ControlPlane(
        open_store(env["HOSTED_CONTROL_DB"]),
        AccountDatabases(env["HOSTED_MYSQL_ADMIN_URL"], env.get("HOSTED_MYSQL_HOST_FOR_ACCOUNTS")),
        CoolifyProvisioner(env["COOLIFY_URL"], env["COOLIFY_TOKEN"], env["COOLIFY_PROJECT_UUID"], env["COOLIFY_SERVER_UUID"],
                           env["HOSTED_BASE_DOMAIN"], env.get("KEEL_IMAGE_TAG", "latest")),
        Settings(invite_codes={c for c in env.get("HOSTED_INVITE_CODES", "").split(",") if c}, examples_dir=env.get("HOSTED_EXAMPLES_DIR", "examples"),
                 rps=int(env.get("HOSTED_DEFAULT_RPS", "10")), events_month=int(env.get("HOSTED_DEFAULT_EVENTS_MONTH", "100000"))),
    )
    return build_app(plane)
