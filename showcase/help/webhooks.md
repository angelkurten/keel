# Webhooks and 502 errors

Relay delivers webhooks with up to 5 retries over 30 minutes. A 502 on delivery usually means Relay could not reach the customer's endpoint, or, during an incident, that Relay's own edge failed.

To investigate: check the delivery log under Workflows > Webhooks, compare the times with the status page, and ask the customer whether their endpoint changed. Deliveries that exhausted their retries can be resent from the log.
