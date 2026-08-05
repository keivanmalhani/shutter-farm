# shutter-farm

[![CI](https://github.com/keivanmalhani/shutter-farm/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/shutter-farm/actions/workflows/ci.yml)
![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

English | [Espanol](README.es.md)

Point a container at a media volume. It culls the whole archive on a schedule, and never does the same work twice.

`shutter-farm` is the deployment story for the [shutter toolchain](https://github.com/keivanmalhani). It discovers folders that need processing, dispatches each to [shutter-cull](https://github.com/keivanmalhani/shutter-cull) or [shutter-select](https://github.com/keivanmalhani/shutter-select), and keeps a ledger so a nightly cron is idempotent, resumable, and cheap.

## Local-first is not un-deployable

Local-first is a promise about where your data goes. It is not a claim that the software cannot be operated properly.

A studio with an archive machine, a NAS, or a rack does not want to run a CLI by hand over four terabytes of shoots. They want a thing that runs at 3am, tells them what it did, and does not fall over when one card is corrupt. That is infrastructure, and infrastructure is not the opposite of privacy: this container mounts *your* volume, on *your* machine or *your* cluster, and the only port it opens is the metrics endpoint you choose to scrape.

The photos still never leave.

## What it does

```text
   /media  ......................  your archive, mountable read-only
      |
      v
   discover  ....................  one job per media-bearing folder
      |                            outputs, dotfiles, "do not include" all skipped
      v
   ask the ledger  ..............  done and unchanged? skip it, that is the point
      |
      v
   dispatch  ....................  shutter-cull for photos, shutter-select for video
      |                            subprocess, timeout, process group killed on hang
      v
   record  ......................  after EVERY folder, not at the end of the batch
      |
      +--> structured JSON logs on stdout
      +--> Prometheus metrics on :9090
      +--> /healthz and /readyz
```

## The interesting part is the ledger

A cron job that reprocesses everything every hour is not a pipeline, it is a space heater. So the farm has to answer one question cheaply and correctly: **has this folder already been done, in the state it is in right now?**

Timestamps are the obvious answer and the wrong one, because a folder's mtime changes when anything inside it is touched, including by the tools the farm just ran. The key is content: a fingerprint over each media file's name, size and mtime, and *only* media files.

That one decision buys everything:

- Add a photo, and the folder is work again.
- Run the tools, which write sidecars and a `_selects` tree right into that folder, and the fingerprint does not move. The next sweep does nothing.
- Copy the folder somewhere else, and it is correctly a different job.
- Lose the ledger entirely, and the farm reprocesses. Wasteful, never wrong, which is the correct direction for something running unattended.

Verified end to end: a real sweep over a real archive ran both engines, wrote XMP sidecars and a selects timeline, and the second sweep finished in 0.1 seconds having done nothing. Adding one photo brought exactly one folder back.

## Failure is per folder, never per run

One unreadable card must not fail a nightly batch of two hundred shoots.

A folder that fails is recorded with its error, retried with exponential backoff, and after three attempts **quarantined**: it stops burning cycles and starts being visible instead, logged at WARNING on every subsequent sweep with the error that put it there. Fix the folder, and the fingerprint change releases it automatically. Or release it by hand:

```bash
shutter-farm retry --root /media /media/2026-04-canyon
```

## Built to be killed

Batch work belongs on cheap capacity, so the whole design assumes the node can vanish mid-sweep:

- State is written after every folder, so a preempted job loses one folder of work, not the batch.
- SIGTERM finishes the current folder and exits cleanly rather than dying mid-write.
- Every tool invocation has a timeout, and a hang kills the whole process group so ffmpeg children do not outlive their parent.
- Exit codes distinguish causes: `0` clean, `1` misconfiguration, `2` the sweep ran but some folders failed. A scheduler can tell "I could not start" from "I ran and some of the work is broken", which are different pages at 3am.

## Run it

Locally:

```bash
ARCHIVE=~/Pictures docker compose up
```

Kubernetes, nightly:

```bash
kubectl apply -f deploy/k8s/pvc.yaml -f deploy/k8s/cronjob.yaml
```

Cloud Run Jobs:

```bash
./deploy/cloud-run-job.sh my-project us-central1
```

Or with no container at all, on the machine the archive is already on:

```bash
pip install git+https://github.com/keivanmalhani/shutter-farm.git
```

```bash
shutter-farm doctor --root /Volumes/Archive
```

```bash
shutter-farm run --root /Volumes/Archive
```

## Before you trust it with a schedule

```bash
shutter-farm doctor --root /media
```

Every scheduled job has the same first support ticket: it ran, it did nothing, and the logs are technically complete and humanly useless. The cause is almost always environmental. An engine is not on PATH inside the container. The archive is mounted read-only so the ledger cannot be written, which quietly turns off idempotency and makes every sweep redo everything. `--write` is on against a read-only volume, so all two hundred folders fail identically forever.

`doctor` asks those questions on purpose, before the schedule does:

```text
  [ok  ] python                        Python 3.11
  [ok  ] media root                    /media is readable
  [ok  ] work found                    41 folder(s) with media
  [FAIL] ledger                        Cannot create /media/.shutter-farm-state.json, /media is read-only
                                     -> Put the ledger on its own writable volume:
                                        --state /state/shutter-farm-state.json. This is the normal
                                        setup when the archive is mounted read-only, which it should be.
  [ok  ] write mode                    Writes are off.
  [warn] shutter-select                Not on PATH, so video folders will be skipped
                                     -> pip install git+https://github.com/keivanmalhani/shutter-select.git
  [ok  ] shutter-cull                  shutter-cull, installed, does not report a version
  [ok  ] shutter-cull needs exiftool   present
  [ok  ] disk space                    412.9 GB free
  [ok  ] metrics port                  Port 9090 is free

  1 blocking problem(s), 1 warning(s). A sweep will not work until the
  blocking ones are fixed.
```

Three rules it holds itself to:

- **Every problem in one pass, not the first one.** Three round trips to fix three things is exactly the support experience this avoids, so nothing bails out early.
- **A check that cannot tell you what to type is not finished.** Every non-passing line carries the command that fixes it, including the compose and Kubernetes spellings where they differ.
- **A false alarm is worse than no alarm.** Being on PATH is not the same as being runnable, so the engines are actually executed. But a non-zero `--version` does not mean broken: shutter-cull requires a subcommand and exits 2 on a bare `--version`, so the probe falls back to `--help` rather than sending you to reinstall something that works.

`--json` emits one object per check plus a single `doctor_verdict` line, so it works as a container startup probe. Exit code is 0 when a sweep will work and 1 when it will not, and "neither engine is installed" counts as will not, even though each engine alone is only a warning.

## Writes are off by default

The farm passes a `--write` flag through to the tools rather than deciding for you, and it defaults to off everywhere: in the CLI, in the manifests, and in the compose file, which additionally mounts the archive read-only. A scheduled batch that quietly starts rating a client archive because a default flipped is exactly the failure this avoids.

Look at a dry sweep first. Then turn it on and mean it.

## Configuration

Every flag has an environment variable, because manifests configure with env and humans configure with flags.

| Flag | Env | Default | What it does |
| --- | --- | --- | --- |
| `--root` | `FARM_ROOT` | - | The media volume to sweep. |
| `--state` | `FARM_STATE` | `<root>/.shutter-farm-state.json` | Ledger path. Put it on its own volume if the archive is read-only. |
| `--write` | `FARM_WRITE` | off | Let the tools write their outputs. |
| `--timeout` | `FARM_TIMEOUT` | 3600 | Seconds before one folder's tool is killed. |
| `--max-jobs` | `FARM_MAX_JOBS` | 0 | Cap folders per sweep. The rest queue for next time. |
| `--max-attempts` | `FARM_MAX_ATTEMPTS` | 3 | Failures before a folder is quarantined. |
| `--metrics-port` | `FARM_METRICS_PORT` | 0 | Serve `/metrics`, `/healthz`, `/readyz`. |
| `--interval` | `FARM_INTERVAL` | 900 | `serve` mode only: seconds between sweeps. |

## Observability without a vendor

Logs are one JSON object per line on stdout, with the `severity` spelling Cloud Logging expects, so `jsonPayload.event` and `jsonPayload.folder` are queryable fields with no parser and no sidecar agent:

```json
{"severity":"INFO","time":"2026-08-05T17:12:11Z","event":"job_finished","service":"shutter-farm","folder":"/media/2026-04-canyon","tool":"shutter-cull","duration_seconds":1.9,"media_files":4}
```

Metrics are Prometheus text format from a stdlib HTTP server, so a GKE ServiceMonitor and a local `curl` both work:

```text
shutter_farm_jobs_total{result="success",tool="shutter-cull"} 41
shutter_farm_jobs_total{result="failure",tool="shutter-select"} 2
shutter_farm_folders{status="quarantined"} 1
shutter_farm_last_run_timestamp_seconds 1785945130
```

That last one is the alert worth having: if it stops advancing, the schedule is broken, and no amount of green pods will tell you that.

## Security posture

- Non-root (uid 10001), read-only root filesystem, all capabilities dropped, `no-new-privileges`.
- No outbound network. The only listener is the metrics port.
- The farm is stdlib only: zero dependencies of its own, so it is never the reason an image fails to build or a CVE scan lights up.
- Commands are built as argument lists, never shell strings. A folder with a quote in its name is a normal thing on a photographer's drive, not an injection.
- Symlinked directories are not followed, so a link inside the work root cannot pull an unrelated archive into a scheduled batch.

## Development

```bash
pip install -e ".[dev]"
pytest
```

92 tests, no engines needed: the farm dispatches to the tools rather than importing them, so the suite runs against a fake dispatcher and covers what the farm actually is, which is discovery, idempotency, failure isolation, diagnostics and observability. doctor's environment is injected rather than real, so its tests build a machine broken in one specific way instead of asserting on whatever the runner happens to be sitting on, and they hold identically on a laptop with ffmpeg and in a container without it. CI additionally builds the image on every push and asserts it does not run as root, because a repo whose whole argument is "this deploys" should not leave that unverified.

## Family

[shutter-cull](https://github.com/keivanmalhani/shutter-cull) and [shutter-select](https://github.com/keivanmalhani/shutter-select) are the engines. [shutter-cull-mcp](https://github.com/keivanmalhani/shutter-cull-mcp) is the agent interface. This is the batch one: same tools, same guarantees, on a schedule.

## License

MIT, see [LICENSE](LICENSE).
