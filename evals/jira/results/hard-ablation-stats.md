# Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | keel | 320 | 3 | 92.6 | [90.1, 94.9] | 92.6 | - | 1.065 | 0.00120 | 16287 |
| Haiku | keel+query | 320 | 3 | 95.8 | [93.6, 97.8] | 95.8 | - | 0.613 | 0.00067 | 13015 |
| Haiku | sql | 320 | 3 | 97.4 | [95.8, 98.8] | 97.4 | - | 0.583 | 0.00062 | 6908 |
| Haiku | sql-log | 320 | 3 | 94.5 | [92.5, 96.2] | 94.5 | - | 1.397 | 0.00154 | 20122 |
| Haiku | sql-tables | 320 | 3 | 95.9 | [94.0, 97.7] | 95.9 | - | 0.787 | 0.00085 | 8601 |
| Haiku | sqlgen2 | 320 | 3 | 96.2 | [94.4, 97.9] | 96.2 | - | 0.860 | 0.00093 | 12183 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 320 | -1.1 | [-3.1, +0.8] | > -2.0 | 10 / 16 | not confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 320 | -1.6 | [-3.8, +0.5] | > -2.0 | 12 / 23 | not confirmed |
| H3 | query adds to Keel's five tools | keel+query - keel | 320 | +3.2 | [+0.9, +5.7] | > +0.0 | 36 / 17 | confirmed |

## Exploratory differences (paired, questions both answered; 95% cluster interval, no decision rule)

| A - B | n | A - B (points) | 95% CI (cluster) | A better / B better |
| --- | ---: | ---: | --- | --- |
| sql-tables - sql | 320 | -1.5 | [-2.8, -0.2] | 7 / 22 |
| sql-log - sql | 320 | -2.9 | [-5.0, -1.0] | 11 / 29 |
| sqlgen2 - sql-log | 320 | +1.8 | [-0.3, +4.0] | 24 / 14 |
| keel+query - sql-log | 320 | +1.4 | [-0.8, +3.5] | 24 / 18 |
| keel - sql-log | 320 | -1.9 | [-4.5, +0.7] | 22 / 36 |
