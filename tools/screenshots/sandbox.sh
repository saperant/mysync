#!/bin/sh
# Run a command where the demo home is /home/me and none of the real home directories are
# visible, except this checkout (and its .venv) and, if set, $MYRCP_GTK_DIR (a myrcp build
# with myrcp-gtk). Needs bubblewrap.  sandbox.sh DEMO_DIR command...
set -e
D=$(realpath "$1"); shift
REPO=$(realpath "$(dirname "$0")/../..")
HOMES=$(realpath /home)
mkdir -p "$D/home" "$D/run" "$D/disk" "$D/srv"
set -- --dev-bind / / --tmpfs "$HOMES" --bind "$REPO" "$REPO" \
  ${MYRCP_GTK_DIR:+--bind "$(realpath "$MYRCP_GTK_DIR")" "$(realpath "$MYRCP_GTK_DIR")"} \
  --tmpfs "$(realpath /mnt)" --bind "$D/disk" "$(realpath /mnt)/backup-disk" --bind "$D/srv" /srv \
  --bind "$D/home" "$HOMES/me" --tmpfs /run/user --bind "$D/run" /run/user/me \
  --setenv HOME /home/me --setenv USER me --setenv LOGNAME me \
  --setenv XDG_CONFIG_HOME /home/me/.config --setenv XDG_STATE_HOME /home/me/.local/state \
  --setenv XDG_RUNTIME_DIR /run/user/me --unsetenv MYSYNC_CONFIG_DIR --unsetenv MYSYNC_LOG_DIR \
  --setenv MYRCP_GTK_DIR "${MYRCP_GTK_DIR:+$(realpath "$MYRCP_GTK_DIR")}" \
  --setenv PATH "$REPO/.venv/bin:/usr/bin:/bin" --chdir /home/me "$@"
exec bwrap "$@"
