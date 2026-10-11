#!/bin/bash
# Development only (needs AutoCAD 2027): rebuilds tests/fixtures from tools/entities.scr.
# Draws one of every entity type we can script, then saves it in each DWG release plus DXF as ground truth.
cd "$(dirname "$0")/.."
out="$PWD/tests/fixtures"
work=$(mktemp -d)
settings=$'FILEDIA 0\nCMDDIA 0\nOSMODE 0\nATTDIA 0\nATTREQ 1'
current=""
n=0
# split the script into one file per step
awk -v dir="$work" '/^;;; /{name=$2; next} /^;/{next} name{print > (dir "/step_" name ".txt")}' tools/entities.scr
for name in $(grep '^;;; ' tools/entities.scr | cut -d' ' -f2); do
  n=$((n + 1))
  { echo "$settings"; cat "$work/step_$name.txt"; echo "SAVEAS 2018 $work/at_$n.dwg"; echo "QUIT Y"; } > "$work/$name.scr"
  ACAD_TIMEOUT=${STEP_TIMEOUT:-25} tools/acad.sh "$work/$name.scr" $current 2>/dev/null
  if [ -s "$work/at_$n.dwg" ]; then current="$work/at_$n.dwg"; else echo "step failed: $name (log: $work/$name.scr.log)"; fi
done
{
  echo "FILEDIA 0"
  echo "ZOOM E"
  for v in R14 2000 2004 2007 2010 2013 2018; do echo "SAVEAS $v $work/entities_${v#R}.dwg"; done
  echo "DXFOUT $work/entities.dxf 16"
  echo "QUIT Y"
} > "$work/save.scr"
tools/acad.sh "$work/save.scr" "$current" || exit 1
mkdir -p "$out"
cp "$work"/entities_*.dwg "$work/entities.dxf" "$out/"
ls -la "$out"
