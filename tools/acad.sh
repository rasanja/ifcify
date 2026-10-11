#!/bin/bash
# Development only: runs an AutoCAD script headlessly with the console bundled in AutoCAD 2027.
# usage: tools/acad.sh script.scr [input.dwg]     (log goes to script.scr.log)
C="/Applications/Autodesk/AutoCAD 2027/AutoCAD 2027.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole"
"$C" ${2:+/i "$2"} /s "$1" > "$1.log" 2>&1 &
pid=$!
for _ in $(seq ${ACAD_TIMEOUT:-180}); do kill -0 $pid 2>/dev/null || exit 0; sleep 1; done
kill -9 $pid; echo "AutoCAD timed out, see $1.log" >&2; exit 1  # a stuck console ignores a plain kill and spins a core
