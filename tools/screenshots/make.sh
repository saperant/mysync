#!/bin/sh
# The README's screenshots (docs/screenshots), from a made-up home in a sandbox:
#   tools/screenshots/make.sh [MYRCP_BUILD_DIR]
# The terminal ones need pyte (the test extra); the GTK ones a myrcp build with myrcp-gtk
# (cmake -DMYRCP_GTK=ON) given as the argument, Xvfb, xdotool and ImageMagick.
set -e
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(dirname "$(dirname "$HERE")")
OUT=$REPO/docs/screenshots; WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT" "$WORK/demo/run"; chmod 700 "$WORK/demo/run"
"$HERE/sandbox.sh" "$WORK/demo" sh "$HERE/demo_setup.sh"
"$HERE/sandbox.sh" "$WORK/demo" python "$HERE/rcp_shots.py" "$WORK"
cp "$WORK"/rcp-jobs.svg "$WORK"/rcp-plan.svg "$WORK"/rcp-selective-plan.svg "$WORK"/rcp-selection.svg "$OUT/"
if [ -n "$1" ]; then
  MYRCP_GTK_DIR=$1 "$HERE/sandbox.sh" "$WORK/demo" python "$HERE/gtk_shots.py" "$WORK" \
    "key:ctrl+p" "sleep:0.5" "type:perspective mysync" "sleep:0.6" "key:Return" "sleep:1.5" \
    "click:200,214" "sleep:1" "shot:gtk-jobs" \
    "click:200,172" "click:46,579" "sleep:4" "click:400,243" "sleep:1" "shot:gtk-plan" \
    "click:57,69" "sleep:1" "click:200,235" "click:46,579" "sleep:4" "shot:gtk-selective-plan" \
    "key:ctrl+p" "sleep:0.5" "type:selection (selective" "sleep:0.6" "key:Return" "sleep:4" \
    "click:234,579" "sleep:1.5" "shot:gtk-selection"
  for f in gtk-jobs gtk-plan gtk-selective-plan gtk-selection; do
    convert "$WORK/$f.png" -strip -colors 256 "PNG8:$OUT/$f.png"
  done
fi
ls -l "$OUT"
