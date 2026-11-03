# Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | keel | 258 | 3 | 94.8 | [88.5, 98.9] | 94.8 | 95.1 | 0.402 | 0.00055 | 7798 |
| Haiku | keel+query | 258 | 3 | 96.0 | [89.5, 100.0] | 95.2 | 99.3 | 0.325 | 0.00044 | 9502 |
| Haiku | sql | 258 | 3 | 95.7 | [89.3, 99.7] | 94.8 | 100.0 | 0.356 | 0.00048 | 5285 |
| Haiku | sqlgen2 | 258 | 3 | 96.0 | [89.4, 100.0] | 95.2 | 99.3 | 0.355 | 0.00048 | 6322 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 258 | +0.3 | [-0.3, +0.7] | > -2.0 | 3 / 1 | confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 258 | +0.3 | [-0.3, +0.7] | > -2.0 | 3 / 1 | confirmed |
| H3 | query adds to Keel's five tools | keel+query - keel | 258 | +1.2 | [+0.2, +2.5] | > +0.0 | 10 / 2 | confirmed |
