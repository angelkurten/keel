# Held-out set: results

Bootstrap B = 10000, seed 0; a question's repetitions averaged first.

## Accuracy, cost per correct answer and input tokens

| Agent | System | Questions | Reps | Accuracy | 95% CI (cluster) | One-issue | Tenant-wide | USD | cost per correct | Input tokens/run |
| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Haiku | api | 387 | 3 | 86.7 | [79.3, 92.7] | 92.5 | 61.6 | 0.879 | 0.00087 | 9918 |
| Haiku | keel | 387 | 3 | 95.9 | [93.8, 97.6] | 96.9 | 91.2 | 0.639 | 0.00057 | 8405 |
| Haiku | sql | 387 | 3 | 98.7 | [97.5, 99.6] | 98.4 | 100.0 | 0.536 | 0.00047 | 5278 |
| Haiku | sql-tables | 387 | 3 | 96.2 | [93.6, 98.3] | 97.6 | 90.3 | 0.812 | 0.00073 | 8179 |
| Haiku | sqlgen | 387 | 3 | 98.2 | [96.6, 99.5] | 98.7 | 95.8 | 0.850 | 0.00075 | 10126 |

## Planned comparisons (paired, questions both answered; Holm over the ten)

| Agent | A | B | n | A - B (points) | 95% CI (cluster) | p (cluster) | p (Holm) | 95% CI (iid) | A better / B better |
| --- | --- | --- | ---: | ---: | --- | ---: | ---: | --- | --- |
| Haiku | keel | sql | 387 | -2.8 | [-4.7, -1.4] | 0.0000 | 0.0000 | [-4.2, -1.6] | 5 / 29 |
| Haiku | keel | sqlgen | 387 | -2.3 | [-3.9, -1.0] | 0.0000 | 0.0000 | [-3.6, -1.1] | 5 / 27 |
| Haiku | keel | sql-tables | 387 | -0.3 | [-2.9, +2.2] | 0.7398 | 0.8120 | [-2.1, +1.5] | 18 / 25 |
| Haiku | keel | api | 387 | +9.1 | [+3.3, +16.4] | 0.0002 | 0.0006 | [+6.1, +12.2] | 70 / 22 |
| Haiku | sql | sqlgen | 387 | +0.5 | [-0.5, +1.8] | 0.4060 | 0.8120 | [-0.3, +1.5] | 8 / 6 |
