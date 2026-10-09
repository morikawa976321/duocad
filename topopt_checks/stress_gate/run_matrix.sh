#!/usr/bin/env bash
# 小型2題材の15条件(05_SYNTHETIC と同じ荷重・幅・格子の振り)で、現行と統合ゲートを比べる。
# 統合ゲートの d は探索格子の h(拘束面に接する1層)。細分の確認は k=2 だけ。
#   bash topopt_checks/stress_gate/run_matrix.sh <ログ>
set -u
LOG=${1:-matrix.log}
P=".venv/Scripts/python.exe topopt_checks/stress_gate/phase_b_search.py"
run() {  # model load h w
  for g in current unified_zball; do
    extra=""
    [ "$g" = unified_zball ] && extra="--d $3 --dist nodes"
    $P --model "$1" --gate $g --load "$2" --h "$3" --w "$4" $extra --refine 2 --tag mx >> "$LOG" 2>&1 \
      || echo "FAIL $* $g" >> "$LOG"
  done
}
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
