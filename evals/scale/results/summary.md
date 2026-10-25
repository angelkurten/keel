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
| per_subject:8 | memory | 100,000 | 3.7 | 27,224 | 3.7 | 26,685 | – | 1,731 | 0.20 |  |
| per_subject:8 | sqlite | 100,000 | 5.0 | 19,865 | 5.3 | 18,899 | 1,150 | 2,923 | 0.31 |  |
| per_subject:64 | memory | 100,000 | 12.3 | 8,146 | 11.3 | 8,811 | – | 2,123 | 0.24 |  |
| per_subject:64 | sqlite | 100,000 | 5.2 | 19,296 | 5.8 | 17,195 | 911 | 2,509 | 0.27 |  |
| per_subject:512 | memory | 100,000 | 72.1 | 1,387 | 101.7 | 984 | – | 9,975 | 0.97 |  |
| per_subject:512 | sqlite | 100,000 | 12.0 | 8,335 | 7.7 | 12,932 | 836 | 2,100 | 0.23 |  |
| per_subject:4096 | memory | 100,000 | 622.6 | 161 | 639.7 | 156 | – | 77,854 | 7.29 |  |
| per_subject:4096 | sqlite | 100,000 | 34.4 | 2,905 | 36.7 | 2,722 | 845 | 1,646 | 0.19 |  |

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
| context_now | 0.143 / 0.186 / 0.230 | 0.260 / 0.456 / 0.727 | – / – / – |
| context_as_of | 0.138 / 0.190 / 0.217 | 0.308 / 0.458 / 0.766 | – / – / – |
| context_known_at | 0.190 / 0.249 / 0.291 | 0.400 / 0.611 / 0.955 | – / – / – |
| context_missing | 0.011 / 0.019 / 0.033 | 0.048 / 0.115 / 0.218 | – / – / – |
| history_subject | 0.244 / 0.324 / 0.359 | 0.475 / 0.736 / 1.2 | – / – / – |
| related | 0.010 / 0.023 / 0.030 | 0.026 / 0.070 / 0.103 | – / – / – |
| facts_as_of | 0.007 / 0.010 / 0.012 | 0.054 / 0.105 / 0.157 | – / – / – |
| facts_known_at | 0.035 / 0.053 / 0.057 | 0.109 / 0.185 / 0.298 | – / – / – |
| history_tenant_window | 95.3 / 115.4 / 119.6 | 1,033 / 1,181 / 1,215 | – / – / – |
| search_rule_count | 87.6 / 129.1 / 136.2 | 755.9 / 897.1 / 917.5 | – / – / – |
| search_status_at | 80.3 / 103.2 / 108.5 | 784.1 / 875.2 / 881.0 | – / – / – |
| search_known_at_count | 352.6 / 507.0 / 530.6 | 1,150 / 1,511 / 1,656 | – / – / – |

Weighted benchmark mix, mean: memory: 28.67 ms, sqlite: 173.74 ms.

### per_subject:64

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 0.334 / 0.493 / 0.563 | 0.427 / 0.597 / 0.659 | – / – / – |
| context_as_of | 0.245 / 0.369 / 0.468 | 0.696 / 1.3 / 2.4 | – / – / – |
| context_known_at | 0.450 / 0.857 / 1.0 | 1.2 / 2.1 / 2.6 | – / – / – |
| context_missing | 0.012 / 0.021 / 0.033 | 0.050 / 0.084 / 0.138 | – / – / – |
| history_subject | 0.780 / 0.881 / 0.989 | 1.7 / 2.0 / 2.1 | – / – / – |
| related | 0.019 / 0.037 / 0.047 | 0.056 / 0.095 / 0.142 | – / – / – |
| facts_as_of | 0.037 / 0.057 / 0.063 | 0.183 / 0.413 / 0.511 | – / – / – |
| facts_known_at | 0.125 / 0.277 / 0.310 | 0.468 / 0.697 / 0.970 | – / – / – |
| history_tenant_window | 90.5 / 98.2 / 99.4 | 753.3 / 882.7 / 893.9 | – / – / – |
| search_rule_count | 51.1 / 70.1 / 72.1 | 585.1 / 654.3 / 658.7 | – / – / – |
| search_status_at | 49.7 / 62.2 / 63.9 | 541.5 / 685.6 / 733.3 | – / – / – |
| search_known_at_count | 232.9 / 298.5 / 303.2 | 862.3 / 922.6 / 925.3 | – / – / – |

Weighted benchmark mix, mean: memory: 19.95 ms, sqlite: 121.54 ms.

### per_subject:512

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 2.0 / 3.3 / 3.7 | 1.2 / 3.0 / 4.5 | – / – / – |
| context_as_of | 1.3 / 2.5 / 3.6 | 6.7 / 16.1 / 30.9 | – / – / – |
| context_known_at | 3.6 / 11.6 / 16.1 | 18.1 / 44.0 / 57.7 | – / – / – |
| context_missing | 0.015 / 0.027 / 0.038 | 0.089 / 0.199 / 0.254 | – / – / – |
| history_subject | 1.3 / 1.7 / 2.4 | 7.9 / 14.5 / 35.6 | – / – / – |
| related | 0.076 / 0.159 / 0.208 | 0.302 / 0.643 / 0.746 | – / – / – |
| facts_as_of | 0.222 / 0.310 / 0.349 | 1.4 / 3.5 / 6.4 | – / – / – |
| facts_known_at | 0.869 / 1.6 / 2.0 | 4.1 / 9.5 / 13.2 | – / – / – |
| history_tenant_window | 102.0 / 144.9 / 146.5 | 1,249 / 1,642 / 1,789 | – / – / – |
| search_rule_count | 47.9 / 81.7 / 89.7 | 759.5 / 955.3 / 966.1 | – / – / – |
| search_status_at | 43.2 / 55.7 / 58.6 | 727.6 / 1,237 / 1,306 | – / – / – |
| search_known_at_count | 231.5 / 311.0 / 330.1 | 1,233 / 2,132 / 2,222 | – / – / – |

Weighted benchmark mix, mean: memory: 21.93 ms, sqlite: 199.54 ms.

### per_subject:4096

| op | MemoryStore | SqliteStore | DuckDB |
| --- | --- | --- | --- |
| context_now | 41.0 / 48.3 / 48.8 | 2.5 / 3.3 / 6.3 | – / – / – |
| context_as_of | 24.3 / 44.6 / 47.4 | 131.6 / 281.1 / 461.5 | – / – / – |
| context_known_at | 74.2 / 241.5 / 270.7 | 377.0 / 918.7 / 1,143 | – / – / – |
| context_missing | 0.023 / 0.031 / 0.043 | 0.101 / 0.150 / 0.202 | – / – / – |
| history_subject | 6.3 / 8.5 / 11.7 | 52.9 / 92.2 / 135.8 | – / – / – |
| related | 0.104 / 0.167 / 0.508 | 0.484 / 0.658 / 1.0 | – / – / – |
| facts_as_of | 1.6 / 2.6 / 2.7 | 9.6 / 28.8 / 39.3 | – / – / – |
| facts_known_at | 5.6 / 14.7 / 15.5 | 26.4 / 51.1 / 95.2 | – / – / – |
| history_tenant_window | 201.0 / 296.5 / 300.3 | 1,439 / 2,263 / 2,706 | – / – / – |
| search_rule_count | 59.7 / 64.2 / 64.3 | 224.9 / 739.6 / 785.0 | – / – / – |
| search_status_at | 57.0 / 61.2 / 61.6 | 264.5 / 737.9 / 760.8 | – / – / – |
| search_known_at_count | 275.9 / 361.8 / 364.5 | 1,033 / 1,125 / 1,141 | – / – / – |

Weighted benchmark mix, mean: memory: 59.14 ms, sqlite: 267.30 ms.

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
