#!/usr/bin/env bash
set -euo pipefail
RADAR_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RADAR_TCCLI="${RADAR_TCCLI:-$RADAR_ROOT/server/.venv/bin/tccli}"
# Official Tencent CLI. Read-only: no credential values, mutations or host restarts.
"$RADAR_TCCLI" lighthouse DescribeInstances --region ap-seoul --InstanceIds '["lhins-e5gcg722"]'
"$RADAR_TCCLI" lighthouse DescribeFirewallRules --region ap-seoul --InstanceId lhins-e5gcg722
"$RADAR_TCCLI" dnspod DescribeRecordList --Domain yswdra.cn
