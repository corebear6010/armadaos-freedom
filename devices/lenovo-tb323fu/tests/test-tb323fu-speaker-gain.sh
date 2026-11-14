#!/bin/sh
# SPDX-License-Identifier: MIT
# Black-box behavior test for the tb323fu speaker-gain script (source under test).
#
# Usage: test-tb323fu-speaker-gain.sh ABS_SOURCE ABS_OUTDIR
#   ABS_SOURCE : absolute path to the script under test (production helper)
#   ABS_OUTDIR : absolute path to a FRESH output directory (must not exist)
#
# The test writes all its new files only inside ABS_OUTDIR (fixtures/ bin/
# mocks, logs/). It never deletes existing files and never scans outside
# ABS_OUTDIR, apart from the single explicit /etc guard described below.
# Every external tool the source uses is mocked inside
# ABS_OUTDIR/bin, so no real audio server / device / network / service is
# touched. pw-cli and pw-metadata are mocked to rc 127 (the original
# missing-dependency case).
#
# The model gate reads a NUL-delimited device-tree fixture and the card lookup
# reads <sound class>/card*/id, both files under ABS_OUTDIR. Only the mocked
# `cat` sees the fixture card id; every other cat is delegated to the real one.
# The production helper sources the fixed path /etc/tb323fu/audio.conf when it
# exists and has no environment override for it, so the test refuses to run on a
# host that has that file instead of silently testing the host's values.
#
# SCENARIO selects the mock behavior; the source is always invoked as
#   /bin/sh SOURCE on|off|status
# Expectations assert external behavior only: source rc, the two explicit
# amixer cset values, whether set-default was called (and its target), and the
# number of waits. Any failed check makes the final exit status 1.
#
# Scenarios:
#   protected_ok       default already protected, node present      -> rc0 21/21, set0, list1 wait0
#   empty              empty default, node present                  -> rc0 21/21, set1 ->prot
#   raw                *HiFi__Speaker__sink default                 -> rc0 21/21, set1 ->prot
#   old                effect_input.speaker_plus* default           -> rc0 21/21, set1 ->prot
#   headphone          headphone default preserved, node present    -> rc0 21/21, set0
#   bluetooth          bluetooth default preserved, node present    -> rc0 21/21, set0
#   protected_missing  node never appears                           -> rc1 159/159, list21 wait20
#   near_name          only effect_input.speakers.extra present     -> rc1 159/159, list21 wait20
#   list_rc_nonzero    list rc!=0 though stdout forged exact line   -> rc1 159/159, list21 wait20
#   get_rc_nonzero     get-default rc!=0, stdout looks valid        -> rc1 159/159, no set, no 21
#   set_fail           needed set-default rc!=0                     -> rc1 159/159, set1, no 21
#   delayed_3rd        node on 3rd list call                        -> rc0 21/21, list3 wait2
#   off_all_fail       off, every pactl call fails                  -> rc0 159/159, zero pactl calls
#   card_delayed_3rd   sound card absent on probes 1-2, present(0)  -> rc0 21/21, probe3 wait2
#   card_missing       sound card never appears                     -> rc!=0, no amixer, <=20 waits
#   card_sleep_fail    sound card absent and the wait itself fails  -> rc!=0, no amixer
#   gen4               compatible is lenovo,baldur-gen4 alone       -> rc0, no pactl/amixer/probe
#   gen4card           card id "LENOVO Gen4" (similar, not exact)   -> rc!=0, no amixer, waits
#   simcard            card id "LENOVOTB323FU-extra" (not exact)    -> rc!=0, no amixer, waits
# The last three pin the exact model token and the exact LENOVOTB323FU card id:
# neither a longer device-tree token nor a similar card id may reach the amps.
# The card cases pin the sound-card RETRY contract: a missing card is waited
# out (bounded, 0.5 s steps) and never faked as success. The old behavior
# "first probe missing -> exit 0, amps untouched" fails card_delayed_3rd.

if [ $# -ne 2 ]; then
	echo "usage: $0 ABS_SOURCE ABS_OUTDIR" >&2
	exit 2
fi
SRC=$1
OUT=$2
case $SRC in /*) ;; *) echo "source must be an absolute path: $SRC" >&2; exit 2 ;; esac
case $OUT in /*) ;; *) echo "outdir must be an absolute path: $OUT" >&2; exit 2 ;; esac
[ -f "$SRC" ] && [ -r "$SRC" ] || { echo "source is not a readable file: $SRC" >&2; exit 2; }
if [ -e "$OUT" ]; then echo "outdir already exists (refusing): $OUT" >&2; exit 2; fi

# The source reads /etc/tb323fu/audio.conf from a fixed path with no override.
# It is absent in the container; refuse a host where it would be sourced.
if [ -e /etc/tb323fu/audio.conf ]; then
	echo "refusing: /etc/tb323fu/audio.conf exists and would be sourced" >&2
	exit 2
fi

BIN=$OUT/bin
LOGS=$OUT/logs
mkdir -p "$BIN" "$LOGS" || exit 2

# ---- fixtures (all new files live under $OUT) ------------------------------

MODEL=$OUT/model
COMPAT=$MODEL/compatible
COMPAT_GEN4=$MODEL/compatible-gen4
SOUNDCLASS=$OUT/sound
mkdir -p "$MODEL" "$SOUNDCLASS/card0" || exit 2
printf 'lenovo,baldur\000qcom,kaanapali\000' > "$COMPAT" || exit 2
printf 'lenovo,baldur-gen4\000qcom,kaanapali-gen4\000' > "$COMPAT_GEN4" || exit 2
printf 'LENOVOTB323FU' > "$SOUNDCLASS/card0/id" || exit 2

# ---- mocks (all new files live under $OUT) ---------------------------------

cat > "$BIN/pactl" <<'EOF' || exit 2
#!/bin/sh
cd="${CASE_DIR:?CASE_DIR unset}"
printf 'pactl %s\n' "$*" >> "$cd/cmd.log"
case "${SCENARIO:-}" in off_all_fail) exit 1 ;; esac
inc() { n=$(cat "$cd/$1" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$cd/$1"; echo "$n"; }
sub=$1
case "$sub" in
get-default-sink)
	case "${SCENARIO:-}" in
	get_rc_nonzero) printf 'effect_input.speakers\n'; exit 1 ;;
	empty|set_fail) exit 0 ;;
	raw) printf 'alsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink\n'; exit 0 ;;
	old) printf 'effect_input.speaker_plus\n'; exit 0 ;;
	headphone) printf 'alsa_output.pci-0000_00_1f.3.HiFi__Headphone__sink\n'; exit 0 ;;
	bluetooth) printf 'bluez_output.AC_12_2F_00_11_22.1\n'; exit 0 ;;
	*) printf 'effect_input.speakers\n'; exit 0 ;;
	esac ;;
set-default-sink)
	n=$(inc set.count)
	printf '%s\n' "$2" > "$cd/set.target.$n"
	case "${SCENARIO:-}" in set_fail) exit 1 ;; esac
	exit 0 ;;
list)
	[ $# -eq 3 ] && [ "$2" = short ] && [ "$3" = sinks ] || exit 2
	n=$(inc list.count)
	case "${SCENARIO:-}" in
	list_rc_nonzero)
		printf '42\teffect_input.speakers\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED\n'
		exit 1 ;;
	near_name)
		printf '42\teffect_input.speakers.extra\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED\n'
		printf '43\tother_sink\tPipeWire\teffect_input.speakers\tSUSPENDED\n'
		exit 0 ;;
	protected_missing)
		printf '42\talsa_output.pci-0000_00_1f.3.HiFi__Headphone__sink\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n'
		exit 0 ;;
	delayed_3rd)
		if [ "$n" -ge 3 ]; then
			printf '42\teffect_input.speakers\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED\n'
		else
			printf '42\talsa_output.pci-0000_00_1f.3.HiFi__Headphone__sink\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n'
		fi
		exit 0 ;;
	*)
		printf '42\teffect_input.speakers\tPipeWire\tfloat32le 2ch 48000Hz\tSUSPENDED\n'
		printf '43\talsa_output.pci-0000_00_1f.3.HiFi__Headphone__sink\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n'
		exit 0 ;;
	esac ;;
*) exit 0 ;;
esac
EOF

cat > "$BIN/amixer" <<'EOF' || exit 2
#!/bin/sh
cd="${CASE_DIR:?CASE_DIR unset}"
printf 'amixer %s\n' "$*" >> "$cd/cmd.log"
exit 0
EOF

cat > "$BIN/sleep" <<'EOF' || exit 2
#!/bin/sh
cd="${CASE_DIR:?CASE_DIR unset}"
printf 'sleep %s\n' "$*" >> "$cd/cmd.log"
n=$(cat "$cd/sleep.count" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$cd/sleep.count"
case "${SCENARIO:-}" in card_sleep_fail) exit 1 ;; esac
exit 0
EOF

cat > "$BIN/cat" <<'EOF' || exit 2
#!/bin/sh
cd="${CASE_DIR:?CASE_DIR unset}"
fixture="${CARDFIXTURE:?CARDFIXTURE unset}"
for arg in "$@"; do
	case "$arg" in
	"$fixture"/card*/id)
		n=$(/usr/bin/cat "$cd/card.count" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$cd/card.count"
		printf 'card.query %s\n' "$n" >> "$cd/cmd.log"
		case "${SCENARIO:-}" in
		card_missing | card_sleep_fail) exit 0 ;;
		card_delayed_3rd) [ "$n" -ge 3 ] && printf 'LENOVOTB323FU'; exit 0 ;;
		gen4card) printf 'LENOVO Gen4'; exit 0 ;;
		simcard) printf 'LENOVOTB323FU-extra'; exit 0 ;;
		*) printf 'LENOVOTB323FU'; exit 0 ;;
		esac ;;
	esac
done
exec /usr/bin/cat "$@"
EOF

cat > "$BIN/pw-cli" <<'EOF' || exit 2
#!/bin/sh
exit 127
EOF

cat > "$BIN/pw-metadata" <<'EOF' || exit 2
#!/bin/sh
exit 127
EOF

chmod +x "$BIN/pactl" "$BIN/amixer" "$BIN/sleep" "$BIN/cat" "$BIN/pw-cli" "$BIN/pw-metadata" || exit 2

# ---- assertion helpers -----------------------------------------------------

FAILS=0
TOTAL=0
pass() { TOTAL=$((TOTAL+1)); printf 'PASS %s\n' "$1"; }
fail() { TOTAL=$((TOTAL+1)); FAILS=$((FAILS+1)); printf 'FAIL %s :: expected=[%s] actual=[%s]\n' "$1" "$2" "$3"; }
eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "$2" "$3"; fi; }

nonzero() { # name value -- pass only when value is an integer other than 0
	case $2 in ''|*[!0-9]*) fail "$1" "non-zero" "$2"; return 0 ;; esac
	if [ "$2" -ne 0 ]; then pass "$1"; else fail "$1" "non-zero" "$2"; fi
}

range() { # name lo hi value -- pass when lo <= value <= hi
	case $4 in ''|*[!0-9]*) fail "$1" "$2..$3" "$4"; return 0 ;; esac
	if [ "$4" -ge "$2" ] && [ "$4" -le "$3" ]; then
		pass "$1"
	else
		fail "$1" "$2..$3" "$4"
	fi
}

waits05() {
	a=$(grep -c '^sleep ' "$CD/cmd.log" 2>/dev/null); [ -n "$a" ] || a=0
	b=$(grep -c '^sleep 0\.5$' "$CD/cmd.log" 2>/dev/null); [ -n "$b" ] || b=0
	eq "$cname all waits are 0.5" "$a" "$b"
}

never21() {
	if [ -n "$(grep -E 'cset name=aw_dev_[01]_volume 21$' "$CD/cmd.log" 2>/dev/null)" ]; then
		fail "$cname never opens to 21" "no 21" "found 21"
	else
		pass "$cname never opens to 21"
	fi
}

expect() { # rc v0 v1 setn [listn] [sleepn]
	eq "$cname rc" "$1" "$RC"
	eq "$cname aw_dev_0_volume" "$2" "$V0"
	eq "$cname aw_dev_1_volume" "$3" "$V1"
	eq "$cname set-default count" "$4" "$SETN"
	[ $# -ge 5 ] && eq "$cname list count" "$5" "$LISTN"
	[ $# -ge 6 ] && eq "$cname sleep count" "$6" "$SLEEPN"
	return 0
}

run_case() { # name scenario args...
	cname=$1
	scen=$2
	shift 2
	CD=$LOGS/$cname
	mkdir -p "$CD" || exit 2
	: > "$CD/cmd.log"
	case $scen in gen4) compat=$COMPAT_GEN4 ;; *) compat=$COMPAT ;; esac
	PATH="$BIN:$PATH" SCENARIO=$scen CASE_DIR=$CD \
		TB323FU_DT_COMPATIBLE=$compat TB323FU_SOUND_CLASS=$SOUNDCLASS \
		CARDFIXTURE=$SOUNDCLASS \
		/bin/sh "$SRC" "$@" > "$CD/stdout.log" 2> "$CD/stderr.log"
	RC=$?
	SETN=$(cat "$CD/set.count" 2>/dev/null || echo 0)
	LISTN=$(cat "$CD/list.count" 2>/dev/null || echo 0)
	SLEEPN=$(cat "$CD/sleep.count" 2>/dev/null || echo 0)
	V0=$(grep 'cset name=aw_dev_0_volume ' "$CD/cmd.log" 2>/dev/null | tail -n 1 | sed 's/.* //')
	V1=$(grep 'cset name=aw_dev_1_volume ' "$CD/cmd.log" 2>/dev/null | tail -n 1 | sed 's/.* //')
	SETTARGET=$(cat "$CD/set.target.1" 2>/dev/null || echo "")
	PACTLTOTAL=$(grep -c '^pactl ' "$CD/cmd.log" 2>/dev/null); [ -n "$PACTLTOTAL" ] || PACTLTOTAL=0
	AMIXTOTAL=$(grep -c '^amixer ' "$CD/cmd.log" 2>/dev/null); [ -n "$AMIXTOTAL" ] || AMIXTOTAL=0
	CARDQ=$(cat "$CD/card.count" 2>/dev/null || echo 0)
	printf '\n== case %s (scenario=%s args=%s) rc=%s ==\n' "$cname" "$scen" "$*" "$RC"
	printf -- '-- %s stdout --\n' "$cname"; sed 's/^/  /' "$CD/stdout.log"
	printf -- '-- %s stderr --\n' "$cname"; sed 's/^/  /' "$CD/stderr.log"
	printf -- '-- %s cmd.log --\n' "$cname"; sed 's/^/  /' "$CD/cmd.log"
}

# ---- cases -----------------------------------------------------------------

# 1 protected already default and node present: no set-default, open to 21
run_case protected_ok protected_ok on
expect 0 21 21 0 1 0
waits05

# 2 empty default -> set default once to the protected sink
run_case empty empty on
expect 0 21 21 1 1 0
eq "$cname set-default target" effect_input.speakers "$SETTARGET"
waits05

# 3 raw *HiFi__Speaker__sink default -> set default once
run_case raw raw on
expect 0 21 21 1 1 0
eq "$cname set-default target" effect_input.speakers "$SETTARGET"

# 4 old effect_input.speaker_plus* default -> set default once
run_case old old on
expect 0 21 21 1 1 0
eq "$cname set-default target" effect_input.speakers "$SETTARGET"

# 5 headphone default preserved, node present -> open to 21, no set-default
run_case headphone headphone on
expect 0 21 21 0 1 0

# 6 bluetooth default preserved, node present -> open to 21, no set-default
run_case bluetooth bluetooth on
expect 0 21 21 0 1 0

# 7 node never appears: 21 queries / 20 waits, fail closed at 159
run_case protected_missing protected_missing on
expect 1 159 159 0 21 20
never21
waits05

# 8 only effect_input.speakers.extra present: exact second-column match required
run_case near_name near_name on
expect 1 159 159 0 21 20
never21
waits05

# 9 list rc!=0 even though stdout forges an exact protected line: fail closed
run_case list_rc_nonzero list_rc_nonzero on
expect 1 159 159 0 21 20
never21
waits05

# 10 get-default rc!=0 with a seemingly valid value: fail closed, no set, no 21
run_case get_rc_nonzero get_rc_nonzero on
expect 1 159 159 0
never21

# 11 needed set-default returns rc!=0: fail closed, never 21
run_case set_fail set_fail on
expect 1 159 159 1
never21

# 12 node appears only on the 3rd list call: 2 waits then open to 21
run_case delayed_3rd delayed_3rd on
expect 0 21 21 0 3 2
waits05

# 13 off while every pactl call fails: rc0, 159/159, no pactl invocation
run_case off_all_fail off_all_fail off
expect 0 159 159 0
eq "$cname pactl total calls" 0 "$PACTLTOTAL"
never21

# 14 sound card absent on the 1st and 2nd probe, present (as card 0) on the
# 3rd: the source must wait it out (2 waits), then open both channels to 21.
# A script that gives up on the first missing probe (exit 0, never 21) fails.
run_case card_delayed_3rd card_delayed_3rd on
expect 0 21 21 0 1 2
eq "$cname card probes" 3 "$CARDQ"
waits05

# 15 sound card never appears: bounded wait (<= 20 * 0.5 s), non-zero exit,
# no amixer write at all and never the loud 21 -- success is not faked.
run_case card_missing card_missing on
nonzero "$cname rc" "$RC"
eq "$cname amixer calls" 0 "$AMIXTOTAL"
range "$cname card probes" 1 21 "$CARDQ"
range "$cname waits" 1 20 "$SLEEPN"
never21
waits05

# 16 sound card absent on the 1st probe and the wait itself fails:
# non-zero exit, no amixer write, never 21.
run_case card_sleep_fail card_sleep_fail on
nonzero "$cname rc" "$RC"
eq "$cname amixer calls" 0 "$AMIXTOTAL"
range "$cname waits" 1 20 "$SLEEPN"
never21

# 17 compatible is lenovo,baldur-gen4: a longer token is not the exact
# NUL-delimited lenovo,baldur, so the model gate exits 0 before the config,
# the card lookup and every amixer/pactl call.
run_case gen4 gen4 on
expect 0 "" "" 0
eq "$cname amixer calls" 0 "$AMIXTOTAL"
eq "$cname pactl calls" 0 "$PACTLTOTAL"
eq "$cname card probes" 0 "$CARDQ"
never21
waits05

# 18 the sound card id reads "LENOVO Gen4": similar but not the exact
# LENOVOTB323FU, so the card is never found, the wait is bounded, no amixer
# write happens and the amps are never opened to 21.
run_case gen4card gen4card on
nonzero "$cname rc" "$RC"
eq "$cname amixer calls" 0 "$AMIXTOTAL"
range "$cname card probes" 1 21 "$CARDQ"
range "$cname waits" 1 20 "$SLEEPN"
never21
waits05

# 19 the sound card id reads "LENOVOTB323FU-extra": an exact id match is
# required, so the longer id is rejected exactly like a missing card.
run_case simcard simcard on
nonzero "$cname rc" "$RC"
eq "$cname amixer calls" 0 "$AMIXTOTAL"
range "$cname card probes" 1 21 "$CARDQ"
range "$cname waits" 1 20 "$SLEEPN"
never21
waits05

# ---- summary ---------------------------------------------------------------

printf '\n==== summary: %s checks, %s failures ====\n' "$TOTAL" "$FAILS"
[ "$FAILS" -eq 0 ] || exit 1
exit 0
