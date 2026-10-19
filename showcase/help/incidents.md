# Incidents and the status page

Platform incidents are posted on status.relay.example with their region (us-east, eu-west, ap-south), start and end. Customers in the affected region may see failed or delayed workflow runs and 5xx errors from the API or on webhook deliveries during the window.

When a customer reports errors, check whether an incident affected their region at that time before investigating their workflows. Runs that failed during an incident can be replayed from the run history.
