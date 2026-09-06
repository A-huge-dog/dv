#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dv_root="$(cd "${script_dir}/.." && pwd)"
v2_root="$(cd "${dv_root}/../../../.." && pwd)"
skill_root="${v2_root}/dv/soc-dv"
mode="${1:-q3}"
run_id="${2:-phase1-${mode}}"
workspace_root="${RSCU_WORKSPACE_ROOT:-${v2_root}/workspace}"
output_dir="${workspace_root}/dv/results/${run_id}"

if [[ -r /etc/profile.d/cadence-license.sh ]]; then
  source /etc/profile.d/cadence-license.sh
fi
export XCELIUM_HOME="${XCELIUM_HOME:-/arm/tools/cadence/xcelium/26.03_e065}"
export PATH="${XCELIUM_HOME}/bin:${XCELIUM_HOME}/tools/bin/64bit:${XCELIUM_HOME}/tools/bin:${PATH}"

case "${mode}" in
  compile)
    set +e
    /usr/bin/python3.12 "${skill_root}/scripts/run_eda.py" \
      --config "${dv_root}/xcelium.json" --mode compile \
      --workdir "${dv_root}" --output-dir "${output_dir}" --run-id "${run_id}"
    runner_status=$?
    set -e
    ;;
  q3)
    set +e
    /usr/bin/python3.12 "${skill_root}/scripts/run_eda.py" \
      --config "${dv_root}/xcelium.json" --mode test \
      --workdir "${dv_root}" --output-dir "${output_dir}" --run-id "${run_id}" \
      --test q3
    runner_status=$?
    set -e
    ;;
  --regression)
    set +e
    /usr/bin/python3.12 "${skill_root}/scripts/run_eda.py" \
      --config "${dv_root}/xcelium.json" --mode regression \
      --workdir "${dv_root}" --output-dir "${output_dir}" --run-id "${run_id}" \
      --regression phase1-directed --allow-regression
    runner_status=$?
    set -e
    ;;
  *)
    printf 'Usage: %s [compile|q3|--regression] [unique-run-id]\n' "$0" >&2
    exit 64
    ;;
esac

/usr/bin/python3.12 "${skill_root}/scripts/parse_result.py" \
  --run-record "${output_dir}/run-record.json" \
  --output "${output_dir}/run-result.json"

exit "${runner_status}"
