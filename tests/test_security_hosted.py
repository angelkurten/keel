"""Adversarial tests for the hosted control plane (hosted/app.py) and the console API it serves.

A passing test is a defence that holds. A test marked xfail(strict=True, reason="SECURITY: ...") is a real weakness,
kept here so that fixing it turns the test green (and strict xfail then fails until the mark is removed).
Nothing here reaches a real service: the fakes of tests/test_hosted.py stand in for Coolify, MySQL and the containers.
"""

import json
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from test_hosted import PW, ROOT, FakeProvisioner, plane, wait

import hosted.app as hosted_app
from hosted.app import COOKIE, _hash, build_app, hash_password
from keel.envelope import utcnow

CLINICS = {n: (ROOT / "examples/clinics" / n).read_text() for n in ("cartridge.yaml", "handlers.py")}


def signup(p, email="a@acme.com", account="acme", invite="pilot-1"):
    client = TestClient(build_app(p))
    r = client.post("/api/signup", json={"email": email, "account": account, "password": PW, "invite": invite})
    assert r.status_code == 200, r.text
    wait(client)
    return client


@pytest.fixture
def two(tmp_path):
    """Two ready accounts on one control plane: acme (the victim) and zeta (the attacker)."""
    p = plane(tmp_path)
    return p, signup(p), signup(p, "z@zeta.io", "zeta", invite="pilot-2")  # an invitation opens one account


# ---- sign-up and invitations -----------------------------------------------------------------


def test_signup_without_a_valid_invite_is_refused(tmp_path):
    p = plane(tmp_path)
    client = TestClient(build_app(p))
    for invite in ("", " ", "PILOT-1", "pilot-1 ", "pilot", "pilot-1\x00", None):
        r = client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": invite})
        assert r.status_code in (403, 422), (invite, r.text)
    body = {"email": "a@acme.com", "account": "acme", "password": PW}
    assert client.post("/api/signup", json=body).status_code == 422  # no invite field at all
    assert p.provisioner.created == [] and p._row("acme") is None


def test_an_invite_is_used_once(tmp_path):
    p = plane(tmp_path)
    signup(p)
    again = TestClient(build_app(p)).post("/api/signup", json={"email": "b@beta.io", "account": "beta", "password": PW, "invite": "pilot-1"})
    assert again.status_code == 403


def test_account_slugs_cannot_collide_or_inject(tmp_path):
    p = plane(tmp_path)
    signup(p)
    client = TestClient(build_app(p))
    hostile = ("ACME", "Acme", "acme ", " acme", "acme\n", "acmе",  # Cyrillic е
               "__global__", "acme/demo", "acme.evil.com", "a;rm -rf /", "a`id`", "$(id)", "a'b", 'a"b', "a b", "a%2fb",
               "../x", "-acme", "x" * 33, "", "acme\x00")
    for i, account in enumerate(hostile):
        r = client.post("/api/signup", json={"email": f"u{i}@x{i}.io", "account": account, "password": PW, "invite": "pilot-1"})
        assert r.status_code == 400, (account, r.text)
    assert [c[0] for c in p.provisioner.created] == ["acme"]  # nothing hostile reached Coolify or MySQL
    assert p.databases.dropped == []


def test_an_email_has_one_account_whatever_its_case(tmp_path):
    p = plane(tmp_path)
    signup(p)
    r = TestClient(build_app(p)).post("/api/signup", json={"email": " A@ACME.COM ", "account": "acme-2", "password": PW, "invite": "pilot-2"})
    assert r.status_code == 400 and "already" in r.json()["detail"]


def test_reserved_slugs_are_refused(tmp_path):
    p = plane(tmp_path)
    assert all(p.available(name) is not None for name in ("www", "admin", "api", "mail", "console", "mcp", "embed", "coolify"))


def test_a_slug_is_a_valid_dns_label(tmp_path):
    assert plane(tmp_path).available("acme-") is not None


# ---- sign-in, sessions and cookies -----------------------------------------------------------


def test_session_cookie_flags(tmp_path):
    p = plane(tmp_path, secure_cookies=True)
    r = TestClient(build_app(p), base_url="https://testserver").post(
        "/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "domain=" not in cookie  # host-only: account containers on sibling subdomains never receive it
    token = r.cookies[COOKIE]
    assert len(token) >= 40 and p.control._one("SELECT token FROM hosted_sessions")["token"] == _hash(token)  # only the hash is kept


def test_forged_expired_and_signed_out_sessions_are_refused(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    token = client.cookies[COOKIE]
    anon = TestClient(build_app(p))
    for forged in ("", "x", _hash(token), token[:-1], token + "A", "' OR '1'='1"):
        anon.cookies.set(COOKIE, forged)
        assert anon.get("/api/me").status_code == 401, forged
    # the stored hash is not a usable token
    anon.cookies.set(COOKIE, p.control._one("SELECT token FROM hosted_sessions")["token"])
    assert anon.get("/api/me").status_code == 401
    # expiry is enforced server side, whatever the cookie's max-age
    p.control._exec("UPDATE hosted_sessions SET expires_at = ?", ((utcnow() - timedelta(seconds=1)).isoformat(),))
    assert client.get("/api/me").status_code == 401
    # sign-out deletes the session, so a copied cookie stops working too
    fresh = TestClient(build_app(p))
    assert fresh.post("/api/login", json={"email": "a@acme.com", "password": PW}).status_code == 200
    stolen = fresh.cookies[COOKIE]
    fresh.post("/api/signout")
    replay = TestClient(build_app(p))
    replay.cookies.set(COOKIE, stolen)
    assert replay.get("/api/me").status_code == 401


def test_login_does_not_say_which_part_was_wrong(tmp_path):
    p = plane(tmp_path)
    signup(p)
    client = TestClient(build_app(p))
    unknown = client.post("/api/login", json={"email": "nobody@acme.com", "password": PW})
    wrong = client.post("/api/login", json={"email": "a@acme.com", "password": "not the password"})
    assert unknown.status_code == wrong.status_code == 403 and unknown.json() == wrong.json()
    p.control._exec("UPDATE hosted_accounts SET password = NULL")  # an account from before passwords
    assert client.post("/api/login", json={"email": "a@acme.com", "password": ""}).status_code == 403


def test_login_is_rate_limited(tmp_path):
    p = plane(tmp_path)
    signup(p)
    client = TestClient(build_app(p))
    codes = [client.post("/api/login", json={"email": "a@acme.com", "password": f"guess-{i:06d}"}).status_code for i in range(25)]
    assert 429 in codes


def test_login_costs_the_same_for_an_unknown_email(tmp_path, monkeypatch):
    p = plane(tmp_path)
    signup(p)
    real, calls = hosted_app.hashlib.scrypt, []
    monkeypatch.setattr(hosted_app.hashlib, "scrypt", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    TestClient(build_app(p)).post("/api/login", json={"email": "nobody@acme.com", "password": PW})
    assert calls


def test_a_password_reset_ends_open_sessions(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    # what hosted/__main__.py does
    p.control._exec("UPDATE hosted_accounts SET password = ? WHERE account = ?", (hash_password("a brand new password"), "acme"))
    assert client.get("/api/me").status_code == 401


# ---- CSRF ----------------------------------------------------------------------------------


def test_state_changing_endpoints_need_a_json_body(tmp_path):
    """Account containers run customer code on sibling subdomains, which are same-site for SameSite=Lax. What stops
    a page there from posting a form here is that these endpoints only parse application/json, which a cross-origin
    page cannot send without a CORS preflight, and no CORS is configured."""
    p = plane(tmp_path)
    client = signup(p)
    before = len(client.get("/api/keys").json())
    evil = {"Origin": "https://evil.keel.angelkurten.com"}
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        h = {**evil, "Content-Type": ctype}
        assert client.post("/api/keys", content='{"global": true}', headers=h).status_code in (403, 422)  # refused for its origin, or for its body
        assert client.put("/api/cartridge", content='{"example": "crm"}', headers=h).status_code in (403, 422)  # refused for its origin, or for its body
        assert client.post("/api/v1/annotate", content='{"subject": "x"}', headers=h).status_code in (403, 422)  # refused for its origin, or for its body
        assert client.post("/api/events", content="[]", headers=h).status_code in (403, 422)  # refused for its origin, or for its body
        assert client.put("/api/cdc", content='{"dsn": "postgresql://x", "map": "a: 1"}', headers=h).status_code in (403, 422)  # refused for its origin, or for its body
    assert len(client.get("/api/keys").json()) == before
    pre = client.options("/api/keys", headers={**evil, "Access-Control-Request-Method": "DELETE"})
    assert "access-control-allow-origin" not in pre.headers


def test_signout_refuses_a_foreign_origin(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    client.post("/api/signout", content="", headers={"Origin": "https://evil.keel.angelkurten.com",
                                                     "Content-Type": "application/x-www-form-urlencoded"})
    assert p.control._one("SELECT COUNT(*) AS n FROM hosted_sessions")["n"] == 1


# ---- acting on another account ---------------------------------------------------------------


def test_one_account_cannot_reach_another(two):
    p, acme, zeta = two
    key = acme.post("/api/keys", json={"tenant": "demo"}).json()
    acme.put("/api/agents/helper", json={"config": {"system": "acme's prompt"}})
    # keys: list, revoke
    assert zeta.get("/api/keys").json() == []
    assert zeta.delete(f"/api/keys/{key['id']}").status_code == 409
    assert next(k for k in acme.get("/api/keys").json() if k["id"] == key["id"])["revoked_at"] is None
    # a body naming the other account is ignored: the account comes from the session only
    made = zeta.post("/api/keys", json={"tenant": "demo", "account": "acme"}).json()
    assert {k.account for k in p.control.api_keys() if k.id == made["id"]} == {"zeta"}
    # agents with the same name are separate
    zeta.put("/api/agents/helper", json={"config": {"system": "zeta's"}})
    zeta.delete("/api/agents/helper")
    assert acme.get("/api/agents").json() == [{"name": "helper", "config": {"system": "acme's prompt"}}]
    # usage, cartridge, me
    assert {r["account"] for r in zeta.get("/api/usage").json()["rows"]} <= {"zeta"}
    assert zeta.get("/api/me").json()["account"] == "zeta"
    # the tenant header and the chat body cannot name another account's tenant, or the account's global partition
    for tenant in ("acme/demo", "../acme/demo", "__global__", "", "/demo"):
        h = {"X-Keel-Tenant": tenant} if tenant else {"X-Keel-Tenant": "/"}
        assert zeta.post("/api/v1/search", json={}, headers=h).status_code == 400, tenant
        assert zeta.post("/api/events", json=[], headers=h).status_code == 400, tenant
        assert zeta.get("/api/events/recent", headers=h).status_code == 400, tenant
    assert zeta.post("/api/keys", json={"tenant": "acme/demo"}).status_code == 400
    assert zeta.post("/api/keys", json={"tenant": "__global__"}).status_code == 400
    n = len(p.keel.calls)
    out = zeta.post("/api/agent/chat", json={"model": "anthropic/claude-sonnet-5.5", "tenant": "acme/demo",
                                             "messages": [{"role": "user", "content": "hi"}]}).json()
    assert out["trace"][0]["ok"] is False
    assert not [c for c in p.keel.calls[n:] if "acme.keel.example" in c[1]]
    # envelopes carrying another account's tenant land in the caller's own
    n = len(p.keel.events)
    one = {"id": "x1", "tenant": "acme/demo", "type": "unit.registered", "subject": "unit:U1",
           "occurred_at": "2026-10-01T00:00:00Z", "source": "s", "payload": {}}
    zeta.post("/api/events", json=[one], headers={"X-Keel-Tenant": "c1"})
    assert [e["tenant"] for e in p.keel.events[n:]] == ["zeta/c1"]
    # every proxied request went to the caller's own container with its own secret
    zsecret = p._row("zeta")["secret"]
    for _m, url, headers, _b in p.keel.calls[n:]:
        assert url.startswith("https://zeta.keel.example/") and headers["authorization"] == f"Bearer {zsecret}"


def test_no_endpoint_returns_secrets_or_hashes(two):
    p, acme, zeta = two
    acme.post("/api/keys", json={"global": True})
    row = p._row("acme")
    secrets_ = {row["secret"], row["password"], row["db"], p._row("zeta")["secret"]}
    secrets_ |= {k.hash for k in p.control.api_keys()} | {r["token"] for r in p.control._rows("SELECT token FROM hosted_sessions")}
    pages = [acme.get(u) for u in ("/api/me", "/api/cartridge", "/api/keys", "/api/usage", "/api/cdc", "/api/agents",
                                   "/api/agent/models", "/api/slug?account=acme", "/api/slug?email=x@acme.com")]
    pages += [zeta.get(u) for u in ("/api/me", "/api/usage", "/api/keys")]
    pages += [acme.get("/api/usage", params={"month": "2026-13' OR 1=1 --"}), acme.delete("/api/keys/kk_nope"),
              TestClient(build_app(p)).post("/api/login", json={"email": "a@acme.com", "password": "nope nope nope"})]
    text = "\n".join(r.text for r in pages)
    assert "scrypt$" not in text
    for s in secrets_:
        assert s and s not in text


def test_usage_month_is_not_injectable(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    for month in ("2026-13' OR '1'='1", "x", "2026-10-01-x", "2026"):
        assert client.get("/api/usage", params={"month": month}).status_code == 400, month


# ---- code in uploaded or generated cartridges ------------------------------------------------


def test_handlers_never_run_in_the_control_plane(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    marker = tmp_path / "pwned"
    evil = CLINICS["handlers.py"] + f"\nopen({str(marker)!r}, 'w').write('ran')\n"
    decor = f"from keel.handlers import HandlerRegistry\nregistry = HandlerRegistry()\n@registry.on(open({str(marker)!r}, 'w').write('x'))\ndef h(e, s): pass\n"
    assert client.post("/api/cartridge/check", json={"cartridge_yaml": CLINICS["cartridge.yaml"], "handlers_py": evil}).status_code == 200
    assert client.post("/api/cartridge/check", json={"cartridge_yaml": CLINICS["cartridge.yaml"], "handlers_py": decor}).status_code == 200
    assert client.put("/api/cartridge", json={"cartridge_yaml": CLINICS["cartridge.yaml"], "handlers_py": evil}).status_code == 200
    wait(client)
    assert not marker.exists()
    # what went to the container is exactly the two files, by their fixed names
    assert p.provisioner.updates[-1][1] == ["cartridge.yaml", "handlers.py"]


def test_cartridge_yaml_cannot_load_code(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    marker = tmp_path / "pwned"
    tags = "name: x\nentities: {a: {}}\nevents: {a.b: {subject: a}}\nboom: !!python/object/apply:os.system ['touch " + str(marker) + "']\n"
    assert client.put("/api/cartridge", json={"cartridge_yaml": tags, "handlers_py": ""}).status_code == 400
    assert client.post("/api/cartridge/check", json={"cartridge_yaml": tags, "handlers_py": ""}).json()["problems"]
    spec = f"os:system?command=touch+{marker}"
    factories = ("name: x\nentities: {a: {}}\nevents: {a.b: {subject: a}}\n"
                 f"consolidate: {{c: {{from: {{predicate: p}}, into: {{predicate: q}}, summarizer: '{spec}'}}}}\n"
                 f"semantic: {{index: '{spec}', embedder: '{spec}'}}\nmemory: {{summarizer: '{spec}'}}\n")
    client.post("/api/cartridge/check", json={"cartridge_yaml": factories, "handlers_py": ""})
    client.put("/api/cartridge", json={"cartridge_yaml": factories, "handlers_py": ""})
    wait(client)
    assert not marker.exists()


def test_cartridge_files_have_fixed_names(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    body = {"cartridge_yaml": CLINICS["cartridge.yaml"], "handlers_py": CLINICS["handlers.py"],
            "../../etc/passwd": "x", "files": {"../x.py": "y"}, "example": None}
    assert client.put("/api/cartridge", json=body).status_code == 200
    wait(client)
    assert all(u[1] in ([], ["cartridge.yaml", "handlers.py"]) for u in p.provisioner.updates)
    assert client.put("/api/cartridge", json={"example": "../clinics"}).status_code == 400
    assert client.put("/api/cartridge", json={"example": "/etc"}).status_code == 400


def test_cartridge_check_needs_a_session(tmp_path):
    p = plane(tmp_path)
    anon = TestClient(build_app(p))
    assert anon.post("/api/cartridge/check", json={"cartridge_yaml": CLINICS["cartridge.yaml"], "handlers_py": ""}).status_code == 401


def test_cartridge_check_reports_a_malformed_section(tmp_path):
    p = plane(tmp_path)
    signup(p)
    anon = signup(p, "b@beta.io", "beta", invite="pilot-2")  # signed in: the check needs a session now
    yaml_text = "name: x\nentities: {a: {}}\nevents: {a.b: {subject: a}}\nconsolidate: {c: {from: [1], into: [2]}}\n"
    assert anon.post("/api/cartridge/check", json={"cartridge_yaml": yaml_text, "handlers_py": ""}).status_code == 200


# ---- provisioning, proxying and static files -------------------------------------------------


def test_provisioning_sees_only_the_validated_slug(tmp_path):
    p = plane(tmp_path)
    signup(p, account="ok-1")
    account, _files, env = p.provisioner.created[0]
    assert account == "ok-1" and env["KEEL_ACCOUNT"] == "ok-1"
    assert set(env) == {"KEEL_DB", "KEEL_ACCOUNT", "KEEL_SECRET"}  # no control-plane credential reaches the container
    from hosted.provision import db_name

    assert db_name("ok-1") == "keel_ok_1"


def test_cdc_settings_reach_only_the_callers_container(two):
    p, _acme, zeta = two
    tires = "name: tires\nentities: {tire: {}}\nevents: {tire.created: {subject: tire}, tire.updated: {subject: tire}, tire.deleted: {subject: tire}}\n"
    p.control._exec("UPDATE hosted_accounts SET cartridge = ? WHERE account = 'zeta'", (tires,))
    assert zeta.put("/api/cdc", json={"dsn": "mysql://root@acme", "map": "t: {kind: tire, id: i, tenant: t}"}).status_code == 400
    r = zeta.put("/api/cdc", json={"dsn": "postgresql://u:p@db/app", "map": "t: {kind: tire, id: i, tenant: t}"})
    assert r.status_code == 200 and p.provisioner.updates[-1][0] == "app-zeta" and p.provisioner.updates[-1][1] == ["cdc.yaml"]


def test_sdk_language_reaches_only_the_sdk_route(tmp_path):
    p = plane(tmp_path)
    client = signup(p)
    n = len(p.keel.calls)
    for language in ("x%3Fq%3D1", "%2e%2e", "x%23frag"):
        client.get(f"/api/sdk/{language}")
    urls = [c[1] for c in p.keel.calls[n:]]
    assert all("?" not in u and "#" not in u and "/v1/sdk/" in u and not u.endswith("/v1/") for u in urls), urls


def test_static_files_stay_inside_the_build(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (tmp_path / "secret.txt").write_text("TOPSECRET")
    client = TestClient(build_app(plane(tmp_path, static_dir=str(dist))))
    for path in ("/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/..%2f..%2fsecret.txt",
                 "/%2e%2e%2fcontrol.db", f"/{tmp_path}/secret.txt", "//etc/passwd"):
        r = client.get(path)
        assert "TOPSECRET" not in r.text and "SQLite" not in r.text, path


def test_the_embeddings_proxy_knows_containers_only_by_their_secret(two):
    p, acme, _zeta = two
    anon = TestClient(build_app(p))
    for auth in ("", "Bearer ", "Bearer x", "Basic " + p._row("acme")["secret"], f"Bearer {p._row('acme')['secret']}x", "Bearer ' OR 1=1 --"):
        assert anon.post("/embed/v1/embeddings", json={"input": ["x"]}, headers={"Authorization": auth}).status_code == 403, auth
    # a session cookie is not a container secret
    assert acme.post("/embed/v1/embeddings", json={"input": ["x"]}).status_code == 403


def test_a_failed_provision_does_not_show_credentials(tmp_path):
    p = plane(tmp_path, provisioner=FakeProvisioner(fail=True))
    client = TestClient(build_app(p))
    client.post("/api/signup", json={"email": "a@acme.com", "account": "acme", "password": PW, "invite": "pilot-1"})
    me = wait(client)
    assert me["status"] == "failed" and str(tmp_path) not in json.dumps(me)  # the database URL stays out of the error


def test_a_provisioning_error_never_shows_the_credentials_it_was_sent(tmp_path):
    """A Coolify error may echo the environment it was given: the database URL (with its password) and the
    container's secret must not reach /api/me."""

    class Echoing(FakeProvisioner):
        def create(self, account, files, env):
            raise RuntimeError(f"422 invalid env: {env}")

    p = plane(tmp_path, provisioner=Echoing())
    client = signup(p)
    wait(client, "failed")
    shown = json.dumps(client.get("/api/me").json())
    assert "invalid env" in shown and "KEEL_SECRET" in shown  # the error itself is still useful
    assert str(tmp_path) not in shown and "hidden" in shown
