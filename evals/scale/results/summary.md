# Keel scalability evaluation: results

Machine: Apple M5, 10 cores, 32.0 GB RAM, Darwin 27.0.0 (27.0); Python 3.13.14, SQLite 3.53.1, DuckDB 2.0.0; Keel 0.34.0 at e8ae81e70d81.

## Ingest throughput, replay time, store size

Ingest: the log read from .jsonl and applied in batches of 5,000 (`keel ingest`'s path), parsing included. Replay: `Ingestor.replay` of the measured tenant over the built graph (wipe, then rebuild). Disk: the SQLite file after a WAL checkpoint. RSS: resident memory after ingest minus before (the whole process: store, interpreter caches).

| log | store | events | ingest s | events/s | replay s | replay ev/s | disk B/event | RSS B/event | peak RSS GiB | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| real:hyperledger | memory | 18,267 | 0.5 | 33,791 | 0.5 | 35,162 | – | 3,165 | 0.09 |  |
| real:hyperledger | sqlite | 18,267 | 0.9 | 20,163 | 1.0 | 19,124 | 1,397 | 5,285 | 0.13 |  |
| real:mariadb | memory | 18,189 | 0.5 | 40,045 | 0.4 | 41,029 | – | 2,995 | 0.09 |  |
| real:mariadb | sqlite | 18,189 | 0.8 | 22,895 | 0.9 | 20,758 | 1,394 | 4,452 | 0.11 |  |
| real:spring | memory | 15,927 | 0.3 | 46,222 | 0.4 | 45,382 | – | 3,428 | 0.09 |  |
| real:spring | sqlite | 15,927 | 0.7 | 22,728 | 0.8 | 20,800 | 1,430 | 4,742 | 0.11 |  |
| syn:10000 | memory | 10,000 | 0.2 | 42,922 | 0.3 | 32,666 | – | 4,673 | 0.08 |  |
| syn:10000 | sqlite | 10,000 | 0.4 | 23,495 | 0.5 | 21,501 | 1,193 | 5,135 | 0.09 |  |
| syn:100000 | memory | 100,000 | 3.4 | 29,654 | 3.5 | 28,256 | – | 1,780 | 0.20 |  |
| syn:100000 | sqlite | 100,000 | 4.7 | 21,057 | 5.2 | 19,246 | 1,187 | 2,986 | 0.32 |  |
| syn:1000000 | memory | 1,000,000 | 64.2 | 15,566 | 67.9 | 14,730 | – | 1,466 | 1.40 |  |
| syn:1000000 | sqlite | 1,000,000 | 55.3 | 18,068 | 60.1 | 16,629 | 1,202 | 743 | 0.87 |  |
| syn:10000000 | memory | 10,000,000 | 1,007.5 | 9,926 | 1,092.1 | 9,157 | – | 1,169 | 11.32 |  |
| syn:10000000 | sqlite | 10,000,000 | 624.8 | 16,006 | 938.5 | 10,655 | 1,232 | 143 | 2.13 |  |
| tenants:1 | memory | 1,000,000 | 72.1 | 13,868 | 79.7 | 12,547 | – | 1,466 | 1.40 |  |
| tenants:1 | sqlite | 1,000,000 | 59.8 | 16,728 | 119.4 | 8,374 | 1,202 | 836 | 0.87 |  |
| tenants:10 | memory | 1,000,000 | 69.6 | 14,358 | 10.4 | 9,639 | – | 1,471 | 1.41 |  |
| tenants:10 | sqlite | 1,000,000 | 55.3 | 18,071 | 8.5 | 11,711 | 1,183 | 934 | 0.91 |  |
| tenants:100 | memory | 1,000,000 | 79.1 | 12,642 | 2.5 | 4,063 | – | 1,505 | 1.44 |  |
| tenants:100 | sqlite | 1,000,000 | 64.8 | 15,443 | 3.0 | 3,384 | 1,185 | 867 | 0.90 |  |
| per_subject:8 | memory | 100,000 | 4.3 | 23,490 | 4.2 | 23,674 | – | 2,335 | 0.26 |  |
| per_subject:8 | sqlite | 100,000 | 5.1 | 19,574 | 5.8 | 17,375 | 1,299 | 3,260 | 0.34 |  |
| per_subject:64 | memory | 100,000 | 3.0 | 33,592 | 3.3 | 30,699 | – | 1,653 | 0.19 |  |
| per_subject:64 | sqlite | 100,000 | 5.0 | 19,952 | 5.6 | 18,016 | 1,021 | 2,777 | 0.30 |  |
| per_subject:512 | memory | 100,000 | 3.0 | 33,890 | 3.0 | 33,144 | – | 1,627 | 0.19 |  |
| per_subject:512 | sqlite | 100,000 | 4.7 | 21,211 | 5.3 | 18,789 | 941 | 2,242 | 0.25 |  |
| per_subject:4096 | memory | 100,000 | 3.2 | 30,962 | 3.1 | 32,020 | – | 1,702 | 0.20 |  |
| per_subject:4096 | sqlite | 100,000 | 4.0 | 24,959 | 4.5 | 22,089 | 953 | 1,707 | 0.20 |  |

## Per-tool latency (ms): p50 / p95 / p99

In-process Tools calls (context, history, search, related) and store primitives (facts_*), interleaved in a seeded order. Subject ops: up to 200 calls each; tenant-wide ops: up to 10 (3 at 1e7), each op stopped once its calls used 60 s. DuckDB: the equivalent read on the bitemporal table (– where there is none).

### real:hyperledger

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.134 / 0.178 / 0.202 | 0.239 / 0.304 / 0.332 | 0.273 / 0.339 / 0.363 |
| context_as_of | 0.134 / 0.196 / 0.250 | 0.284 / 0.437 / 0.630 | 0.428 / 0.543 / 0.571 |
| context_known_at | 0.197 / 0.299 / 0.381 | 0.408 / 0.701 / 0.839 | 0.881 / 1.1 / 1.2 |
| context_missing | 0.011 / 0.016 / 0.024 | 0.046 / 0.065 / 0.081 | – / – / – |
| history_subject | 0.273 / 0.555 / 0.725 | 0.512 / 1.1 / 1.4 | 0.527 / 0.668 / 0.889 |
| related | 0.010 / 0.023 / 0.031 | 0.030 / 0.064 / 0.077 | – / – / – |
| facts_as_of | 0.007 / 0.014 / 0.015 | 0.046 / 0.086 / 0.153 | 0.431 / 0.545 / 0.695 |
| facts_known_at | 0.038 / 0.081 / 0.099 | 0.111 / 0.246 / 0.310 | 0.878 / 0.991 / 1.1 |
| history_tenant_window | 12.0 / 13.8 / 14.0 | 162.6 / 165.7 / 165.7 | 0.434 / 0.498 / 0.505 |
| search_rule_count | 15.9 / 21.3 / 21.6 | 107.5 / 149.5 / 149.7 | – / – / – |
| search_status_at | 15.2 / 19.3 / 21.0 | 90.5 / 129.2 / 134.0 | 0.492 / 0.633 / 0.650 |
| search_known_at_count | 67.4 / 73.1 / 73.3 | 200.1 / 209.7 / 210.5 | 1.7 / 1.9 / 2.0 |

Weighted benchmark mix, mean: memory: 4.97 ms, sqlite: 26.38 ms.

### real:mariadb

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.123 / 0.160 / 0.188 | 0.228 / 0.289 / 0.348 | – / – / – |
| context_as_of | 0.124 / 0.155 / 0.179 | 0.243 / 0.338 / 0.375 | – / – / – |
| context_known_at | 0.172 / 0.221 / 0.246 | 0.340 / 0.448 / 0.485 | – / – / – |
| context_missing | 0.010 / 0.016 / 0.021 | 0.047 / 0.068 / 0.090 | – / – / – |
| history_subject | 0.176 / 0.299 / 0.408 | 0.344 / 0.562 / 0.724 | – / – / – |
| related | 0.009 / 0.020 / 0.026 | 0.026 / 0.065 / 0.083 | – / – / – |
| facts_as_of | 0.005 / 0.009 / 0.011 | 0.033 / 0.064 / 0.112 | – / – / – |
| facts_known_at | 0.029 / 0.050 / 0.061 | 0.083 / 0.140 / 0.164 | – / – / – |
| history_tenant_window | 14.8 / 17.3 / 17.6 | 187.3 / 198.8 / 200.4 | – / – / – |
| search_rule_count | 21.5 / 25.3 / 25.5 | 124.7 / 166.8 / 167.6 | – / – / – |
| search_status_at | 16.1 / 23.4 / 25.5 | 146.4 / 175.0 / 175.0 | – / – / – |
| search_known_at_count | 40.2 / 77.5 / 78.3 | 198.6 / 233.7 / 235.4 | – / – / – |

Weighted benchmark mix, mean: memory: 4.52 ms, sqlite: 31.24 ms.

### real:spring

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.123 / 0.160 / 0.202 | 0.229 / 0.282 / 0.314 | – / – / – |
| context_as_of | 0.123 / 0.160 / 0.188 | 0.257 / 0.338 / 0.382 | – / – / – |
| context_known_at | 0.171 / 0.219 / 0.253 | 0.335 / 0.445 / 0.512 | – / – / – |
| context_missing | 0.011 / 0.019 / 0.023 | 0.046 / 0.069 / 0.086 | – / – / – |
| history_subject | 0.162 / 0.305 / 0.341 | 0.320 / 0.588 / 0.653 | – / – / – |
| related | 0.009 / 0.016 / 0.026 | 0.023 / 0.043 / 0.062 | – / – / – |
| facts_as_of | 0.005 / 0.008 / 0.011 | 0.040 / 0.062 / 0.082 | – / – / – |
| facts_known_at | 0.028 / 0.044 / 0.052 | 0.078 / 0.118 / 0.142 | – / – / – |
| history_tenant_window | 12.0 / 17.8 / 19.9 | 164.5 / 167.1 / 167.1 | – / – / – |
| search_rule_count | 18.7 / 22.4 / 23.2 | 132.1 / 159.0 / 165.9 | – / – / – |
| search_status_at | 15.3 / 18.6 / 18.8 | 122.2 / 147.9 / 151.5 | – / – / – |
| search_known_at_count | 31.9 / 71.2 / 73.8 | 174.9 / 208.3 / 209.8 | – / – / – |

Weighted benchmark mix, mean: memory: 4.21 ms, sqlite: 28.42 ms.

### syn:10000

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.137 / 0.177 / 0.208 | 0.245 / 0.312 / 0.347 | 0.220 / 0.302 / 0.459 |
| context_as_of | 0.131 / 0.168 / 0.312 | 0.291 / 0.368 / 0.399 | 0.332 / 0.401 / 0.501 |
| context_known_at | 0.174 / 0.229 / 0.256 | 0.348 / 0.440 / 0.463 | 0.693 / 0.868 / 1.2 |
| context_missing | 0.010 / 0.020 / 0.030 | 0.046 / 0.067 / 0.095 | – / – / – |
| history_subject | 0.194 / 0.296 / 0.348 | 0.372 / 0.547 / 0.585 | 0.477 / 0.602 / 0.782 |
| related | 0.008 / 0.016 / 0.024 | 0.023 / 0.040 / 0.050 | – / – / – |
| facts_as_of | 0.005 / 0.008 / 0.009 | 0.045 / 0.082 / 0.089 | 0.332 / 0.423 / 0.493 |
| facts_known_at | 0.029 / 0.045 / 0.052 | 0.088 / 0.123 / 0.135 | 0.679 / 0.781 / 0.832 |
| history_tenant_window | 8.2 / 9.4 / 9.8 | 97.7 / 105.5 / 106.4 | 0.446 / 0.485 / 0.495 |
| search_rule_count | 12.0 / 15.0 / 15.8 | 67.4 / 92.3 / 94.4 | – / – / – |
| search_status_at | 9.7 / 13.8 / 14.0 | 71.5 / 96.8 / 101.3 | 0.381 / 0.433 / 0.454 |
| search_known_at_count | 28.3 / 48.4 / 51.9 | 109.4 / 125.3 / 125.9 | 1.3 / 1.4 / 1.5 |

Weighted benchmark mix, mean: memory: 2.97 ms, sqlite: 16.67 ms.

### syn:100000

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.138 / 0.180 / 0.202 | 0.245 / 0.319 / 0.361 | 0.496 / 0.606 / 0.639 |
| context_as_of | 0.132 / 0.179 / 0.220 | 0.277 / 0.361 / 0.420 | 0.740 / 1.1 / 1.2 |
| context_known_at | 0.180 / 0.240 / 0.246 | 0.369 / 0.453 / 0.510 | 0.983 / 1.1 / 1.3 |
| context_missing | 0.011 / 0.020 / 0.034 | 0.046 / 0.070 / 0.112 | – / – / – |
| history_subject | 0.226 / 0.304 / 0.353 | 0.427 / 0.565 / 0.638 | 0.656 / 0.779 / 0.876 |
| related | 0.010 / 0.020 / 0.030 | 0.024 / 0.040 / 0.065 | – / – / – |
| facts_as_of | 0.007 / 0.009 / 0.010 | 0.046 / 0.079 / 0.088 | 0.800 / 1.1 / 1.2 |
| facts_known_at | 0.032 / 0.050 / 0.066 | 0.094 / 0.127 / 0.150 | 0.973 / 1.1 / 1.2 |
| history_tenant_window | 94.2 / 101.1 / 103.3 | 1,023 / 1,075 / 1,076 | 0.579 / 0.811 / 0.939 |
| search_rule_count | 90.3 / 177.6 / 202.8 | 797.3 / 896.5 / 915.0 | – / – / – |
| search_status_at | 87.8 / 149.5 / 180.7 | 785.2 / 864.1 / 864.9 | 0.973 / 1.3 / 1.4 |
| search_known_at_count | 347.6 / 409.2 / 423.6 | 1,118 / 1,203 / 1,227 | 3.1 / 4.0 / 4.1 |

Weighted benchmark mix, mean: memory: 28.86 ms, sqlite: 168.17 ms.

### syn:1000000

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.137 / 0.199 / 0.251 | 0.250 / 0.320 / 0.383 | 0.830 / 1.1 / 1.3 |
| context_as_of | 0.132 / 0.207 / 0.250 | 0.251 / 0.329 / 0.404 | 1.2 / 1.7 / 1.9 |
| context_known_at | 0.190 / 0.277 / 0.305 | 0.377 / 0.457 / 0.508 | 1.3 / 1.5 / 1.7 |
| context_missing | 0.011 / 0.024 / 0.072 | 0.048 / 0.073 / 0.126 | – / – / – |
| history_subject | 0.213 / 0.304 / 0.455 | 0.409 / 0.566 / 0.598 | 0.940 / 1.2 / 1.5 |
| related | 0.010 / 0.021 / 0.038 | 0.026 / 0.045 / 0.076 | – / – / – |
| facts_as_of | 0.007 / 0.012 / 0.027 | 0.034 / 0.064 / 0.085 | 1.2 / 1.7 / 2.0 |
| facts_known_at | 0.035 / 0.065 / 0.079 | 0.101 / 0.135 / 0.189 | 1.2 / 1.6 / 1.7 |
| history_tenant_window | 979.2 / 1,049 / 1,050 | 12,366 / 12,683 / 12,721 (n=5) | 1.1 / 1.3 / 1.3 |
| search_rule_count | 1,678 / 2,172 / 2,225 | 8,136 / 9,485 / 9,543 (n=8) | – / – / – |
| search_status_at | 926.8 / 1,531 / 1,835 | 7,787 / 8,605 / 8,606 (n=8) | 3.5 / 4.2 / 4.2 |
| search_known_at_count | 3,197 / 5,133 / 5,358 | 11,636 / 13,393 / 13,465 (n=5) | 19.0 / 24.2 / 25.4 |

Weighted benchmark mix, mean: memory: 318.33 ms, sqlite: 1,851.54 ms.

### syn:10000000

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.237 / 0.460 / 0.978 | 0.852 / 2.4 / 3.3 | 1.9 / 5.1 / 8.6 |
| context_as_of | 0.224 / 0.431 / 0.817 | 0.788 / 2.4 / 3.8 | 2.4 / 6.2 / 7.2 |
| context_known_at | 0.299 / 0.576 / 1.1 | 0.915 / 3.1 / 4.0 | 2.1 / 4.6 / 5.8 |
| context_missing | 0.021 / 0.067 / 0.216 | 0.052 / 0.072 / 0.133 | – / – / – |
| history_subject | 0.286 / 0.671 / 1.0 | 0.558 / 2.2 / 2.9 | 1.9 / 5.0 / 8.5 |
| related | 0.041 / 0.211 / 0.352 | 0.492 / 1.1 / 1.5 | – / – / – |
| facts_as_of | 0.045 / 0.111 / 0.196 | 0.055 / 1.2 / 1.5 | 2.5 / 6.0 / 8.0 |
| facts_known_at | 0.088 / 0.365 / 0.566 | 0.162 / 1.9 / 2.4 | 2.2 / 5.1 / 5.9 |
| history_tenant_window | 15,019 / 16,664 / 16,810 (n=3) | 232,525 / 232,525 / 232,525 (n=1) | 4.6 / 5.6 / 5.7 |
| search_rule_count | 18,481 / 18,657 / 18,673 (n=3) | 101,007 / 101,007 / 101,007 (n=1) | – / – / – |
| search_status_at | 15,054 / 39,333 / 41,492 (n=3) | 129,582 / 129,582 / 129,582 (n=1) | 25.7 / 42.0 / 45.4 |
| search_known_at_count | 37,103 / 44,941 / 45,638 (n=2) | 141,230 / 141,230 / 141,230 (n=1) | 191.4 / 269.6 / 280.2 |

Weighted benchmark mix, mean: memory: 4,355.22 ms, sqlite: 28,109.84 ms.

### tenants:1

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.149 / 0.260 / 0.358 | 0.309 / 0.465 / 0.578 | – / – / – |
| context_as_of | 0.138 / 0.258 / 0.562 | 0.301 / 0.460 / 0.583 | – / – / – |
| context_known_at | 0.203 / 0.336 / 0.481 | 0.466 / 0.695 / 0.740 | – / – / – |
| context_missing | 0.012 / 0.028 / 0.061 | 0.063 / 0.101 / 0.190 | – / – / – |
| history_subject | 0.226 / 0.329 / 0.361 | 0.477 / 0.858 / 0.924 | – / – / – |
| related | 0.011 / 0.028 / 0.051 | 0.034 / 0.056 / 0.091 | – / – / – |
| facts_as_of | 0.008 / 0.014 / 0.029 | 0.046 / 0.087 / 0.127 | – / – / – |
| facts_known_at | 0.037 / 0.075 / 0.094 | 0.117 / 0.200 / 0.252 | – / – / – |
| history_tenant_window | 1,125 / 1,374 / 1,434 | 14,130 / 14,531 / 14,545 (n=5) | – / – / – |
| search_rule_count | 1,982 / 2,460 / 2,479 | 9,449 / 14,671 / 14,982 (n=7) | – / – / – |
| search_status_at | 998.3 / 1,861 / 2,238 | 9,384 / 11,250 / 11,521 (n=7) | – / – / – |
| search_known_at_count | 3,338 / 5,834 / 6,002 | 13,102 / 14,665 / 14,716 (n=5) | – / – / – |

Weighted benchmark mix, mean: memory: 361.15 ms, sqlite: 2,183.28 ms.

### tenants:10

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.141 / 0.187 / 0.219 | 0.269 / 0.353 / 0.407 | – / – / – |
| context_as_of | 0.130 / 0.180 / 0.208 | 0.264 / 0.363 / 0.446 | – / – / – |
| context_known_at | 0.188 / 0.235 / 0.306 | 0.395 / 0.529 / 0.613 | – / – / – |
| context_missing | 0.011 / 0.021 / 0.040 | 0.051 / 0.091 / 0.127 | – / – / – |
| history_subject | 0.220 / 0.302 / 0.358 | 0.451 / 0.615 / 0.675 | – / – / – |
| related | 0.010 / 0.024 / 0.029 | 0.028 / 0.053 / 0.088 | – / – / – |
| facts_as_of | 0.007 / 0.010 / 0.013 | 0.037 / 0.065 / 0.096 | – / – / – |
| facts_known_at | 0.035 / 0.052 / 0.064 | 0.107 / 0.139 / 0.172 | – / – / – |
| history_tenant_window | 128.2 / 134.3 / 134.3 | 1,084 / 1,156 / 1,163 | – / – / – |
| search_rule_count | 97.5 / 138.7 / 142.8 | 840.6 / 960.4 / 960.9 | – / – / – |
| search_status_at | 88.6 / 102.7 / 104.4 | 851.7 / 982.1 / 982.3 | – / – / – |
| search_known_at_count | 310.1 / 416.8 / 418.7 | 1,233 / 1,386 / 1,389 | – / – / – |

Weighted benchmark mix, mean: memory: 29.52 ms, sqlite: 183.88 ms.

### tenants:100

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.142 / 0.192 / 0.217 | 0.254 / 0.343 / 0.486 | – / – / – |
| context_as_of | 0.134 / 0.169 / 0.208 | 0.254 / 0.350 / 0.404 | – / – / – |
| context_known_at | 0.184 / 0.239 / 0.261 | 0.382 / 0.526 / 0.742 | – / – / – |
| context_missing | 0.011 / 0.018 / 0.028 | 0.047 / 0.073 / 0.087 | – / – / – |
| history_subject | 0.211 / 0.290 / 0.310 | 0.418 / 0.584 / 0.660 | – / – / – |
| related | 0.011 / 0.021 / 0.030 | 0.026 / 0.050 / 0.096 | – / – / – |
| facts_as_of | 0.007 / 0.009 / 0.011 | 0.035 / 0.061 / 0.097 | – / – / – |
| facts_known_at | 0.035 / 0.055 / 0.062 | 0.103 / 0.142 / 0.239 | – / – / – |
| history_tenant_window | 10.3 / 13.3 / 13.8 | 95.8 / 99.2 / 99.9 | – / – / – |
| search_rule_count | 10.1 / 14.2 / 14.7 | 76.4 / 95.5 / 99.1 | – / – / – |
| search_status_at | 9.4 / 12.4 / 12.8 | 60.5 / 80.3 / 80.7 | – / – / – |
| search_known_at_count | 26.3 / 46.4 / 48.0 | 109.2 / 130.4 / 132.8 | – / – / – |

Weighted benchmark mix, mean: memory: 2.87 ms, sqlite: 16.41 ms.

### per_subject:8

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.143 / 0.189 / 0.231 | 0.254 / 0.324 / 0.408 | – / – / – |
| context_as_of | 0.137 / 0.180 / 0.220 | 0.301 / 0.403 / 0.461 | – / – / – |
| context_known_at | 0.188 / 0.243 / 0.315 | 0.388 / 0.495 / 0.600 | – / – / – |
| context_missing | 0.011 / 0.021 / 0.039 | 0.047 / 0.080 / 0.133 | – / – / – |
| history_subject | 0.241 / 0.330 / 0.352 | 0.462 / 0.619 / 0.661 | – / – / – |
| related | 0.010 / 0.023 / 0.037 | 0.025 / 0.050 / 0.090 | – / – / – |
| facts_as_of | 0.007 / 0.011 / 0.019 | 0.052 / 0.084 / 0.110 | – / – / – |
| facts_known_at | 0.036 / 0.056 / 0.069 | 0.105 / 0.146 / 0.181 | – / – / – |
| history_tenant_window | 100.8 / 105.5 / 105.7 | 1,054 / 1,075 / 1,083 | – / – / – |
| search_rule_count | 88.4 / 214.1 / 276.0 | 761.0 / 869.8 / 877.2 | – / – / – |
| search_status_at | 85.5 / 176.5 / 227.1 | 759.7 / 840.7 / 849.8 | – / – / – |
| search_known_at_count | 308.6 / 387.6 / 406.8 | 1,106 / 1,189 / 1,202 | – / – / – |

Weighted benchmark mix, mean: memory: 28.47 ms, sqlite: 166.48 ms.

### per_subject:64

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.326 / 0.455 / 0.556 | 0.422 / 0.599 / 0.699 | – / – / – |
| context_as_of | 0.246 / 0.372 / 0.463 | 0.700 / 1.3 / 1.5 | – / – / – |
| context_known_at | 0.447 / 0.833 / 1.0 | 1.3 / 2.2 / 3.0 | – / – / – |
| context_missing | 0.011 / 0.022 / 0.035 | 0.050 / 0.075 / 0.107 | – / – / – |
| history_subject | 0.771 / 0.850 / 0.870 | 1.7 / 2.1 / 2.5 | – / – / – |
| related | 0.017 / 0.033 / 0.043 | 0.057 / 0.095 / 0.117 | – / – / – |
| facts_as_of | 0.036 / 0.055 / 0.061 | 0.184 / 0.417 / 0.500 | – / – / – |
| facts_known_at | 0.122 / 0.279 / 0.321 | 0.471 / 0.698 / 0.863 | – / – / – |
| history_tenant_window | 81.5 / 86.0 / 86.4 | 751.4 / 829.2 / 832.2 | – / – / – |
| search_rule_count | 51.0 / 72.1 / 75.1 | 551.8 / 699.4 / 711.3 | – / – / – |
| search_status_at | 52.3 / 61.7 / 65.1 | 533.3 / 648.2 / 664.5 | – / – / – |
| search_known_at_count | 225.9 / 288.4 / 303.0 | 893.9 / 930.2 / 932.1 | – / – / – |

Weighted benchmark mix, mean: memory: 19.28 ms, sqlite: 121.68 ms.

### per_subject:512

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 2.0 / 3.3 / 3.7 | 0.980 / 1.2 / 1.3 | – / – / – |
| context_as_of | 1.3 / 2.5 / 3.2 | 5.0 / 10.1 / 11.8 | – / – / – |
| context_known_at | 3.5 / 10.9 / 14.5 | 12.7 / 28.8 / 36.6 | – / – / – |
| context_missing | 0.014 / 0.024 / 0.032 | 0.058 / 0.090 / 0.109 | – / – / – |
| history_subject | 1.3 / 1.5 / 1.6 | 6.2 / 6.9 / 7.1 | – / – / – |
| related | 0.065 / 0.125 / 0.130 | 0.236 / 0.309 / 0.341 | – / – / – |
| facts_as_of | 0.227 / 0.303 / 0.329 | 1.1 / 2.6 / 2.8 | – / – / – |
| facts_known_at | 0.861 / 1.6 / 1.7 | 3.1 / 3.8 / 3.9 | – / – / – |
| history_tenant_window | 96.2 / 102.1 / 103.7 | 860.1 / 904.5 / 904.7 | – / – / – |
| search_rule_count | 47.4 / 61.5 / 64.5 | 573.4 / 701.5 / 702.0 | – / – / – |
| search_status_at | 46.7 / 52.2 / 53.1 | 543.2 / 678.0 / 689.6 | – / – / – |
| search_known_at_count | 223.1 / 291.3 / 306.6 | 840.9 / 914.0 / 925.8 | – / – / – |

Weighted benchmark mix, mean: memory: 20.50 ms, sqlite: 128.58 ms.

### per_subject:4096

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 39.4 / 46.7 / 49.5 | 2.4 / 2.6 / 3.2 | – / – / – |
| context_as_of | 23.1 / 45.1 / 47.2 | 119.5 / 243.1 / 274.1 | – / – / – |
| context_known_at | 77.3 / 240.1 / 270.6 | 322.4 / 663.2 / 734.9 | – / – / – |
| context_missing | 0.024 / 0.033 / 0.044 | 0.092 / 0.133 / 0.148 | – / – / – |
| history_subject | 6.4 / 8.7 / 10.7 | 42.7 / 61.5 / 75.0 | – / – / – |
| related | 0.103 / 0.154 / 0.182 | 0.463 / 0.582 / 0.611 | – / – / – |
| facts_as_of | 1.6 / 2.6 / 2.7 | 8.3 / 22.5 / 27.1 | – / – / – |
| facts_known_at | 6.1 / 14.3 / 15.2 | 23.9 / 34.7 / 39.3 | – / – / – |
| history_tenant_window | 165.8 / 235.9 / 239.7 | 1,369 / 1,457 / 1,466 | – / – / – |
| search_rule_count | 56.8 / 62.6 / 63.1 | 217.1 / 729.0 / 744.9 | – / – / – |
| search_status_at | 54.1 / 59.8 / 61.4 | 227.4 / 717.1 / 722.4 | – / – / – |
| search_known_at_count | 271.8 / 354.5 / 355.3 | 938.4 / 1,029 / 1,041 | – / – / – |

Weighted benchmark mix, mean: memory: 56.66 ms, sqlite: 235.96 ms.

## Tenant-wide reads pushed down to the store (ms): p50 / p95

The same seeded tenant-wide ops, answered by the store's optional capabilities (facts_between, subjects_where, rule_subjects) instead of Tools' Python sweep; tests/test_pushdown.py and evals/jira/test_pushdown.py check the outputs are identical. SqliteStore: plain indexed SQL, rules still in Python. DuckdbStore and PostgresStore hold the rows Keel's SqliteStore ingest wrote, copied as stored (`one --load-from`). Copied in: pushdown:syn:1000000 DuckdbStore: 11.0 s; pushdown:syn:1000000 PostgresStore: 30.7 s; pushdown:syn:1000000 PostgresStore: 20.6 s; pushdown:syn:10000000 DuckdbStore: 143.2 s; pushdown:syn:10000000 PostgresStore: 233.6 s; pushdown:syn:1000000 PostgresStore: 22.1 s.

### syn:1000000

| op | MemoryStore, Python | SqliteStore, Python | SqliteStore, push-down | DuckdbStore, push-down | PostgresStore, push-down |
| --- | --- | --- | --- | --- | --- |
| history_tenant_window | 979.2 / 1,049 | 12,366 / 12,683 (n=5) | 11.4 / 15.4 | 25.8 / 28.8 | 13.5 / 16.8 |
| search_rule_count | 1,678 / 2,172 | 8,136 / 9,485 (n=8) | 6,528 / 9,391 (n=9) | 48.5 / 63.5 | 2,260 / 3,604 |
| search_status_at | 926.8 / 1,531 | 7,787 / 8,605 (n=8) | 194.0 / 362.6 | 26.3 / 32.4 | 223.1 / 372.6 |
| search_known_at_count | 3,197 / 5,133 | 11,636 / 13,393 (n=5) | 791.7 / 1,550 | 60.8 / 98.3 | 434.5 / 786.1 |

### syn:10000000

| op | MemoryStore, Python | SqliteStore, Python | SqliteStore, push-down | DuckdbStore, push-down | PostgresStore, push-down |
| --- | --- | --- | --- | --- | --- |
| history_tenant_window | 15,019 / 16,664 (n=3) | 232,525 / 232,525 (n=1) | 114.8 / 381.9 | 138.1 / 186.6 | 114.3 / 189.7 |
| search_rule_count | 18,481 / 18,657 (n=3) | 101,007 / 101,007 (n=1) | 117,049 / 117,049 (n=1) | 425.5 / 574.2 | 34,013 / 41,021 (n=2) |
| search_status_at | 15,054 / 39,333 (n=3) | 129,582 / 129,582 (n=1) | 16,695 / 28,144 (n=4) | 161.8 / 243.0 | 2,719 / 6,662 |
| search_known_at_count | 37,103 / 44,941 (n=2) | 141,230 / 141,230 (n=1) | 22,829 / 43,829 (n=3) | 438.3 / 815.2 | 13,133 / 18,180 (n=5) |

## DuckDB baseline: load and size

DuckDB reads the .jsonl, derives valid_to/closed_at with one window function and indexes (tenant, subject); facts only, no links, no rules, no dedupe, no incremental maintenance.

| log | events | DuckDB load s | rows | DuckDB B/event | SqliteStore ingest s | SqliteStore B/event |
| --- | --- | --- | --- | --- | --- | --- |
| real:hyperledger | 18,267 | 0.08 | 35,876 | 144 | 0.9 | 1,397 |
| syn:10000 | 10,000 | 0.03 | 21,634 | 159 | 0.4 | 1,193 |
| syn:100000 | 100,000 | 0.16 | 205,014 | 97 | 4.7 | 1,187 |
| syn:1000000 | 1,000,000 | 0.60 | 2,039,315 | 88 | 55.3 | 1,202 |
| syn:10000000 | 10,000,000 | 7.19 | 20,382,262 | 85 | 624.8 | 1,232 |

## Determinism: the log in written order and shuffled

Seeded shuffle of the whole log; graph hashed without the ingest clock. `same` per part across every build: syn:100000: {'entities': True, 'relations': False, 'facts': True}, replay rebuilds the same facts: True; syn:1000000: {'entities': True, 'relations': False, 'facts': True}, replay rebuilds the same facts: True

| log | build | facts | relations | facts hash | relations hash | relation rows differing | ingest s |
| --- | --- | --- | --- | --- | --- | --- | --- |
| syn:100000 | memory/written | 205,014 | 22,905 | a94b2cc47c69 | 0783dd40d4b2 | 0 | 5.2 |
| syn:100000 | memory/shuffled | 205,014 | 22,905 | a94b2cc47c69 | 97c09cf56bc5 | 152 | 6.2 |
| syn:100000 | sqlite/written | 205,014 | 22,905 | a94b2cc47c69 | 0783dd40d4b2 | 0 | 7.1 |
| syn:100000 | sqlite/shuffled | 205,014 | 22,905 | a94b2cc47c69 | 97c09cf56bc5 | 152 | 12.8 |
| syn:1000000 | memory/written | 2,039,315 | 228,041 | 9dd66b71517f | 60bcde768753 | 0 | 86.0 |
| syn:1000000 | memory/shuffled | 2,039,315 | 228,041 | 9dd66b71517f | 6ff757de1999 | 1,342 | 70.0 |
| syn:1000000 | sqlite/written | 2,039,315 | 228,041 | 9dd66b71517f | 60bcde768753 | 0 | 72.9 |
| syn:1000000 | sqlite/shuffled | 2,039,315 | 228,041 | 9dd66b71517f | 6ff757de1999 | 1,342 | 200.7 |

## Transport: in-process vs `keel serve` over HTTP (syn:100000, SqliteStore), p50 / p99 ms

| op | in-process | HTTP |
| --- | --- | --- |
| context_now | 0.290 / 0.501 | 0.967 / 1.7 |
| context_as_of | 0.339 / 0.585 | 1.0 / 2.0 |
| context_known_at | 0.407 / 0.603 | 1.1 / 2.1 |
| related | 0.029 / 0.107 | 0.634 / 1.3 |
| search_status_at | 791.9 / 1,000 | 746.1 / 991.0 |
| history_subject | 0.457 / 0.657 | 1.1 / 1.8 |
