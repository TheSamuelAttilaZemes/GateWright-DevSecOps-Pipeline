# Resource management: how Gatewright avoids freezing the machine

Gatewright runs on laptops and servers, often next to other work. It protects the machine in four layers.

## 1. Detection

At every run (and with `gatewright hw`) Gatewright detects:

- CPU cores (logical and physical), current load
- total and free RAM, free disk
- NVIDIA GPUs (via `nvidia-smi`)
- whether it runs on battery, inside a container, under WSL
- OS and distribution

## 2. Profile and budget

| | laptop | server |
|---|---|---|
| Chosen when (`profile: auto`) | the machine has a battery, or has < 8 CPUs / < 16 GB RAM | 8+ CPUs, 16 GB+ RAM, no battery |
| Max parallel jobs | half the CPUs (halved again on battery) | CPUs − 1 |
| CPU budget for jobs | half the CPUs | CPUs − 1 |
| RAM budget for jobs | 50% of free RAM | 80% of free RAM |
| Start new jobs only below | 80% CPU, 80% RAM | 90% CPU, 85% RAM |
| Watchdog stops the newest job above | 92% RAM | 95% RAM |
| Job priority | lowered (`nice 10` / below normal) | normal |

Force a profile with `profile: laptop|server` and override any value under `limits:`
(see [configuration.md](configuration.md#limits)).

## 3. Scheduling

Each job declares what it needs:

```yaml
resources: {cpu: 2, mem: 1GB}
```

A job starts only when **all** of these hold:

- its `needs` have finished OK
- fewer than `max_parallel` jobs are running
- declared CPU and RAM of the running jobs plus this one fit the budget
- live system CPU and RAM are below the start limits

If nothing is running, the next job always starts, so a job bigger than the budget runs alone
instead of waiting forever. `gatewright run -v` and the TUI show why a job is waiting
(e.g. `memory budget (3.0GB/2.0GB)`).

## 4. Watchdog

Every second, the scheduler checks system RAM. Above `kill_mem_percent`, it stops the **most
recently started** job (it has lost the least work), including its whole process tree or container.
The job is reported as an INFRA `resource` error:

> Gatewright stopped this job to protect the machine: system memory reached 93% (limit 92%), job was using 2.1GB.

Container jobs also get hard limits from Docker (`--cpus`, `--memory`). A container that exceeds its
memory is killed by the kernel and reported as out of memory (exit 137).

## Tips

- Declare realistic `mem` for heavy jobs (test suites, builds, container scans). The default is 256 MB.
- On a busy laptop, set `limits: {max_parallel: 1}` for predictable runs.
- Use `gatewright run --dry-run` to see each job's declared needs and the budget before running.
