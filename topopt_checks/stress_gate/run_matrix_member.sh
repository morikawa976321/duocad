#!/usr/bin/env bash
# run_matrix.sh と同じ13条件で、境界域を判定しない統合ゲート(部材域の Q_{w³} だけ。境界域の球平均は記録のみ)を走らせる。
#   bash topopt_checks/stress_gate/run_matrix_member.sh <ログ>
set -u
LOG=${1:-matrix_member.log}
P=".venv/Scripts/python.exe topopt_checks/stress_gate/phase_b_search.py"
run() {  # model load h w
  $P --model "$1" --gate unified --load "$2" --h "$3" --w "$4" --d "$3" --dist nodes --refine 2 --tag mx >> "$LOG" 2>&1 \
    || echo "FAIL $*" >> "$LOG"
}
run dilution 300 2.0 4.0
run path 3000 2.0 6.0
run dilution 200 2.0 4.0
run dilution 250 2.0 4.0
run dilution 300 2.0 8.0
run dilution 300 1.5 4.0
run dilution 300 2.5 4.0
run dilution 300 3.0 4.0
run path 2700 2.0 6.0
run path 3300 2.0 6.0
run path 3000 2.0 4.0
run path 3000 2.0 8.0
run path 3000 1.5 6.0
run path 3000 2.5 6.0
run path 3000 3.0 6.0
echo MATRIX_DONE >> "$LOG"
