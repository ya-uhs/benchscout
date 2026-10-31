#!/bin/sh
# Build with the open Gowin flow (yosys + nextpnr-gowin + apicula).
# Tools: pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula  (or oss-cad-suite)
# Set TOOLS=<venv>/bin if they are not on PATH.
set -e
cd "$(dirname "$0")"
T=${TOOLS:+$TOOLS/}
mkdir -p build
${T}yowasp-yosys -q -l build/yosys.log -p "read_verilog top.v; synth_gowin -top top -json build/top.json"
${T}yowasp-nextpnr-himbaechel-gowin -q -l build/pnr.log --json build/top.json --write build/pnr.json \
    --device GW1NR-LV9QN88PC6/I5 --vopt family=GW1N-9C --vopt cst=tangnano9k.cst --freq 27
${T}gowin_pack -d GW1N-9C -o build/tangnano9k-testbit.fs build/pnr.json
echo "built build/tangnano9k-testbit.fs"
# Load (later, not while benchscout holds the board):
#   openFPGALoader -b tangnano9k build/tangnano9k-testbit.fs        # SRAM, lost on power cycle
#   openFPGALoader -b tangnano9k -f build/tangnano9k-testbit.fs     # internal flash
