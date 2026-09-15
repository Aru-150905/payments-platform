# CS F372: Operating Systems — Assignment 1

**Birla Institute of Technology & Science, Pilani — Hyderabad Campus**
First Semester 2026–2027

---

## 1. Group Details

| Name | ID Number |
|------|-----------|
| Arihant Rakhecha | *(fill in ID)* |
| *(member 2)* | *(fill in ID)* |
| *(member 3)* | *(fill in ID)* |
| *(member 4)* | *(fill in ID)* |

> Replace the placeholders above with the full names and ID numbers of every group member before submitting.

---

## 2. Environment

Everything was developed and tested on:

- **OS:** Ubuntu 24.04 LTS, running as a guest on Oracle VirtualBox
- **VM resources:** 4 CPU cores, 4 GB RAM, 50 GB virtual disk
- **Compiler:** `gcc` (from `build-essential`), all programs compile cleanly with `-Wall`
- **Kernel for Problem 5:** custom-built Linux **6.14.0** (`uname -r` → `6.14.0`)

Problems 1–4 run on any standard Ubuntu 24.x system. **Problem 5 requires the custom kernel** built using the steps in Section 7.

### One-time setup on a fresh machine

```bash
sudo apt update
sudo apt install -y build-essential
```

Problems 2 and 3 also rely on standard command-line utilities (`ps`, `awk`, `sort`, `head`, `cut`, `uniq`, `ping`). All of these ship with Ubuntu 24.04 by default; no extra installation is needed.

---

## 3. Files in this Submission

| File | Belongs to | Description |
|------|-----------|-------------|
| `prob1.c` | Problem 1 | Fork + two unnamed pipes, GCD exchange between parent and child |
| `prob2.c` | Problem 2 | Weighted resource monitor using System V message queues |
| `prob3.c` | Problem 3 | `logtop` — log frequency analyzer built from a 5-stage process pipeline |
| `access.log` | Problem 3 | Sample log file (114 lines) used for testing |
| `prob4.c` | Problem 4 | `belt_shell` — warehouse conveyor-belt shell |
| `setnice_logged.c` | Problem 5 | Kernel source file for the custom system call |
| `kernel_edits.txt` | Problem 5 | Exact list of kernel files edited, with the lines added |
| `test_syscall.c` | Problem 5 | User-space test — valid case (`nice_val = 5`) |
| `test_syscall_edge_case.c` | Problem 5 | User-space test — invalid case (`nice_val = 25`) |
| `test_syscall` | Problem 5 | Pre-compiled binary of `test_syscall.c` |
| `test_syscall_edge_case` | Problem 5 | Pre-compiled binary of `test_syscall_edge_case.c` |
| `output_run.png` | Problem 5 | Screenshot: kernel version, successful call, `dmesg` output |
| `test_2_edge_case.png` | Problem 5 | Screenshot: invalid argument case, no new kernel log entry |
| `README.md` | — | This file |

---

## 4. Quick Start — Compile Everything

From the directory containing the source files:

```bash
gcc -Wall -o prob1      prob1.c
gcc -Wall -o prob2      prob2.c
gcc -Wall -o logtop     prob3.c
gcc -Wall -o belt_shell prob4.c
```

All four compile with **no warnings and no errors** under `-Wall`.

No `Makefile`, no external libraries, and no special linker flags are required.

> **Note:** Problem 5 is not part of this step. Its kernel component must be compiled *into the kernel*, and its test programs only work once that kernel is booted. See Section 7.

---

## 5. Problem-by-Problem Instructions

### Problem 1 — Fork, Pipes and GCD (`prob1.c`) [2 Marks]

**Compile and run**

```bash
gcc -Wall -o prob1 prob1.c
./prob1
```

The program takes **no command-line arguments and no user input**. The integer array is hard-coded inside `main()`.

**What it does**

1. The parent creates two unnamed pipes — `p2c` (parent → child) and `c2p` (child → parent) — then forks.
2. Each round, the parent picks two elements at random positions from the array and *removes* them by swapping each with the current last element and shrinking the logical size. This guarantees no element is ever picked twice.
3. The parent prints `Parent : x y` and writes both integers to `p2c` in a single `write()`.
4. The child reads the pair, computes the GCD using the iterative Euclidean algorithm, and prints `Child : x y g`.
5. The child sleeps for `time(NULL) % g` **milliseconds** (via `usleep`), then writes `g` back on `c2p`.
6. The parent reads `g`, prints `Parent: g`, and sleeps for `g` milliseconds.

**Termination**

- Normally after `n/2` rounds, once the array is exhausted. The parent then closes both pipes and `wait()`s for the child.
- Or immediately on **Ctrl+C**. `SIGINT` is caught with `sigaction`; the handler uses only async-signal-safe calls (`write` and `_exit`) and prints:
  `[INTERRUPT] SIGINT received. Terminating.`

**Expected output (values are random each run)**

```
Parent : 40 1
Child : 40 1 1
Parent: 1
Parent : 45 43
Child : 45 43 1
Parent: 1
Parent : 24 3
Child : 24 3 3
Parent: 3
...
```

**Runtime note:** the built-in array holds 320 integers, i.e. **160 rounds**. Because both processes sleep between rounds, a full run takes a couple of minutes. Press **Ctrl+C** at any time to stop early — this is also the required interrupt behaviour, so it doubles as the demo for it.

**Design notes**

- No global variables are used anywhere. The array, pipe descriptors and PID all live in `main()`; the child's work is done in `child_loop(in_fd, out_fd)`, which receives its descriptors as parameters.
- Each side closes the pipe ends it does not use immediately after `fork()`, so the child's `read()` loop terminates cleanly on EOF when the parent closes `p2c[1]`.
- `fflush(stdout)` is called after every `printf` so parent and child output interleave in the correct order even when stdout is redirected to a file or a pipe.

---

### Problem 2 — Weighted Resource Monitor (`prob2.c`) [3 Marks]

**Compile and run**

```bash
gcc -Wall -o prob2 prob2.c
./prob2
```

The program then prompts for three positive integers on one line:

```
Enter n (seconds between prints), k (processes per print), r (iterations before prompt): 2 5 3
```

- `n` — seconds between two consecutive prints
- `k` — number of processes shown per print
- `r` — number of iterations after which the user is prompted for a PID

Any non-positive or non-numeric input is rejected with an error message and the program exits.

**What it does**

- A **System V message queue** is created *before* `fork()` using `ftok("/tmp", 'R')` and `msgget(key, IPC_CREAT | 0666)`, so parent and child share the same queue.
- Two message types are used: `mtype = 1` for parent → child, `mtype = 2` for child → parent. This keeps the two directions from ever reading each other's messages off the same queue.
- Every `n` seconds the **child** prints the top `k` processes by `usage_score = 3 * CPU% + 2 * MEM%`, in descending order, with PID, command, CPU%, MEM% and score. This is produced by a 4-stage pipeline built with `pipe`, `fork`, `dup2` and `execvp`:

  ```
  ps ax -o pid=,comm=,pcpu=,pmem=  |  awk '{score = 3*$3 + 2*$4; ...}'  |  sort -k5 -rn  |  head -n k
  ```

- Every `r` iterations the child sends a "ready" message and blocks. The **parent** prompts:

  ```
  >>> Enter PID to act on (-1 = skip, -2 = quit program):
  ```

  and sends the value to the child.

- On receiving the value, the child does one of:
  - `-2` → removes the message queue (`msgctl(..., IPC_RMID, NULL)`) and exits the program.
  - `-1` → takes no action and resumes monitoring.
  - any other non-positive value → reported as an invalid PID and ignored, monitoring resumes.
  - a positive PID → prints the process's PID, owner, command, CPU%, MEM% and usage_score (via `ps -p <pid> -o pid=,user=,comm=,pcpu=,pmem=` piped into `awk`), **then** kills it with `SIGKILL` and prints a confirmation. If the PID does not exist, this is reported and nothing is killed.

**Termination**

- Normally when the user enters `-2`. The child removes the queue and exits; the parent breaks its loop, reaps the child, and prints `[PARENT] child reaped. Exiting.`
- Or on **Ctrl+C**, which is caught with `sigaction`. The handler removes the message queue before exiting, so no stale IPC object is left behind.

**Sample output format**

```
PID      COMMAND                    CPU%     MEM%      SCORE
   1234 firefox                      12.3      8.1      53.10
   2201 gnome-shell                   6.4      4.0      27.20
...

>>> Enter PID to act on (-1 = skip, -2 = quit program): 2201

[CHILD] process details before termination:
  PID     : 2201
  COMMAND : gnome-shell
  OWNER   : ranjit
  CPU%    : 6.4
  MEM%    : 4.0
  SCORE   : 27.20
[CHILD] process 2201 terminated successfully.
```

**Suggested way to demo the kill safely**

Do not kill a random system process. Instead, create a CPU-hungry dummy process in a second terminal — it will immediately climb to the top of the list:

```bash
yes > /dev/null &        # note the PID printed by the shell
```

Then enter that PID at the prompt. Killing it affects nothing else on the system.

**Permissions:** a process can only be killed if it is owned by the same user (or the program is run with `sudo`). Attempting to kill a process owned by another user reports a permission error rather than crashing.

**If the program is ever force-killed (e.g. `kill -9`), the message queue may survive.** Check and clean up with:

```bash
ipcs -q                  # list message queues
ipcrm -q <msqid>         # remove the leftover queue
```

A leftover queue is harmless — the next run simply reuses it — but removing it keeps the demo clean.

**Design notes**

- No global variables. The queue id, `n`, `k` and `r` are passed into `child_loop()` as parameters; the signal handler re-derives the queue id from `ftok()` rather than reading a global.
- `msgsnd`/`msgrcv` interrupted by a signal (`EINTR`) are retried rather than treated as fatal.

---

### Problem 3 — Log Frequency Analyzer, `logtop` (`prob3.c`) [2 Marks]

**Compile and run**

```bash
gcc -Wall -o logtop prob3.c
./logtop access.log 1
```

Usage: `./logtop <logfile> <column>` — the column number is 1-based, with a single space as the field delimiter.

**Verified output** on the included `access.log` — this matches the sample output in the assignment PDF exactly:

```
     42 192.168.1.10
     31 192.168.1.14
     19 10.0.0.5
     12 10.0.0.9
      7 172.16.0.3
```

Another column works the same way. `./logtop access.log 3` (the endpoint column) gives:

```
     30 /home
     25 /login
     20 /about
     18 /api
     15 /index.html
```

**What it does**

As required by the assignment, **no counting, sorting or frequency-aggregation logic is implemented in C**. The program builds this five-stage UNIX pipeline itself, one `fork()`/`execvp()` per stage, wiring the stages together with `pipe()` and `dup2()`:

```
cut -d' ' -f<col> <logfile> | sort | uniq -c | sort -rn | head -n 5
```

Each child inherits the previous stage's read end on `stdin` and writes to the next stage's write end on `stdout`. The parent closes every descriptor it no longer needs, so each stage sees a proper EOF and the pipeline drains without deadlock. The parent then `waitpid()`s on all five children.

**Error handling**

- Wrong number of arguments → `Usage: ./logtop <logfile> <column>`
- Non-positive or non-numeric column → `Error: column must be a positive integer`
- Missing or unreadable file → the filename followed by the system error, via `perror` (e.g. `nosuch.log: No such file or directory`)

If the file has fewer than 5 distinct values, `head -n 5` simply prints however many exist, as required.

---

### Problem 4 — `belt_shell` (`prob4.c`) [3 Marks]

**Compile and run**

```bash
gcc -Wall -o belt_shell prob4.c
./belt_shell
```

The prompt is `belt-control$`. The item queue starts empty, holds at most **10 items**, and each item name may be up to 63 characters (longer names are truncated). Input lines are read with `fgets` (maximum 1023 characters) and split on spaces and tabs using `strtok`.

**Internal commands** — handled directly by the shell process, with no `fork()`:

| Command | Behaviour |
|---------|-----------|
| `add_item <name>` | Appends `<name>` to the queue. If `<name>` is missing → `Error: usage is add_item <name>`. If the queue already holds 10 items → `Error: belt queue is full (max 10 items)`. |
| `list_items` | Prints every queued item, one per line, in the order added. If empty → `Queue is empty`. |
| `quit` | Exits the shell. This is the only command that exits it. |

**External commands** — the shell forks, the child calls `execvp()`, the parent `waitpid()`s for it to finish:

| Command | Behaviour |
|---------|-----------|
| `date` | Runs `date`, displaying the current system date and time. |
| `ping <address>` | Runs `ping -c 4 <address>`, i.e. exactly 4 pings. If `<address>` is missing → `Error: usage is ping <address>`. |

Any unrecognised command prints `Error: '<cmd>' is not a recognised command`. Empty lines are ignored and simply reprint the prompt.

**Signal handling (Ctrl+C)**

`SIGINT` is installed with `sigaction` and **without** `SA_RESTART`, so a Ctrl+C during the blocking `fgets()` interrupts it rather than being ignored. The handler itself only sets a `volatile sig_atomic_t` flag — the safe minimum of work inside a signal handler. The top of the next loop iteration sees the flag, clears `stdin`'s error state, prints

```
[ALERT] Emergency stop triggered, item queue cleared
```

resets the queue to empty, and reprints the prompt. **The shell does not terminate.**

Child processes reset `SIGINT` to `SIG_DFL` before `execvp()`, so pressing Ctrl+C during a long `ping` kills the `ping` and returns to the prompt, leaving the shell itself running. The parent's `waitpid()` retries on `EINTR` rather than treating the interruption as an error.

**Verified sample session**

```
belt-control$ add_item box1
belt-control$ add_item box2
belt-control$ list_items
box1
box2
belt-control$ date
Fri Sep  4 09:13:41 IST 2026
belt-control$ foo bar
Error: 'foo' is not a recognised command
belt-control$ add_item
Error: usage is add_item <name>
belt-control$ quit
```

And the Ctrl+C behaviour (verified on a terminal):

```
belt-control$ add_item box1
belt-control$ list_items
box1
belt-control$ ^C
[ALERT] Emergency stop triggered, item queue cleared
belt-control$ list_items
Queue is empty
belt-control$ quit
```

**Notes for the demo**

- `ping` needs working network access in the VM. If the VM has no internet it will report an unresolved host — an environment limitation, not a program fault. `ping 127.0.0.1` works offline and can be used instead.
- Ctrl+D (EOF) also ends the shell cleanly, in addition to `quit`.

---

## 6. Problem 5 — Custom System Call `setnice_logged` [2 Marks]

**Syscall name:** `setnice_logged`
**Syscall number:** `470`
**Kernel version:** Linux `6.14`
**Signature:** `long sys_setnice_logged(int nice_val)`

**Functionality**

1. Validates that `nice_val` lies in `[-20, 19]`. If not, returns `-EINVAL` and modifies nothing.
2. Reads the calling process's current nice value with `task_nice(current)`.
3. Applies the new value with `set_user_nice(current, nice_val)`.
4. Logs the calling process's PID, `comm`, old nice and new nice to the kernel buffer with `printk(KERN_INFO ...)`.
5. Returns `0` on success.

The implementation is in `setnice_logged.c`, written with the `SYSCALL_DEFINE1` macro so the kernel generates the correct entry-point wrapper.

---

## 7. Building the Kernel with the Custom System Call

These are the exact steps used. On a 4-core VM the compile step takes roughly **1–2 hours** and needs about **25–30 GB of free disk space**, so plan accordingly.

### Step 1 — Install build dependencies

```bash
sudo apt update
sudo apt install -y build-essential libncurses-dev bison flex libssl-dev \
                    libelf-dev bc dwarves zstd rsync
```

### Step 2 — Get the Linux 6.14 source tree

```bash
cd ~
wget https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.14.tar.xz
tar -xf linux-6.14.tar.xz
cd linux-6.14
```

### Step 3 — Add the syscall source file

```bash
cp /path/to/setnice_logged.c kernel/
```

### Step 4 — Apply the three kernel edits

These are recorded in `kernel_edits.txt`. All three must be made.

**(a) `arch/x86/entry/syscalls/syscall_64.tbl`** — add a new row after the last `common` entry (line 466 in the stock 6.14 tree), *before* the `x32` block:

```
470	common	setnice_logged	sys_setnice_logged
```

> The separators in this table are **tab characters**, not spaces. Copy the line from `kernel_edits.txt` rather than retyping it.

**(b) `include/linux/syscalls.h`** — add the prototype alongside the other `asmlinkage` declarations (around line 305), before the closing `#endif`:

```c
asmlinkage long sys_setnice_logged(int nice_val);
```

**(c) `kernel/Makefile`** — add the object to the always-built list (around line 14):

```make
obj-y     += setnice_logged.o
```

### Step 5 — Configure

```bash
cp /boot/config-$(uname -r) .config
make olddefconfig

# Ubuntu's stock config points at signing keys that are not in the source tree.
# These two lines prevent the "No such file or directory: debian/canonical-certs.pem" build failure.
scripts/config --disable SYSTEM_TRUSTED_KEYS
scripts/config --disable SYSTEM_REVOCATION_KEYS

# Optional but strongly recommended: cuts build time and saves ~20 GB of disk.
scripts/config --set-val DEBUG_INFO n
scripts/config --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT
scripts/config --enable  DEBUG_INFO_NONE

make olddefconfig
```

### Step 6 — Build and install

```bash
make -j$(nproc)
sudo make modules_install
sudo make install
sudo update-grub
sudo reboot
```

### Step 7 — Boot into the new kernel and verify

If the machine does not boot into 6.14.0 automatically, hold **Shift** (or **Esc**) during startup and pick it under *Advanced options for Ubuntu*.

```bash
uname -r
# expected: 6.14.0
```

---

## 8. Testing Problem 5

Two test programs are provided. Pre-compiled binaries (`test_syscall`, `test_syscall_edge_case`) are included, but recompiling is trivial:

```bash
gcc -Wall -o test_syscall            test_syscall.c
gcc -Wall -o test_syscall_edge_case  test_syscall_edge_case.c
```

### Test 1 — Valid case (`nice_val = 5`)

```bash
./test_syscall
sudo dmesg | grep setnice_logged
```

Expected:

```
Nice value successfully changed.
```

and a matching kernel-buffer entry:

```
[  298.094881] setnice_logged: PID=6556 comm=test_syscall old_nice=0 new_nice=5
```

This is captured in `output_run.png`, together with `uname -r` showing `6.14.0`.

### Test 2 — Invalid case (`nice_val = 25`, out of range)

```bash
./test_syscall_edge_case
sudo dmesg | grep setnice_logged
```

Expected:

```
setnice_logged failed: Invalid argument
```

`dmesg` shows **no new entry** — the range check returns `-EINVAL` before anything is modified or logged, which is exactly the required behaviour. This is captured in `test_2_edge_case.png`.

### If the tests are run on the wrong kernel

On a stock Ubuntu kernel, syscall 470 does not exist and the test prints:

```
setnice_logged failed: Function not implemented
```

That means the machine has not booted into the custom 6.14.0 kernel. Re-check `uname -r` and Step 7.

### Note on negative nice values

The syscall calls `set_user_nice()` directly and does not perform a `CAP_SYS_NICE` capability check. As a result an unprivileged process can also lower its nice value (e.g. `-5`), which the standard `nice`/`setpriority` interfaces would refuse. This follows the specification given in the assignment, which asks only for a range check on `nice_val`.

---

## 9. Summary of Commands

```bash
# Problems 1–4
gcc -Wall -o prob1      prob1.c        && ./prob1
gcc -Wall -o prob2      prob2.c        && ./prob2          # then enter: n k r
gcc -Wall -o logtop     prob3.c        && ./logtop access.log 1
gcc -Wall -o belt_shell prob4.c        && ./belt_shell

# Problem 5 (requires the custom 6.14.0 kernel to be booted)
gcc -Wall -o test_syscall           test_syscall.c           && ./test_syscall
gcc -Wall -o test_syscall_edge_case test_syscall_edge_case.c && ./test_syscall_edge_case
sudo dmesg | grep setnice_logged
```
