# Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | keel | 320 | 3 | 92.6 | [90.1, 94.9] | 92.6 | - | 1.065 | 0.00120 | 16287 |
| Haiku | keel+query | 320 | 3 | 95.8 | [93.6, 97.8] | 95.8 | - | 0.613 | 0.00067 | 13015 |
| Haiku | keel+query+days | 320 | 3 | 98.6 | [97.6, 99.6] | 98.6 | - | 0.635 | 0.00067 | 13982 |
| Haiku | sql | 320 | 3 | 97.4 | [95.8, 98.8] | 97.4 | - | 0.583 | 0.00062 | 6908 |
| Haiku | sql+days | 320 | 3 | 97.8 | [96.2, 99.2] | 97.8 | - | 0.556 | 0.00059 | 7119 |
| Haiku | sql-log | 320 | 3 | 94.5 | [92.5, 96.2] | 94.5 | - | 1.397 | 0.00154 | 20122 |
| Haiku | sql-log+days | 320 | 3 | 96.6 | [94.7, 98.2] | 96.6 | - | 1.136 | 0.00123 | 15084 |
| Haiku | sql-tables | 320 | 3 | 95.9 | [94.0, 97.7] | 95.9 | - | 0.787 | 0.00085 | 8601 |
| Haiku | sql-tables+days | 320 | 3 | 96.9 | [95.4, 98.2] | 96.9 | - | 0.762 | 0.00082 | 8601 |
| Haiku | sqlgen2 | 320 | 3 | 96.2 | [94.4, 97.9] | 96.2 | - | 0.860 | 0.00093 | 12183 |
| Haiku | sqlgen2+days | 320 | 3 | 98.2 | [97.1, 99.2] | 98.2 | - | 0.897 | 0.00095 | 12954 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 320 | -1.1 | [-3.1, +0.8] | > -2.0 | 10 / 16 | not confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 320 | -1.6 | [-3.8, +0.5] | > -2.0 | 12 / 23 | not confirmed |
| H3 | query adds to Keel's five tools | keel+query - keel | 320 | +3.2 | [+0.9, +5.7] | > +0.0 | 36 / 17 | confirmed |

## Exploratory differences (paired, questions both answered; 95% cluster interval, no decision rule)

| A - B | n | A - B (points) | 95% CI (cluster) | A better / B better |
| --- | ---: | ---: | --- | --- |
| sqlgen2+days - sql+days | 320 | +0.4 | [-0.6, +1.6] | 10 / 7 |
| keel+query+days - sql+days | 320 | +0.8 | [-0.1, +2.2] | 10 / 4 |
| sql+days - sql-log+days | 320 | +1.2 | [-0.2, +2.8] | 14 / 8 |
| sql+days - sql-tables+days | 320 | +0.9 | [-0.4, +2.3] | 14 / 7 |
| sql+days - sql | 320 | +0.4 | [-0.5, +1.3] | 10 / 7 |
| sql-tables+days - sql-tables | 320 | +0.9 | [-0.6, +2.6] | 22 / 14 |
| sql-log+days - sql-log | 320 | +2.1 | [+0.5, +3.5] | 27 / 11 |

