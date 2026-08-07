package com.termux.terminal;

import java.lang.reflect.Field;

public class TerminalSessionHelper {

    /**
     * Android 10+ (targetSdk 29+, this APK targets 34) hard-blocks execve()
     * on every regular file in the app-private data directory — the SELinux
     * "W^X" rule. chmod 0755 does NOT help; the kernel answers EACCES and
     * mksh prints "<file>: Permission denied". The Python backend still
     * materializes its CLI wrappers under files/bin for interpreter-mode use
     * (`sh <path>` reads the file instead of executing it), so the bootstrap
     * shell must:
     *
     *   1. NEVER put the app-private bin directory on PATH. Any lookup that
     *      lands on files/bin/* — including `clear`, which shadows the real
     *      /system/bin/clear toybox applet — dead-ends in "Permission
     *      denied". With a system-only PATH every toybox command resolves
     *      normally.
     *   2. Reach files/bin/zmux-linux-setup through `sh <path>` (an interpreter
     *      *reads* the script; no exec permission is needed). The `zmux-`
     *      prefix keeps this host-owned script clear of the wrapper names
     *      zmux.paths generates into the same directory.
     */
    public static TerminalSession createLocalSession(TerminalSessionClient client, String filesDir) {
        java.io.File dir = new java.io.File(filesDir);
        if (!dir.exists()) {
            dir.mkdirs();
        }

        java.io.File binDir = new java.io.File(dir, "bin");
        if (!binDir.exists()) {
            binDir.mkdirs();
        }

        try {
            java.io.File rc = new java.io.File(dir, ".zmuxrc");
            java.io.FileWriter fw = new java.io.FileWriter(rc);
            // Shell bawaan Android (mksh) butuh karakter escape beneran, bukan string "\033".
            // Biar gampang dan pasti jalan, kita pake prompt bersih atau inject char escape.
            char esc = (char) 27;
            // `clear` now resolves to /system/bin/clear (toybox). PATH below is
            // system-only on purpose — see the W^X contract above.
            fw.write("clear; printf \"\\033[3J\"\n");
            fw.write("echo '" + esc + "[33m=================================================" + esc + "[0m'\n");
            fw.write("echo '" + esc + "[32mWELCOME TO ZMUX, FEEL FREE TO EXEC COMMAND..." + esc + "[0m'\n");
            fw.write("echo ''\n");
            fw.write("echo '(Type " + esc + "[34mlinux-setup" + esc + "[0m to install Alpine Linux)'\n");
            fw.write("echo '" + esc + "[33m=================================================" + esc + "[0m'\n");
            // mksh prompt-width contract (see mksh(1), PS1): the line editor
            // counts printable characters to know where the screen edge is,
            // so raw colour escape codes make it believe the prompt is ~31
            // columns wider than it looks and commands wrap a few letters
            // early ("command kepotong pindah baris"). The ksh88 convention:
            // start with <DELIM><CR> and bracket every non-printing sequence
            // between two DELIMs; inside pairs, nothing is printed or
            // counted. mksh has no \w — use $PWD, which mksh re-expands on
            // every prompt. \001 is the conventional, already-unused DELIM.
            fw.write("export PS1='\001\r\001" + esc + "[1;38;5;202m\001ZMUX\001" + esc + "[0m\001:\001" + esc + "[38;5;80m\001$PWD\001" + esc + "[0m\001$ '\n");
            fw.write("alias ls='ls --color=auto'\n");
            fw.write("alias clear='clear; printf \"\\033[3J\"'\n");
            // Android 10+ W^X: files/bin/* can never be execve()'d directly, so
            // run the setup script through the `sh` interpreter, which only
            // needs to *read* the file. This is the only files/bin reference
            // allowed in this rc — everything else resolves against /system.
            //
            // The filename is deliberately host-owned and distinct from every
            // name in zmux.command_registry.WRAPPER_COMMANDS. Now that APP_DIR
            // is aligned with filesDir, zmux.paths.ensure_cli_wrappers() writes
            // its generated wrappers into this very directory; sharing the name
            // "linux-setup" would let the Python wrapper (which needs a
            // `python` on PATH that Chaquopy does not provide) replace this
            // menu. Gate: KotlinHostFileOwnershipTests in
            // tests/test_app_dir_alignment.py.
            fw.write("alias linux-setup='sh " + shellQuote(binDir.getAbsolutePath() + "/zmux-linux-setup") + "'\n");
            fw.close();

            java.io.File setup = new java.io.File(binDir, "zmux-linux-setup");
            java.io.FileWriter fws = new java.io.FileWriter(setup);
            fws.write("#!/system/bin/sh\n");
            fws.write("echo '" + esc + "[33m=================================================" + esc + "[0m'\n");
            fws.write("echo '" + esc + "[32m ZMUX Linux Setup" + esc + "[0m'\n");
            fws.write("echo '" + esc + "[33m=================================================" + esc + "[0m'\n");
            fws.write("echo 'This will download and install Alpine Linux 3.22 (~3 MiB).'\n");
            fws.write("echo ''\n");
            fws.write("printf 'Install Alpine now? [y/N]: '\n");
            fws.write("IFS= read -r choice\n");
            fws.write("case \"$choice\" in\n");
            fws.write("    y|Y)\n");
            fws.write("        echo ''\n");
            fws.write("        echo '" + esc + "[32m[*]" + esc + "[0m Triggering Alpine Linux installation...'\n");
            fws.write("        rm -f \"$HOME/.setup_done\"\n");
            fws.write("        printf \"\\033]0;INSTALL_ALPINE\\007\"\n");
            fws.write("        while [ ! -f \"$HOME/.setup_done\" ]; do sleep 0.5; done\n");
            fws.write("        rm -f \"$HOME/.setup_done\"\n");
            fws.write("        printf \"\\033]0;ZMUX\\007\"\n");
            fws.write("        ;;\n");
            fws.write("    *)\n");
            fws.write("        echo 'Cancelled.'\n");
            fws.write("        ;;\n");
            fws.write("esac\n");
            fws.close();

            // W^X-safe regardless: the alias above invokes this through `sh`,
            // which only reads the file. setExecutable is kept best-effort for
            // runtimes where direct execution is allowed.
            setup.setExecutable(true, false);
            setup.setReadable(true, false);
        } catch (Exception e) {
            e.printStackTrace();
        }

        String[] env = new String[] {
            "HOME=" + filesDir,
            // System-only PATH: nothing executable can ever live in the
            // app-private directory (Android 10+ W^X), so files/bin must not
            // appear here at any position — a failed lookup would be reported
            // by mksh as "Permission denied" instead of falling through.
            "PATH=/system/bin:/system/xbin:/vendor/bin",
            "ENV=" + filesDir + "/.zmuxrc"
        };
        // The 5th TerminalSession argument is transcriptRows — the scrollback
        // buffer line count, NOT the PTY width. The PTY size always comes
        // from TerminalView.updateSize() -> session.updateSize() (TIOCSWINSZ)
        // after first layout, so this value never affects editing/wrapping.
        // 2000 lines of scrollback matches Termux's own default: 80 lines
        // was far too puny on a real phone screen.
        return new TerminalSession("/system/bin/sh", filesDir, new String[0], env, 2000, client);
    }

    /** Quote a path for embedding inside a POSIX sh command line. */
    private static String shellQuote(String value) {
        return "'" + value.replace("'", "'\\''") + "'";
    }

    /**
     * True when a guest file exists, resolving symlink chains INSIDE the
     * rootfs. Alpine links /bin/sh -> /bin/busybox absolutely; checking that
     * link from the host resolves against the host's missing /bin/busybox and
     * an installed environment would look broken (the "restarts into the
     * bootstrap shell" bug). Mirrors linuxenv._guest_regular_file.
     */
    public static boolean guestRegularFile(java.io.File root, String guestPath) {
        try {
            java.io.File rootAbs = root.getCanonicalFile();
            java.io.File candidate = new java.io.File(rootAbs, guestPath);
            for (int hops = 0; hops < 16; hops++) {
                if (candidate.isFile()) {
                    return true;
                }
                String link;
                try {
                    link = java.nio.file.Files.readSymbolicLink(candidate.toPath()).toString();
                } catch (Exception notASymlink) {
                    return false;
                }
                if (link.startsWith("/")) {
                    candidate = new java.io.File(rootAbs, link.substring(1));
                } else {
                    candidate = new java.io.File(candidate.getParentFile(), link);
                }
                candidate = candidate.getCanonicalFile();
                if (!candidate.getPath().startsWith(rootAbs.getPath() + java.io.File.separator)
                        && !candidate.equals(rootAbs)) {
                    return false; // would escape the rootfs
                }
            }
        } catch (Exception ignored) {
        }
        return false;
    }

    /**
     * Mirror libtalloc under the SONAME filename the linker asks for.
     *
     * Older APKs ship DT_NEEDED entries that do not match the packaged
     * filename (libtalloc.so vs libtalloc.so.2), and nativeLibraryDir is
     * read-only. Mirrors linuxenv._ensure_talloc_compat so a PRoot launch
     * straight from Kotlin (the auto-reopen path, no Python in the loop)
     * self-heals exactly like the Python install path. Returns the directory
     * to prepend to LD_LIBRARY_PATH, or null when nothing was needed.
     */
    private static String ensureTallocCompat(String nativeLibraryDir, String filesDir) {
        try {
            java.io.File nativeDir = new java.io.File(nativeLibraryDir);
            java.io.File runtimeDir = new java.io.File(filesDir, "lib");
            java.io.File srcPlain = new java.io.File(nativeDir, "libtalloc.so");
            java.io.File srcV2 = new java.io.File(nativeDir, "libtalloc.so.2");
            java.io.File wantPlain = new java.io.File(runtimeDir, "libtalloc.so");
            java.io.File wantV2 = new java.io.File(runtimeDir, "libtalloc.so.2");
            if ((srcPlain.isFile() || srcV2.isFile()) && (!wantPlain.isFile() || !wantV2.isFile())) {
                runtimeDir.mkdirs();
                if (srcPlain.isFile() && !wantV2.isFile()) {
                    java.nio.file.Files.copy(srcPlain.toPath(), wantV2.toPath());
                }
                if (srcV2.isFile() && !wantPlain.isFile()) {
                    java.nio.file.Files.copy(srcV2.toPath(), wantPlain.toPath());
                }
            }
            if (wantV2.isFile() || wantPlain.isFile()) {
                return runtimeDir.getAbsolutePath();
            }
        } catch (Exception ignored) {
            // A failed mirror must never block the PRoot launch; the linker's
            // own diagnostics remain the source of truth if talloc is missing.
        }
        return null;
    }

    /**
     * Create the actual guest terminal after linux-setup succeeds.
     *
     * Android only permits executing app-owned native code from
     * nativeLibraryDir, hence PRoot itself is launched from libproot.so there.
     * PRoot then enters the app-private rootfs and execs guest /bin/sh through
     * the same kernel PTY used by TerminalSession — guest binaries are mapped
     * via ptrace+mmap, so the host W^X rule never applies inside the guest.
     * Device files are supplied by PRoot binds; rootfs extraction intentionally
     * leaves them out because an app UID cannot create mknod entries.
     *
     * Heal the guest's /etc/resolv.conf before launch.
     *
     * We used to bind-mount the Android host /etc/resolv.conf into the guest.
     * On many devices that file points at a loopback stub (127.0.0.1 / ::1)
     * the PRoot guest cannot reach, so `apk add` hung on the first DNS
     * lookup. Now the guest gets its own resolv.conf built from any
     * reachable host nameserver plus public IPv4 DNS fallbacks (IPv6 is
     * intentionally omitted: some carriers hand out v6 with no route). This
     * also repairs rootfses installed before the fix.
     */
    private static void ensureGuestResolvConf(java.io.File rootfs) {
        try {
            java.io.File etc = new java.io.File(rootfs, "etc");
            etc.mkdirs();

            java.util.LinkedHashSet<String> servers = new java.util.LinkedHashSet<>();
            java.io.File hostResolv = new java.io.File("/etc/resolv.conf");
            if (hostResolv.isFile()) {
                for (String line : java.nio.file.Files.readAllLines(hostResolv.toPath())) {
                    String[] parts = line.trim().split("\\s+");
                    if (parts.length >= 2 && parts[0].equalsIgnoreCase("nameserver")) {
                        String ip = parts[1];
                        int pct = ip.indexOf('%');
                        if (pct >= 0) ip = ip.substring(0, pct);
                        String low = ip.toLowerCase();
                        boolean unusable = low.equals("::1") || low.equals("localhost")
                                || low.startsWith("127.") || low.contains(":");
                        // IPv4 only: skip loopback and IPv6 addresses. Some
                        // carriers hand out v6 DNS with no working route,
                        // which made apk hang on name resolution.
                        if (!unusable) servers.add(ip);
                    }
                }
            }
            // Public IPv4 fallbacks — always present so a broken/absent host
            // resolver never strands the guest.
            servers.add("8.8.8.8");
            servers.add("1.1.1.1");

            StringBuilder sb = new StringBuilder();
            sb.append("# Generated by ZMUX — do not rely on the Android host resolver.\n");
            for (String s : servers) sb.append("nameserver ").append(s).append('\n');
            sb.append("options timeout:2 attempts:2\n");
            java.nio.file.Files.write(
                    new java.io.File(etc, "resolv.conf").toPath(),
                    sb.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Exception ignored) {
            // A failed repair must never block launching the shell; DNS
            // diagnostics remain the source of truth.
        }
    }

    /**
     * Drop the branded PS1 into /etc/profile.d so it wins over Alpine's
     * default. Busybox ash sources /etc/profile on a login shell, which sets
     * a hostname-based prompt ("localhost:~#") and then sources every
     * /etc/profile.d/*.sh, so our file overrides it regardless of $HOME.
     */
    private static void ensureGuestPrompt(java.io.File rootfs) {
        try {
            java.io.File profileD = new java.io.File(rootfs, "etc/profile.d");
            profileD.mkdirs();
            // The \033 octal text survives single quotes; busybox ash
            // (bb_process_escape_sequence in lineedit.c) and bash turn it into
            // ESC when the prompt is drawn.
            String esc = "\\033";
            // Bracket every non-printing sequence in \[ \]. Busybox ash
            // line editing (parse_and_put_prompt) and bash/readline honour only
            // these markers; without them ash counts the 31 invisible bytes of
            // the colour codes as prompt width, so on a ~38-column phone every
            // long command wraps a few letters too early (the wrap bug).
            String ps1 = "\\[" + esc + "[1;38;5;202m\\]ZMUX"
                    + "\\[" + esc + "[0m\\]:"
                    + "\\[" + esc + "[38;5;80m\\]\\w"
                    + "\\[" + esc + "[0m\\]\\$ ";
            java.nio.file.Files.write(
                    new java.io.File(profileD, "zmux-prompt.sh").toPath(),
                    ("# Managed by ZMUX — branded prompt.\n"
                            + "PS1='" + ps1 + "'\n"
                            + "export PS1\n")
                            .getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Exception ignored) {
        }
    }

    /**
     * Drop a small static MOTD into /etc/profile.d. Pure echo/cat, no extra
     * processes or network calls. The guard on SHLVL keeps it off nested
     * shells so it appears once per login.
     */
    private static void ensureGuestMotd(java.io.File rootfs) {
        try {
            java.io.File profileD = new java.io.File(rootfs, "etc/profile.d");
            profileD.mkdirs();
            String content =
                    "# Managed by ZMUX — first-login banner.\n"
                    + "if [ \"$SHLVL\" = \"1\" ]; then\n"
                    + "  printf '\\033[1;38;5;202m'\n"
                    + "  cat <<'BANNER'\n"
                    + "  ____  __  __ _   _ __  __\n"
                    + " |_  / |  \\/  | | | |\\ \\/ /\n"
                    + "  / /  | |\\/| | |_| | >  < \n"
                    + " /___| |_|  |_|\\___/ /_/\\_\\\n"
                    + "BANNER\n"
                    + "  printf '\\033[0m\\033[38;5;80m  Alpine %s\\033[0m\\n' \"$(cat /etc/alpine-release 2>/dev/null)\"\n"
                    + "  printf '\\033[90m  type \\033[36mapk add <pkg>\\033[90m to install packages\\033[0m\\n'\n"
                    + "  printf '\\033[90m  stuck command? run \\033[36mzmux-doctor\\033[90m\\033[0m\\n'\n"
                    + "  printf '\\n'\n"
                    + "fi\n";
            java.nio.file.Files.write(
                    new java.io.File(profileD, "zmux-motd.sh").toPath(),
                    content.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Exception ignored) {
        }
    }

    /**
     * Install /usr/local/bin/zmux-doctor inside the guest. It answers the
     * "commands hang silently after install" class. Every probe is a DIRECT
     * executable under a 3s timeout — no `sh -c` wrapper (on broken 32-bit
     * kernels that wrapper itself dies with ENOSYS and hides the evidence).
     * Printed DIAG lines classify the signature: HANG (rc=124) vs
     * EXEC-DEPTH-ENOSYS. Must mirror linuxenv._write_guest_doctor.
     */
    private static void ensureGuestDoctor(java.io.File rootfs) {
        try {
            java.io.File binDir = new java.io.File(rootfs, "usr/local/bin");
            binDir.mkdirs();
            java.io.File target = new java.io.File(binDir, "zmux-doctor");
            String content =
                    "#!/bin/sh\n"
                    + "# Managed by ZMUX — guest self-diagnostics for the 'commands hang\n"
                    + "# silently' class. The LAST [doctor] RUN: line before a freeze plus\n"
                    + "# its DIAG line is the diagnosis — screenshot it and report.\n"
                    + "say() { printf '\\033[36m[doctor]\\033[0m %s\\n' \"$1\"; }\n"
                    + "runi() { say \"RUN: $1\"; out=$(timeout 3 \"$@\" 2>&1); rc=$?; printf '%s\\n' \"$out\" | head -3 | sed 's/^/    /'; case \"$out\" in *\"Function not implemented\"*) say \"DIAG: EXEC-DEPTH-ENOSYS (rc=$rc) — deeper fork/exec syscall path broken\";; *) case \"$rc\" in 124) say \"DIAG: HANG (rc=124) — froze inside this syscall path\";; *) say \"rc=$rc\";; esac;; esac; }\n"
                    + "say \"kernel   : $(uname -a 2>&1)\"\n"
                    + "say \"machine  : $(uname -m 2>&1)\"\n"
                    + "say \"alpine   : $(cat /etc/alpine-release 2>&1)\"\n"
                    + "say \"musl     : $(ls /lib/ld-musl-*.so.1 2>/dev/null | head -1)\"\n"
                    + "say \"resolv.conf:\"\n"
                    + "sed 's/^/    /' /etc/resolv.conf 2>&1\n"
                    + "runi stat /etc/os-release\n"
                    + "runi dd if=/dev/urandom bs=8 count=1\n"
                    + "runi nslookup dl-cdn.alpinelinux.org\n"
                    + "runi sh -c true\n"
                    + "runi timeout 2 true\n"
                    + "runi apk --version\n"
                    + "say \"hint: deep syscall breadcrumbs: ZMUX proot debug mode (touch ~/.zmux_proot_debug)\"\n";
            java.nio.file.Files.write(target.toPath(),
                    content.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            target.setExecutable(true, false);
        } catch (Exception ignored) {
        }
    }

    public static TerminalSession createLinuxSession(
            TerminalSessionClient client,
            String filesDir,
            String prootPath,
            String nativeLibraryDir,
            String rootfsDir,
            String homeDir) {
        java.io.File rootfs = new java.io.File(rootfsDir);
        java.io.File home = new java.io.File(homeDir);
        java.io.File cache = new java.io.File(filesDir, "cache");
        home.mkdirs();
        new java.io.File(home, "projects").mkdirs();
        cache.mkdirs();

        // Heal DNS, prompt and motd BEFORE building argv/proot. This is what
        // fixes the apk hang caused by the host resolv.conf loopback stub
        // shadowing the guest.
        ensureGuestResolvConf(rootfs);
        ensureGuestPrompt(rootfs);
        ensureGuestMotd(rootfs);
        ensureGuestDoctor(rootfs);

        java.util.List<String> args = new java.util.ArrayList<>(java.util.Arrays.asList(
            "--kill-on-exit",
            "--link2symlink",
            "--sysvipc",
            "-0",
            "-r", rootfs.getAbsolutePath(),
            "-b", "/dev",
            "-b", "/proc",
            "-b", "/sys",
            "-b", home.getAbsolutePath() + ":/root"
        ));

        // Shared-storage binds: only paths that actually exist AND are
        // readable from this UID. Binding an inaccessible location would make
        // PRoot report its own permission errors inside the guest — the exact
        // failure class this build eliminates. Mirrors
        // linuxenv._storage_bind_paths + _ensure_guest_mountpoint.
        String[] storageCandidates = new String[] {
            System.getenv("EXTERNAL_STORAGE"),
            System.getenv("ANDROID_STORAGE"),
            "/sdcard",
            "/storage/emulated/0",
        };
        java.util.List<String> bound = new java.util.ArrayList<>();
        for (String candidate : storageCandidates) {
            if (candidate == null || candidate.isEmpty() || bound.contains(candidate)) {
                continue;
            }
            java.io.File path = new java.io.File(candidate);
            if (!path.isDirectory() || !path.canRead() || !path.canExecute()) {
                continue;
            }
            // PRoot needs the mountpoint to exist inside the rootfs.
            new java.io.File(rootfs, candidate.substring(1)).mkdirs();
            args.add("-b");
            args.add(candidate + ":" + candidate);
            bound.add(candidate);
        }

        // DNS is configured by ensureGuestResolvConf writing the guest's own
        // /etc/resolv.conf. We deliberately do NOT bind-mount the Android
        // host's resolv.conf here (it is often a 127.0.0.1 stub the guest
        // cannot reach — the root cause of the "Temporary failure resolving"
        // and `apk add` hang reports).

        // libtalloc SONAME self-heal (see ensureTallocCompat above).
        String ldLibraryPath = nativeLibraryDir;
        String compatDir = ensureTallocCompat(nativeLibraryDir, filesDir);
        if (compatDir != null) {
            ldLibraryPath = compatDir + ":" + ldLibraryPath;
        }

        // Opt-in syscall tracing for the "commands hang silently" class.
        // `touch ~/.zmux_proot_debug` in the bootstrap shell, reopen the
        // Linux tab, and proot's verbose breadcrumbs (-v 9) print straight
        // into the terminal: the syscall stream right before the freeze is
        // the diagnosis. Delete the file to go back to a quiet session.
        boolean prootDebug = new java.io.File(filesDir, ".zmux_proot_debug").isFile();

        String loader = new java.io.File(nativeLibraryDir, "libproot-loader.so").getAbsolutePath();
        if (prootDebug) {
            args.add("-v");
            args.add("9");
        }
        args.add("-w");
        args.add("/root");
        args.add("/bin/sh");
        args.add("-l");

        // Fallback prompt only. The real prompt is installed at
        // /etc/profile.d/zmux-prompt.sh by ensureGuestPrompt so it wins over
        // Alpine's /etc/profile default. Same \[ \] contract as that file:
        // busybox ash/bash line editors must not count the 31 invisible
        // bytes of the colour codes, or long commands wrap a few letters
        // early on phone-width screens.
        String ps1 = "\\[\033[1;38;5;202m\\]ZMUX\\[\033[0m\\]:\\[\033[38;5;80m\\]\\w\\[\033[0m\\]\\$ ";

        String[] env = new String[] {
            "HOME=/root",
            "USER=zmux",
            "LOGNAME=zmux",
            "SHELL=/bin/sh",
            "TERM=xterm-256color",
            "LANG=C.UTF-8",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PS1=" + ps1,
            "LD_LIBRARY_PATH=" + ldLibraryPath,
            "PROOT_LOADER=" + loader,
            "PROOT_TMP_DIR=" + cache.getAbsolutePath(),
            // Avoid SIGSYS ("Bad system call") on kernels whose seccomp
            // filter rejects proot's accelerator (Android 14/15).
            "PROOT_NO_SECCOMP=1",
        };
        // 80x24 default; real cols/rows are propagated from TerminalView
        // after layout via updateSize(). See createLocalSession() for why
        // this must not be 2000.
        return new TerminalSession(
            prootPath,
            filesDir,
            args.toArray(new String[0]),
            env,
            // transcriptRows (scrollback lines), not PTY size — see
            // createLocalSession. Real cols/rows arrive via TIOCSWINSZ.
            2000,
            client
        );
    }

    public static TerminalEmulator getEmulator(TerminalSession session) {
        try {
            Field f = TerminalSession.class.getDeclaredField("mEmulator");
            f.setAccessible(true);
            return (TerminalEmulator) f.get(session);
        } catch (Exception e) {
            return null;
        }
    }

    public interface ProgressCallback {
        void invoke(String msg);
    }
}
