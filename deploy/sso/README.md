# SSO in front of /ui

`/ui` asks for a secret or an API key. For your own team, put single sign-on in front of it instead, with no code in Keel: Caddy terminates TLS, [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) signs people in through any OIDC provider (Google Workspace, Microsoft Entra, Okta, GitHub through Dex, Keycloak), and Caddy adds the operator's secret on their behalf.

Two hostnames:

- `KEEL_API_HOST`: agents and products, with an API key (`keel keys create`) or the secret, straight to Keel.
- `KEEL_UI_HOST`: people. Not signed in, they are sent to the provider; signed in, they browse `/ui` as the operator (leave the UI's secret field empty).

```bash
docker compose -f deploy/docker-compose.yml -f deploy/sso/docker-compose.yml up -d
```

with these variables set (an `.env` next to the compose files works):

| Variable | |
|---|---|
| `KEEL_API_HOST`, `KEEL_UI_HOST` | the two hostnames, with DNS pointing at this machine; Caddy gets the certificates |
| `OIDC_ISSUER_URL` | your provider, e.g. `https://accounts.google.com` |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | an app at the provider whose redirect URL is `https://$KEEL_UI_HOST/oauth2/callback` |
| `OAUTH2_COOKIE_SECRET` | 32 random bytes, base64url: `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `SSO_EMAIL_DOMAINS` | who may sign in, e.g. `kubbo.co`; `*` for anyone the provider accepts |

Keel's own port is closed in this setup: only Caddy (80, 443) is reachable. Everyone on the UI host acts as the operator and reaches every tenant, so keep `SSO_EMAIL_DOMAINS` to your team; customers of a hosted deployment use `/ui` on the API host with their own key.

Checked on 2026-10-18 with a stub in place of oauth2-proxy: the API host answers 401 without a key, the UI host redirects to `/oauth2/sign_in` without a session, and with one `/ui` and `/v1` answer with no secret typed.
