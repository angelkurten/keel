# Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | keel | 258 | 3 | 94.7 | [88.4, 98.9] | 94.1 | 97.2 | 0.392 | 0.00053 | 7679 |
| Haiku | keel+query | 258 | 3 | 96.0 | [89.7, 99.9] | 95.4 | 98.6 | 0.320 | 0.00043 | 9412 |
| Haiku | sql | 258 | 3 | 95.9 | [89.4, 99.9] | 94.9 | 100.0 | 0.341 | 0.00046 | 5021 |
| Haiku | sqlgen2 | 258 | 3 | 96.0 | [89.4, 100.0] | 95.2 | 99.3 | 0.351 | 0.00047 | 6216 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 258 | +0.1 | [-0.3, +0.5] | > -2.0 | 2 / 1 | confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 258 | +0.1 | [-0.5, +0.6] | > -2.0 | 3 / 2 | confirmed |
| H3 | query adds to Keel's five tools | keel+query - keel | 258 | +1.3 | [+0.0, +2.6] | > +0.0 | 12 / 2 | confirmed |
