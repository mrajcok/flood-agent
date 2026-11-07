# Flood Agent eval report

| Model | Correct | Accuracy | Retries | Errors | Mean latency | Max latency | Total cost | Cost/question |
|---|---|---|---|---|---|---|---|---|
| z-ai/glm-5.3 | 13/33 | 39% | 10 | 0 | 31.2s | 332.2s | $0.7583 | $0.0230 |
| deepseek/deepseek-v4-pro-0813 | 14/33 | 42% | 9 | 0 | 10.3s | 37.3s | $0.2902 | $0.0088 |

## Per question

| Question | Model | Result | Retries | Latency | Cost | Note |
|---|---|---|---|---|---|---|
| total-flow-count | z-ai/glm-5.3 | FAIL | 0 | 2.2s | $0.0017 | no Result Table was created |
| total-bytes-and-packets | z-ai/glm-5.3 | pass | 0 | 4.9s | $0.0043 |  |
| flows-per-protocol | z-ai/glm-5.3 | pass | 0 | 5.3s | $0.0042 |  |
| top-dports-by-bytes | z-ai/glm-5.3 | pass | 0 | 5.4s | $0.0040 |  |
| distinct-circuits | z-ai/glm-5.3 | FAIL | 0 | 3.1s | $0.0018 | no Result Table was created |
| busiest-hour-of-day | z-ai/glm-5.3 | FAIL | 1 | 43.8s | $0.0317 | no Result Table was created |
| top-talkers-last-hour | z-ai/glm-5.3 | pass | 0 | 7.5s | $0.0083 |  |
| bps-last-hour | z-ai/glm-5.3 | pass | 1 | 7.5s | $0.0085 |  |
| pps-last-hour | z-ai/glm-5.3 | pass | 1 | 10.1s | $0.0124 |  |
| per-minute-bytes-syn-window | z-ai/glm-5.3 | pass | 0 | 14.1s | $0.0166 |  |
| syn-only-flows-jan-2 | z-ai/glm-5.3 | FAIL | 0 | 4.4s | $0.0033 | no Result Table was created |
| sources-in-10-1-slash-16 | z-ai/glm-5.3 | FAIL | 0 | 21.7s | $0.0044 | no Result Table was created |
| udp-total-bytes | z-ai/glm-5.3 | pass | 1 | 5.2s | $0.0057 |  |
| icmp-flow-count | z-ai/glm-5.3 | FAIL | 0 | 2.2s | $0.0017 | no Result Table was created |
| largest-flow | z-ai/glm-5.3 | pass | 0 | 4.4s | $0.0039 |  |
| avg-packets-per-tcp-flow | z-ai/glm-5.3 | pass | 1 | 5.6s | $0.0069 |  |
| syn-flood-victim | z-ai/glm-5.3 | pass | 0 | 8.7s | $0.0124 |  |
| syn-flood-flow-count | z-ai/glm-5.3 | FAIL | 0 | 6.3s | $0.0071 | no Result Table was created |
| syn-flood-distinct-sources | z-ai/glm-5.3 | FAIL | 0 | 12.1s | $0.0120 | no Result Table was created |
| syn-flood-circuit | z-ai/glm-5.3 | FAIL | 0 | 44.1s | $0.0497 | row count: expected 1, got 10 |
| syn-flood-bps | z-ai/glm-5.3 | FAIL | 1 | 332.2s | $0.2205 | row count: expected 1, got 10 |
| amp-victim | z-ai/glm-5.3 | pass | 0 | 19.7s | $0.0183 |  |
| amp-total-bytes | z-ai/glm-5.3 | FAIL | 1 | 17.1s | $0.0166 | values: a requested column has no matching column in the result |
| amp-reflector-count | z-ai/glm-5.3 | FAIL | 0 | 105.9s | $0.0730 | row count: expected 1, got 10 |
| amp-flows-per-source-port | z-ai/glm-5.3 | FAIL | 0 | 45.9s | $0.0290 | row count: expected 2, got 0 |
| amp-flow-count | z-ai/glm-5.3 | FAIL | 0 | 10.9s | $0.0079 | no Result Table was created |
| amp-circuit | z-ai/glm-5.3 | FAIL | 0 | 73.0s | $0.0657 | row count: expected 1, got 5 |
| busiest-hour-jan-4 | z-ai/glm-5.3 | FAIL | 2 | 154.2s | $0.0854 | no Result Table was created |
| scan-source | z-ai/glm-5.3 | pass | 0 | 13.4s | $0.0125 |  |
| scan-flow-count | z-ai/glm-5.3 | FAIL | 0 | 4.6s | $0.0034 | no Result Table was created |
| scan-distinct-targets | z-ai/glm-5.3 | FAIL | 0 | 7.6s | $0.0051 | no Result Table was created |
| scan-port-range | z-ai/glm-5.3 | FAIL | 0 | 10.8s | $0.0085 | values: a requested column has no matching column in the result |
| scan-avg-flow-size | z-ai/glm-5.3 | FAIL | 1 | 16.0s | $0.0120 | values: a requested column has no matching column in the result |
| total-flow-count | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 2.3s | $0.0007 | no Result Table was created |
| total-bytes-and-packets | deepseek/deepseek-v4-pro-0813 | pass | 0 | 5.0s | $0.0055 |  |
| flows-per-protocol | deepseek/deepseek-v4-pro-0813 | pass | 0 | 5.5s | $0.0059 |  |
| top-dports-by-bytes | deepseek/deepseek-v4-pro-0813 | pass | 0 | 7.5s | $0.0054 |  |
| distinct-circuits | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 2.5s | $0.0007 | no Result Table was created |
| busiest-hour-of-day | deepseek/deepseek-v4-pro-0813 | FAIL | 1 | 17.4s | $0.0089 | no Result Table was created |
| top-talkers-last-hour | deepseek/deepseek-v4-pro-0813 | pass | 0 | 4.2s | $0.0060 |  |
| bps-last-hour | deepseek/deepseek-v4-pro-0813 | pass | 1 | 21.0s | $0.0168 |  |
| pps-last-hour | deepseek/deepseek-v4-pro-0813 | pass | 1 | 37.3s | $0.0264 |  |
| per-minute-bytes-syn-window | deepseek/deepseek-v4-pro-0813 | pass | 0 | 12.7s | $0.0138 |  |
| syn-only-flows-jan-2 | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 5.1s | $0.0026 | no Result Table was created |
| sources-in-10-1-slash-16 | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 2.8s | $0.0010 | no Result Table was created |
| udp-total-bytes | deepseek/deepseek-v4-pro-0813 | pass | 1 | 6.5s | $0.0077 |  |
| icmp-flow-count | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 2.7s | $0.0008 | no Result Table was created |
| largest-flow | deepseek/deepseek-v4-pro-0813 | pass | 0 | 5.7s | $0.0053 |  |
| avg-packets-per-tcp-flow | deepseek/deepseek-v4-pro-0813 | pass | 1 | 6.6s | $0.0073 |  |
| syn-flood-victim | deepseek/deepseek-v4-pro-0813 | pass | 0 | 13.8s | $0.0141 |  |
| syn-flood-flow-count | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 8.7s | $0.0064 | no Result Table was created |
| syn-flood-distinct-sources | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 5.0s | $0.0031 | no Result Table was created |
| syn-flood-circuit | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 15.5s | $0.0091 | no Result Table was created |
| syn-flood-bps | deepseek/deepseek-v4-pro-0813 | FAIL | 1 | 11.2s | $0.0127 | values: a requested column has no matching column in the result |
| amp-victim | deepseek/deepseek-v4-pro-0813 | pass | 0 | 14.9s | $0.0145 |  |
| amp-total-bytes | deepseek/deepseek-v4-pro-0813 | FAIL | 1 | 32.6s | $0.0297 | values: a requested column has no matching column in the result |
| amp-reflector-count | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 13.1s | $0.0084 | no Result Table was created |
| amp-flows-per-source-port | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 19.8s | $0.0174 | row count: expected 2, got 0 |
| amp-flow-count | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 6.1s | $0.0045 | no Result Table was created |
| amp-circuit | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 5.7s | $0.0064 | row count: expected 1, got 0 |
| busiest-hour-jan-4 | deepseek/deepseek-v4-pro-0813 | pass | 1 | 22.5s | $0.0212 |  |
| scan-source | deepseek/deepseek-v4-pro-0813 | pass | 0 | 8.1s | $0.0107 |  |
| scan-flow-count | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 2.3s | $0.0009 | no Result Table was created |
| scan-distinct-targets | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 3.7s | $0.0018 | no Result Table was created |
| scan-port-range | deepseek/deepseek-v4-pro-0813 | FAIL | 0 | 4.1s | $0.0055 | values: a requested column has no matching column in the result |
| scan-avg-flow-size | deepseek/deepseek-v4-pro-0813 | FAIL | 1 | 8.5s | $0.0092 | values: a requested column has no matching column in the result |
