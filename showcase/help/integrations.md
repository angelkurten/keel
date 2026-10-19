# Integrations: Salesforce, HubSpot, Slack, Jira, Zendesk

Connected apps sync every 5 minutes. Common errors:

- `INVALID_SESSION_ID` (Salesforce): the connection token expired. Reconnect the app under Settings > Integrations.
- `REQUEST_LIMIT_EXCEEDED` (Salesforce): the customer's org hit its daily API limit. Lower the sync batch size or ask the customer to raise the limit.
- Slack `channel_not_found`: the channel was archived or the bot was removed.

An integration that keeps failing is marked "failing" and its admin gets an email.
