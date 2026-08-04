# ZMUX Capability Report — what you can actually do with the APK

**Date:** 2026-07-31
**Method:** every claim below was executed against the current build, not read
off the source. Commands, outputs and the bugs are reproduced verbatim.
**Comparison baseline:** GNU bash 5.x in the agent sandbox (x86_64 Linux).

> **Caveat that colours everything:** these runs are on **x86_64 Linux with
> CPython 3.11**, not on an ARM phone with the p4a runtime. Logic-level results
> (parsing, routing, exit codes) transfer. Anything touching Android's kernel,
> SELinux or the APK layout does **not** — that is still unverified.

---

## 1. The headline answer

**Is ZMUX a real shell, or a wrapper that fakes output?**

It is **genuinely real execution — but it is not a shell.** Both halves matter.

Real, proven below: separate OS processes with their own PIDs and process
groups, true exit codes (including `42`), signal-death detection, working
pipelines, live streaming output, real HTTPS, a real SQLite engine.

Not a shell: there is **no shell language**. `&&`, `;`, `$VAR`, `$(...)`, `*`,
`~` and `&` are **not implemented**, and — critically — most of them fail
*silently*. ZMUX is best described as **a Python REPL with a command runner
attached**, not as bash.

---

## 2. Execution is real — proof

| Test | Command | Result | Verdict |
|---|---|---|---|
| Real binary | `/bin/echo hello_from_real_binary` | `exit=0`, `'hello_from_real_binary\n'` | real |
| Nonzero exit | `/bin/false` | `exit=1` | real |
| Arbitrary exit | `/bin/sh -c 'exit 42'` | `exit=42` | real |
| Signal death | `/bin/sh -c 'kill -TERM $$'` | `exit=-15`, `[process terminated by signal 15]` | real |
| Separate process | `/bin/sh -c 'echo $$'` | child `5696` vs our `5683` | real |
| Own process group | `ps -o pid,pgid` | `5699 5699 sh` — pgid == pid | real |
| Pipeline | `seq 5 \| head -2 \| tr '\n' ','` | `'1,2,'` | real |

That process-group result is the important one: each pipeline gets its own
group via `start_new_session=True`, which is why Ctrl+C can kill a runaway job
without killing the app itself.

**Streaming is real too.** Measured: three `print`s separated by 0.5 s arrive at
**t=0.0 / 0.5 / 1.0 s**, not in one burst. Live `zpip` download output captured
mid-flight:

```
mdurl-0.1.2          0.0 MiB  272.3 KiB/s [####################] 100%
markdown-it-py-...   0.1 MiB 1410.6 KiB/s [##############------]  71%
```

That is a real HTTP download rendering in real time.

---

## 3. What works well (the actual use case)

Python development is where ZMUX is strong, and it is genuinely good:

| Capability | Verified |
|---|---|
| stdlib: `json`, `math`, `os`, `sys`, `sqlite3`, `urllib` | all import |
| Real database | `sqlite3` in-memory table → `(7,)` |
| Real network | `urllib.request.urlopen('https://pypi.org/...')` → `200` |
| Bigint / recursion | `f(20)` → `2432902008176640000` |
| Real tracebacks | `1/0` → `ZeroDivisionError` |
| Persistent state | `f` defined once, callable many lines later |
| Multiline blocks | `for i in range(2):` → `...` continuation → executes |
| History | arrow-up recall |
| Line editing | backspace |
| **Ctrl+C on a runaway loop** | interrupts a 200-iteration sleep loop |
| `input()` prompts | visible *before* the read blocks |
| Script workflow | write `app.py`, run `python app.py alpha beta`, argv arrives |
| Multi-session | 8 tabs, isolated globals/cwd/history, background execution |
| `zmux-info` | reports real `Runtime ID`, ABI, pointer width, p4a commit |

**Impact:** on a phone with no PC nearby, this is a legitimate Python
scratchpad — parse JSON, query SQLite, hit an API, run a script, keep notes in
`~/.zmuxrc`. For that, it works.

---

## 4. What does NOT work — including three real bugs

### 4.1 No shell language (by design, but under-communicated)

| Syntax | bash | ZMUX | Failure mode |
|---|---|---|---|
| `cmd1 && cmd2` | runs both | **first only** | 🔴 **silent** — reports success |
| `cmd1; cmd2` | runs both | **first only**, `;` passed as an argument | 🔴 **silent** |
| `$HOME` | expands | literal `$HOME` | 🟠 visibly wrong |
| `$(cmd)` | substitutes | literal `$(cmd)` | 🟠 visibly wrong |
| `*.py` | globs | literal `*.py` → "No such file" | 🟠 errors |
| `~` | `$HOME` | literal `~` → "No such file" | 🟠 errors |
| `cmd &` | backgrounds | `&` becomes an argument | 🟠 errors |
| `2>&1` | merges streams | **creates a file named `&1`** | 🔴 **silent + litters FS** |
| `\|`, `>`, `>>`, `<`, quotes | ✅ | ✅ | works |

### 🔴 BUG 1 — `&&` reports success while doing nothing

```
$ /bin/true && /bin/touch built.txt
exit code shown to user : 0        ← looks like success
built.txt created?      : False    ← second command never ran
```

`argv` becomes `['/bin/true','&&','/bin/touch','built.txt']`; `/bin/true`
ignores its arguments and exits 0. **The user is told the build succeeded and
gets nothing.** This is the single most dangerous behaviour found — muscle
memory produces `&&` constantly.

### 🔴 BUG 2 — `2>&1` silently creates a junk file

```
$ /bin/sh -c 'echo out; echo err >&2' 2>&1
stdout        : ''
files created : ['&1']      ← a real file, literally named "&1"
```

The redirection parser sees `2>` `&1` and writes to a file called `&1`. It also
swallows the output. I hit this accidentally during testing and left a stray
`&1` file in the home directory — that is how easy it is to trigger.

### 🔴 BUG 3 — `cd` is ignored by Python code (worst of the three)

The shell tracks its own `cwd` variable, but **never calls `os.chdir()`**.
Subprocesses get `cwd=` explicitly, so *they* obey it. In-process Python does not.

```
$ mkdir -p workdir
$ cd workdir
$ pwd
/…/home/workdir                          ← the terminal says you are here

$ open('made_by_python.txt','w').write('data')     # Python
   file landed in cd'd dir? False
   landed in process cwd?   True        ← somewhere else entirely

$ /usr/bin/touch made_by_subprocess.txt            # subprocess
   landed in cd'd dir? True

$ ls
made_by_subprocess.txt                   ← the Python file is INVISIBLE

$ cat made_by_python.txt
cat: [Errno 2] No such file or directory
```

**You write a file in Python, and `ls` cannot see it. `cat` cannot read it.**
Two filesystems' worth of confusion in one session — and ZMUX is a *Python*
terminal, so this is squarely on the main path.

### 🔴 BUG 4 — `zpip install rich` fails on a dependency name

```
$ zpip install rich
mdurl-0.1.2          0.0 MiB 272.3 KiB/s [####################] 100%
markdown-it-py-...   0.1 MiB 1410.6 KiB/s [##############------] 71%
Error: Dependency markdown-it-py failed: Smoke import failed:
ModuleNotFoundError: No module named 'markdown_it_py'
```

`_import_name()` guesses the module by `name.replace("-","_")` plus a
five-entry alias table. Measured against real wheels:

| PyPI name | zpip guess | actual top-level | |
|---|---|---|---|
| `attrs` | `attrs` | `attr`, `attrs` | OK |
| `typing-extensions` | `typing_extensions` | `typing_extensions` | OK |
| `markdown-it-py` | `markdown_it_py` | **`markdown_it`** | ❌ |
| `python-dateutil` | `python_dateutil` | **`dateutil`** | ❌ |

The correct value is *inside the wheel* (top-level directory, or the
`top_level.txt`/`RECORD` metadata). Guessing means **`rich` — the package the
codebase itself recommends — cannot be installed.** The transaction rolled back
cleanly, which is the system working as designed, but the install is blocked.

### 4.2 Typos produce Python tracebacks

```
$ gti status              # typo of "git"
  SyntaxError: invalid syntax
```

bash says `command not found`. ZMUX falls through to the Python evaluator, so a
mistyped command yields a confusing `SyntaxError`.

### 4.3 No TUI, confirmed

```
/usr/bin/top -n1  → exit=1  'top: failed tty get'
```

Exactly as documented. `vim`, `htop`, `less`, `nano` will not run. Also
`isatty()` is `False` for children, so tools that colourise only for TTYs stay
monochrome.

### 4.4 Built-in `ls` accepts flags it ignores

`ls -R`, `ls -t`, `ls --color` all return `exit=0` and plain output — the flags
are silently discarded rather than rejected.

> **Update (2026-07-31): fixed.** `ls` now implements `-a -l -R -t -r` (with
> GNU-style headers for recursive/multi-operand output) and rejects unknown
> flags loudly (`ls: invalid option -- 'Z'`, exit 1). `ls -R` and `ls -t` are
> no longer silent failures; the strictness principle now covers `ls` the way
> it covers the shell-operator guards.

---

## 5. ZMUX vs bash — honest scorecard

| | bash (my sandbox) | ZMUX |
|---|---|---|
| Real processes, exit codes, signals | ✅ | ✅ |
| Pipelines `\|` | ✅ | ✅ |
| Redirection `>` `>>` `<` | ✅ | ✅ |
| `2>&1` | ✅ | ❌ creates a file `&1` |
| `&&` `\|\|` `;` | ✅ | ❌ silently ignored |
| Glob / `$VAR` / `$(...)` / `~` | ✅ | ❌ |
| Background jobs, job control | ✅ | ❌ |
| Functions, loops, `if`, scripts | ✅ | ❌ (use Python instead) |
| TUI programs | ✅ | ❌ |
| `cd` affects everything | ✅ | ⚠️ subprocesses only |
| Tab completion | ✅ | ❌ |
| **Python REPL built in** | ❌ | ✅ |
| **Persistent interpreter state** | ❌ | ✅ |
| **Hash-verifying package manager** | ❌ | ✅ |
| **Runs on a phone, no root, 1 permission** | ❌ | ✅ |

**Rough capability share: ZMUX covers ~40 % of everyday bash surface, and adds a
Python environment bash does not have.**

Versus **Termux** (a real PTY + full Linux userland): Termux wins outright as a
shell. ZMUX's honest niche is *Python-first, tiny, reproducible, one
permission, no bootstrap download*.

---

## 6. Verdict

**Not a fake.** Every output shown above came from a real process or the real
CPython runtime. There is no mocking, no canned output, no bundle-wrapping.

**Not a shell either.** The gap is not "some polish missing" — it is a missing
shell *language*, and today the missing pieces fail **silently**, which is worse
than failing loudly. A user who types `make && ./run` is told it worked.

### Recommended fixes, in priority order

1. **`cd` must call `os.chdir()`** (BUG 3). One line; removes the split-brain
   filesystem. Highest damage-to-effort ratio.
2. **Reject unsupported operators loudly** — `&&`, `||`, `;`, `&`, `2>&1`
   should print `zmux: '&&' is not supported (no shell language)` and exit
   non-zero. Turns three silent traps into honest errors. *Cheap.*
3. **Read the real module name from wheel metadata** (BUG 4) instead of
   guessing; unblocks `rich` and any hyphenated package.
4. **`command not found` for unknown words** instead of a Python `SyntaxError`.
5. Then consider `$VAR`, `~` and glob expansion — the three that would close
   most of the remaining gap.

Items 1–4 are small and mostly mechanical. With them done, ZMUX would be an
honest, pleasant Python terminal whose limits are visible rather than hidden.
Until then, the most accurate one-line description is:

> **A real Python runtime with a real command runner — not a shell, and it
> currently fails silently when you treat it like one.**
