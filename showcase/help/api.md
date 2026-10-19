# API limits

The API allows 600 requests per minute on Pro and 3,000 on Enterprise; Starter has no API access. Exceeding the limit returns 429 with a Retry-After header. Workflow runs started through the API count against the same limit.
