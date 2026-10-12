# #501: native runner trial on ameesh-agents-1

Prepared for mesh-design's operator. These commands have **not been run**
on production. Run only after the reviewed commit is installed in the native
runner's venv. Trial one admitted native, `ameesh-dev3`; leave guest runners
and their guard alone. A pending instruction or wake must already be arranged
by mesh-design so the runner can complete a turn.

As root on `ameesh-agents-1`, select an on-disk `TMPDIR`, then check the
installed code and prepare the backup. The code assertion must pass before
removing the workaround; it uses an empty explicit gate setting so the
historic drop-in cannot mask the new default.

```bash
set -euo pipefail
: "${TMPDIR:?set an on-disk temporary directory}"
trial_dir=$(mktemp -d "$TMPDIR/ameesh-501-trial.XXXXXX")
trial_uid=$(id -u ameesh)
trial_home=$(getent passwd ameesh | cut -d: -f6)
trial_venv="$trial_home/.local/share/ameesh/venv"
trial_dropin="$trial_home/.config/systemd/user/ameesh-runner-agent@.service.d/zz-porte-hote-natif.conf"
trial_unit=ameesh-runner-agent@ameesh-dev3.service
runuser -u ameesh -- "$trial_venv/bin/python" - <<'PY'
from ameesh.config import Config
from ameesh.host_gate import gate_choice
native = gate_choice(Config(runner_id='runner-ameesh-dev3@ameesh-agents-1', host_gate=''), True)
guest = gate_choice(Config(runner_id='guest-deepseek6@ameesh-agents-1', host_gate=''), True)
assert native[0] == '', native
assert guest[0] == 'file', guest
print('installed #501 gate selection:', native, guest)
PY
cp -a "$trial_dropin" "$trial_dir/zz-porte-hote-natif.conf"
trial_since=$(date --iso-8601=seconds)
printf '%s\n' "$trial_since" > "$trial_dir/since.txt"
rm "$trial_dropin"
runuser -u ameesh -- env XDG_RUNTIME_DIR="/run/user/$trial_uid" systemctl --user daemon-reload
runuser -u ameesh -- env XDG_RUNTIME_DIR="/run/user/$trial_uid" systemctl --user restart "$trial_unit"
```

Wait for the arranged turn to finish; collect the following from the same
shell, preserving `trial_dir`, `trial_since`, `trial_uid`, `trial_venv` and
`trial_unit`. Commands are read-only. The pilot check is run from the
reviewed checkout on the host.

```bash
runuser -u ameesh -- env XDG_RUNTIME_DIR="/run/user/$trial_uid" \
  journalctl --user --unit "$trial_unit" --since "$trial_since" --no-pager \
  > "$trial_dir/native-journal.txt"
runuser -u ameesh -- "$trial_venv/bin/ameesh" list --json > "$trial_dir/registry.json"
sudo deploy/guest/install-pilot.sh --check > "$trial_dir/pilot-check.txt" 2>&1
rg 'host gate selected|lease acquired|end of turn|ack not written|not my gate' "$trial_dir/native-journal.txt"
printf 'Evidence directory: %s\n' "$trial_dir"
```

Acceptance: startup selects `none (native runner; guest gate excluded)`
after canon sync identifies the lending host, dev3 acquires its lease,
and the arranged turn finishes. There must be no repeated `ack not written`
lines. `explicit host_gate=none` alone does **not** prove the new default:
remove another explicit override from the trial environment if present,
through the operator's normal configuration procedure. Confirm the guest
runner still obeys the guard. Attach the journal, registry snapshot, pilot
check and installed commit/version to the PR; do not mark real-host
acceptance complete without them.

If the trial fails, restore the workaround and restart only the trial unit:

```bash
cp -a "$trial_dir/zz-porte-hote-natif.conf" "$trial_dropin"
runuser -u ameesh -- env XDG_RUNTIME_DIR="/run/user/$trial_uid" systemctl --user daemon-reload
runuser -u ameesh -- env XDG_RUNTIME_DIR="/run/user/$trial_uid" systemctl --user restart "$trial_unit"
```

After a successful trial, the operator decides whether the workaround
remains removed and when to restart the other native units. The patch does
not install a replacement drop-in.
