# Set up SSO with Okta

SSO (SAML 2.0) is available on Pro and Enterprise.

1. In Okta, create a SAML 2.0 app integration named Relay.
2. Single sign-on URL: `https://app.relay.example/sso/saml/acs`. Audience URI: `https://app.relay.example/sso/saml/metadata`.
3. Name ID format: EmailAddress. Add the attribute statements `email`, `firstName`, `lastName`.
4. Download the Okta metadata XML and upload it in Relay under Settings > Security > SSO, then click Test connection.
5. Turn on "Require SSO" only after an admin has signed in through Okta once.

A login loop after enabling SSO almost always means the ACS URL or the audience does not match exactly.
