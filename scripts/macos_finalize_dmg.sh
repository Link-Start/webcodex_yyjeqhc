#!/usr/bin/env bash
# Probe native image services early; finalize/repack the actual signed app later.
set -euo pipefail
preflight=0
if [ "$#" -eq 1 ] && [ "$1" = --preflight ]; then
  preflight=1
elif [ "$#" -ne 3 ]; then
  echo "usage: $0 --preflight | <dmg> <adhoc|self-signed|developer-id> <identity>" >&2
  exit 2
else
  case "$2" in
    adhoc) exit 0 ;;
    self-signed|developer-id) ;;
    *) echo '[dmg] invalid signing mode' >&2; exit 2 ;;
  esac
  [ -f "$1" ] && [ ! -L "$1" ] || { echo '[dmg] expected a regular input DMG' >&2; exit 2; }
fi
scripts="$(cd "$(dirname "$0")" && pwd)"
diagnostics="${WEBCODEX_DMG_DIAGNOSTICS_DIR:-$scripts/../target/dmg-diagnostics}"
mkdir -p "$diagnostics"
log="$(mktemp "$diagnostics/operation.XXXXXX")"
scratch="$(mktemp -d "${TMPDIR:-/tmp}/webcodex-final-dmg.XXXXXX")"
mounted=0
replacement=''
stage=prepare
# Log stage identifiers, not commands or environment: signing argv can contain
# credentials. Full native tool diagnostics remain visible in the CI job log.
run_stage() {
  stage="$1"; shift
  printf '[dmg] stage=%s\n' "$stage" | tee -a "$log"
  "$@"
}
cleanup() {
  status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ]; then
    printf '[dmg] failed stage=%s exit=%s; native diagnostics are in the job log\n' "$stage" "$status" | tee -a "$log" >&2
    df -Pk "$scratch" >> "$log" 2>&1 || true
  fi
  cleanup_mount_ok=1
  if [ "$mounted" -eq 1 ] && ! hdiutil detach "$scratch/mount"; then
    cleanup_mount_ok=0
    printf '[dmg] cleanup detach failed; retained scratch=%s\n' "$scratch" | tee -a "$log" >&2
  fi
  if [ -n "$replacement" ]; then rm -f "$replacement"; fi
  # Never recursively remove a mount whose detach failed; retain its owned path
  # for diagnosis and preserve the original failure status over cleanup errors.
  if [ "$cleanup_mount_ok" -eq 1 ]; then rm -rf "$scratch"; fi
  printf '[dmg] diagnostics=%s\n' "$log"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$scratch/mount" "$scratch/payload"
create_image() {
  run_stage create hdiutil create -volname 'WebCodex Desktop' -srcfolder "$scratch/payload" -format UDZO "$scratch/final.dmg"
}
attach_image() {
  # Even a failed attach may leave a partial mount; cleanup owns this exact path.
  mounted=1
  run_stage attach hdiutil attach "$1" -readonly -nobrowse -mountpoint "$scratch/mount"
}
detach_image() {
  run_stage detach hdiutil detach "$scratch/mount"
  mounted=0
}
if [ "$preflight" -eq 1 ]; then
  printf 'disk image preflight\n' > "$scratch/payload/probe"
  create_image
  attach_image "$scratch/final.dmg"
  run_stage verify cmp "$scratch/payload/probe" "$scratch/mount/probe"
  detach_image
  printf '[dmg] native create/attach/read/detach preflight passed\n'
  exit 0
fi
dmg="$1"
attach_image "$dmg"
shopt -s nullglob
apps=("$scratch/mount"/*.app)
stage=inspect-app
if [ "${#apps[@]}" -ne 1 ]; then
  printf '[dmg] expected exactly one app, found %s\n' "${#apps[@]}" >&2
  exit 1
fi
app="$scratch/payload/$(basename "${apps[0]}")"
run_stage copy-app ditto "${apps[0]}" "$app"
detach_image
run_stage sign-app bash "$scripts/macos_finalize_desktop.sh" "$app" "$2" "$3"
ln -s /Applications "$scratch/payload/Applications"
create_image
# Preserve the original candidate on failed copy/disk-full. Only the completed
# replacement is renamed on the same filesystem; never partially overwrite it.
run_stage verify-image hdiutil verify "$scratch/final.dmg"
stage=replace
replacement="$(mktemp "$dmg.final.XXXXXX")"
run_stage copy-image cp "$scratch/final.dmg" "$replacement"
run_stage replace mv -f "$replacement" "$dmg"
replacement=''
printf '[dmg] final image replaced successfully\n'
