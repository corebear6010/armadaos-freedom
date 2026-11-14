#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SEED="$ROOT/system_files/usr/libexec/armada/steam-default-session"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

state="$tmp/state.toml"
legacy="$tmp/desktop-session"

cat >"$tmp/steamosctl" <<'STEAMOSCTL'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
    get-default-desktop-session)
        [[ ! -s "${ARMADA_TEST_STATE:?}" ]] || tail -1 "${ARMADA_TEST_STATE}"
        ;;
    set-default-desktop-session)
        printf '%s\n' "${2:?}" >"${ARMADA_TEST_STATE:?}"
        ;;
    *)
        exit 1
        ;;
esac
STEAMOSCTL
chmod +x "$tmp/steamosctl"

seed() {
    env ARMADA_STEAMOSCTL="$tmp/steamosctl" ARMADA_TEST_STATE="$state" \
        ARMADA_LEGACY_DESKTOP_SESSION="$legacy" "$@" "$SEED"
}

reset() {
    rm -f "$state" "$legacy"
}

# A fresh profile still holds upstream's raw plasma.desktop: seed Mobile.
reset
printf 'plasma.desktop\n' >"$state"
seed
[[ "$(<"$state")" == armada-plasma-mobile.desktop ]]

# No state at all is seeded the same way.
reset
seed
[[ "$(<"$state")" == armada-plasma-mobile.desktop ]]

# Both explicit armada choices survive repeated seeding.
for session in armada-plasma.desktop armada-plasma-mobile.desktop; do
    reset
    printf '%s\n' "$session" >"$state"
    seed
    seed
    [[ "$(<"$state")" == "$session" ]]
done

# The override picks either shell when nothing was persisted yet.
reset
seed ARMADA_DEFAULT_DESKTOP_SESSION=armada-plasma.desktop
[[ "$(<"$state")" == armada-plasma.desktop ]]
reset
seed ARMADA_DEFAULT_DESKTOP_SESSION=armada-plasma-mobile.desktop
[[ "$(<"$state")" == armada-plasma-mobile.desktop ]]

# A legacy explicit preference still wins over the override.
reset
printf 'desktop\n' >"$legacy"
seed ARMADA_DEFAULT_DESKTOP_SESSION=armada-plasma-mobile.desktop
[[ "$(<"$state")" == armada-plasma.desktop ]]
reset
printf 'mobile\n' >"$legacy"
seed ARMADA_DEFAULT_DESKTOP_SESSION=armada-plasma.desktop
[[ "$(<"$state")" == armada-plasma-mobile.desktop ]]

echo 'desktop default seeding tests passed'
