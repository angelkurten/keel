"""The guided scenarios (docs/design/showcase.md): a question, the customer it is about, and the expected answer the
judge grades against. The facts behind each are planted by examples/relay/generate.py and checked by test_relay.py."""

SCENARIOS = [
    {"id": "overcharge", "title": "A 'double charge'", "strength": "context", "customer": "acme",
     "question": "Acme says they were charged twice in September. What happened?",
     "expected": "Not a double charge: Acme upgraded from Pro to Enterprise on Sep 12, so besides the regular September Pro invoice "
                 "($1,200) they got a separate prorated invoice INV-acme-2026-09-U ($506.67) for the upgrade for Sep 12–30."},
    {"id": "plan-then", "title": "The plan on the day", "strength": "time", "customer": "globex",
     "question": "Which plan did Globex have when they reported the outage on October 3?",
     "expected": "Pro. Globex upgraded to Enterprise only on October 10."},
    {"id": "reopened", "title": "A ticket reopened", "strength": "time", "customer": "initech",
     "question": "Is Initech's Salesforce sync problem still open?",
     "expected": "No. Ticket T-2201 was resolved on Oct 2, reopened on Oct 6 (request limit exceeded) and resolved again on Oct 14; "
                 "the integration is connected since then."},
    {"id": "refund", "title": "A refund request", "strength": "rules", "customer": "umbrella",
     "question": "Umbrella wants a refund of their last charge. Can we do it?",
     "expected": "No. The last charge is the annual plan invoice INV-umbrella-2026-A ($3,240), paid on Sep 20, more than 14 days ago; "
                 "refunds are only within 14 days of payment."},
    {"id": "sla", "title": "SLA at risk", "strength": "rules", "customer": "stark",
     "question": "Does Stark have any ticket past its SLA?",
     "expected": "Yes: T-3112 (SSO login loop for all users, high priority) has been open since Oct 14 05:00 UTC, about 31 hours, "
                 "far beyond the 4-hour Enterprise SLA. It should be escalated to the support lead and the CSM (Dana Ortiz)."},
    {"id": "lookalike", "title": "Two similar names", "strength": "scope", "customer": "hooli",
     "question": "Show me Hooli's last two payments.",
     "expected": "Hooli (Pro, 30 seats) paid $900 for October (INV-hooli-2026-10, on Oct 2) and $900 for September (INV-hooli-2026-09, "
                 "on Sep 2). Nothing from Hooli Labs."},
    {"id": "incident", "title": "Errors and an incident", "strength": "context", "customer": "wayne",
     "question": "Wayne reported 502 errors on webhook runs on October 2. Is it related to an incident?",
     "expected": "Yes. Incident INC-7 (webhook delivery degraded, eu-west, Wayne's region) affected them on Oct 2 from 09:10 to 11:40 UTC; "
                 "their ticket T-2301 was opened at 09:30 and resolved after deliveries recovered."},
    {"id": "how-to", "title": "A how-to", "strength": "honesty", "customer": "stark",
     "question": "How do I set up SSO with Okta for Stark?",
     "expected": "Create a SAML 2.0 app in Okta with the ACS URL https://app.relay.example/sso/saml/acs and audience "
                 "https://app.relay.example/sso/saml/metadata, email as Name ID, upload Okta's metadata in Settings > Security > SSO, "
                 "test, and only then require SSO. A login loop usually means the ACS URL or audience does not match."},
    {"id": "memory", "title": "Two agents, one memory", "strength": "memory", "customer": "acme",
     "steps": [
         {"customer": "acme", "role": "support", "new_conversation": False,
          "question": "Lucía Gómez, who receives Acme's invoices, just told me she wants them in Spanish from now on. Please record it so billing knows."},
         {"customer": "acme", "role": "billing", "new_conversation": True,
          "question": "I'm preparing Acme's invoice for November 1. Who should receive it, and in which language?"},
         {"customer": "globex", "role": "billing", "new_conversation": True,
          "question": "I'm preparing Globex's invoice for November 1. Who should receive it, and in which language?"},
     ],
     "expected": "Step 2 (billing agent): send Acme's invoice to Lucía Gómez in Spanish, as the support agent recorded; noting that her contact "
                 "record still says English and should be updated is right, not a hedge. Step 3: Globex's invoice in English to its billing "
                 "contacts; nothing about Spanish carries over from Acme."},
]
