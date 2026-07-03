#!/usr/bin/env bash
# Runs every module's embedded self-test. All must pass before a commit.
# (bridge/gate/engine tests need the live bridge; they are exercised by
#  `python3 -m executor.bridge` and `python3 -m executor.gate` separately.)
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m executor.store
python3 -m executor.analysis
python3 -m executor.strategies
python3 -m executor.backtester
python3 -m executor.risk
python3 -m executor.review
python3 -m executor.news_calendar
python3 -m executor.notify
echo "ALL EXECUTOR SELF-TESTS PASSED"
