# Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Sonnet | keel+query | 320 | 1 | 99.1 | [97.8, 100.0] | 99.1 | - | 2.980 | 0.00940 | 10170 |
| Sonnet | keel+query+days | 320 | 1 | 99.4 | [98.4, 100.0] | 99.4 | - | 3.036 | 0.00955 | 10712 |
| Sonnet | sql | 320 | 1 | 100.0 | [100.0, 100.0] | 100.0 | - | 2.907 | 0.00909 | 5531 |
| Sonnet | sql+days | 320 | 1 | 99.7 | [99.1, 100.0] | 99.7 | - | 2.887 | 0.00905 | 5778 |
| Sonnet | sql-log | 320 | 1 | 99.4 | [98.4, 100.0] | 99.4 | - | 4.115 | 0.01294 | 7507 |
| Sonnet | sql-log+days | 320 | 1 | 99.1 | [97.8, 100.0] | 99.1 | - | 4.342 | 0.01370 | 8219 |
| Sonnet | sqlgen2 | 320 | 1 | 99.1 | [97.8, 100.0] | 99.1 | - | 3.425 | 0.01080 | 7355 |
| Sonnet | sqlgen2+days | 320 | 1 | 99.4 | [98.4, 100.0] | 99.4 | - | 3.571 | 0.01123 | 8024 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 320 | -0.9 | [-2.2, +0.0] | > -2.0 | 0 / 3 | not confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 320 | -0.9 | [-2.2, +0.0] | > -2.0 | 0 / 3 | not confirmed |

## Exploratory differences (paired, questions both answered; 95% cluster interval, no decision rule)

| A - B | n | A - B (points) | 95% CI (cluster) | A better / B better |
| --- | ---: | ---: | --- | --- |
| sqlgen2+days - sql+days | 320 | -0.3 | [-0.9, +0.0] | 0 / 1 |
| keel+query+days - sql+days | 320 | -0.3 | [-0.9, +0.0] | 0 / 1 |
| sql+days - sql-log+days | 320 | +0.6 | [+0.0, +1.6] | 2 / 0 |
| keel+query+days - sql-log+days | 320 | +0.3 | [+0.0, +0.9] | 1 / 0 |
| sqlgen2+days - sql-log+days | 320 | +0.3 | [+0.0, +0.9] | 1 / 0 |
| sql+days - sql | 320 | -0.3 | [-0.9, +0.0] | 0 / 1 |
| sqlgen2+days - sqlgen2 | 320 | +0.3 | [+0.0, +0.9] | 1 / 0 |
| keel+query+days - keel+query | 320 | +0.3 | [+0.0, +0.9] | 1 / 0 |
| sql-log+days - sql-log | 320 | -0.3 | [-1.6, +0.6] | 1 / 2 |

