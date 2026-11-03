all # Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | keel | 320 | 3 | 92.6 | [90.1, 94.9] | 92.6 | - | 1.065 | 0.00120 | 16287 |
| Haiku | keel+query | 320 | 3 | 95.8 | [93.6, 97.8] | 95.8 | - | 0.613 | 0.00067 | 13015 |
| Haiku | keel+query+days | 320 | 3 | 98.6 | [97.6, 99.6] | 98.6 | - | 0.635 | 0.00067 | 13982 |
| Haiku | sql | 320 | 3 | 97.4 | [95.8, 98.8] | 97.4 | - | 0.583 | 0.00062 | 6908 |
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
| sqlgen2+days - sqlgen2 | 320 | +2.0 | [+0.5, +3.5] | 20 / 7 |
| keel+query+days - keel+query | 320 | +2.8 | [+1.1, +4.8] | 24 / 3 |

time_in # Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | keel | 64 | 3 | 86.5 | [80.2, 91.1] | 86.5 | - | 0.082 | 0.00049 | 5835 |
| Haiku | keel+query | 64 | 3 | 86.5 | [81.8, 90.6] | 86.5 | - | 0.087 | 0.00052 | 9078 |
| Haiku | keel+query+days | 64 | 3 | 99.0 | [97.4, 100.0] | 99.0 | - | 0.104 | 0.00055 | 13224 |
| Haiku | sql | 64 | 3 | 97.9 | [95.8, 99.5] | 97.9 | - | 0.124 | 0.00066 | 7550 |
| Haiku | sqlgen2 | 64 | 3 | 89.1 | [85.4, 92.7] | 89.1 | - | 0.163 | 0.00095 | 9381 |
| Haiku | sqlgen2+days | 64 | 3 | 98.4 | [96.9, 99.5] | 98.4 | - | 0.195 | 0.00103 | 14106 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 64 | -8.9 | [-13.0, -4.7] | > -2.0 | 0 / 12 | not confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 64 | -11.5 | [-16.7, -6.8] | > -2.0 | 2 / 18 | not confirmed |
| H3 | query adds to Keel's five tools | keel+query - keel | 64 | -0.0 | [-4.2, +4.7] | > +0.0 | 8 / 11 | not confirmed |

## Exploratory differences (paired, questions both answered; 95% cluster interval, no decision rule)

| A - B | n | A - B (points) | 95% CI (cluster) | A better / B better |
| --- | ---: | ---: | --- | --- |
| sqlgen2+days - sqlgen2 | 64 | +9.4 | [+5.2, +13.5] | 15 / 3 |
| keel+query+days - keel+query | 64 | +12.5 | [+8.3, +16.7] | 19 / 1 |

