# Confirmation set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Sonnet | keel+query | 320 | 1 | 99.1 | [97.8, 100.0] | 99.1 | - | 2.980 | 0.00940 | 10170 |
| Sonnet | sql | 320 | 1 | 100.0 | [100.0, 100.0] | 100.0 | - | 2.907 | 0.00909 | 5531 |
| Sonnet | sql-log | 320 | 1 | 99.4 | [98.4, 100.0] | 99.4 | - | 4.115 | 0.01294 | 7507 |
| Sonnet | sqlgen2 | 320 | 1 | 99.1 | [97.8, 100.0] | 99.1 | - | 3.425 | 0.01080 | 7355 |

## Hypotheses (paired, questions both answered; 95% cluster interval, no adjustment)

| | Claim | A - B | n | A - B (points) | 95% CI (cluster) | bound | A better / B better | Result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| H1 | generated SQL is not worse than hand-written SQL | sqlgen2 - sql | 320 | -0.9 | [-2.2, +0.0] | > -2.0 | 0 / 3 | not confirmed |
| H2 | Keel with query is not worse than hand-written SQL | keel+query - sql | 320 | -0.9 | [-2.2, +0.0] | > -2.0 | 0 / 3 | not confirmed |

## Exploratory differences (paired, questions both answered; 95% cluster interval, no decision rule)

| A - B | n | A - B (points) | 95% CI (cluster) | A better / B better |
| --- | ---: | ---: | --- | --- |
| sqlgen2 - sql-log | 320 | -0.3 | [-1.2, +0.6] | 1 / 2 |
| keel+query - sql-log | 320 | -0.3 | [-1.6, +0.6] | 1 / 2 |
| sql-log - sql | 320 | -0.6 | [-1.6, +0.0] | 0 / 2 |
