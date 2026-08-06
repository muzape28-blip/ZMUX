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
            fw.write("echo '(Type " + esc + "[34mlinux-setup" + esc + "[0m to install Alpine or Debian)'\n");
            fw.write("echo '" + esc + "[33m=================================================" + esc + "[0m'\n");
            fw.write("export PS1='" + esc + "[32mzmux" + esc + "[0m~" + esc + "[34m:" + esc + "[0m$ '\n");
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
            fws.write("echo '1) Alpine Linux (Lightweight, APK)'\n");
            fws.write("echo '2) Debian (Robust, APT)'\n");
            fws.write("echo '3) Cancel'\n");
            fws.write("echo ''\n");
            fws.write("printf 'Choose [1/2/3]: '\n");
            fws.write("read choice\n");
            fws.write("if [ \"$choice\" = \"1\" ]; then\n");
            fws.write("    echo ''\n");
            fws.write("    echo '" + esc + "[32m[*]" + esc + "[0m Triggering Alpine Linux installation...'\n");
            fws.write("    rm -f \"$HOME/.setup_done\"\n");
            fws.write("    printf \"\\033]0;INSTALL_ALPINE\\007\"\n");
            fws.write("    while [ ! -f \"$HOME/.setup_done\" ]; do sleep 0.5; done\n");
            fws.write("    rm -f \"$HOME/.setup_done\"\n");
            fws.write("    printf \"\\033]0;ZMUX\\007\"\n");
            fws.write("elif [ \"$choice\" = \"2\" ]; then\n");
            fws.write("    echo ''\n");
            fws.write("    echo '" + esc + "[32m[*]" + esc + "[0m Triggering Debian installation...'\n");
            fws.write("    rm -f \"$HOME/.setup_done\"\n");
            fws.write("    printf \"\\033]0;INSTALL_DEBIAN\\007\"\n");
            fws.write("    while [ ! -f \"$HOME/.setup_done\" ]; do sleep 0.5; done\n");
            fws.write("    rm -f \"$HOME/.setup_done\"\n");
            fws.write("    printf \"\\033]0;ZMUX\\007\"\n");
            fws.write("else\n");
            fws.write("    echo 'Cancelled.'\n");
            fws.write("fi\n");
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
     */
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

        // DNS: keep the guest on the host resolver when Android has one,
        // exactly like the Python launcher (linuxenv._bind_flags).
        java.io.File resolv = new java.io.File("/etc/resolv.conf");
        if (resolv.isFile()) {
            args.add("-b");
            args.add("/etc/resolv.conf:/etc/resolv.conf");
        }

        // libtalloc SONAME self-heal (see ensureTallocCompat above).
        String ldLibraryPath = nativeLibraryDir;
        String compatDir = ensureTallocCompat(nativeLibraryDir, filesDir);
        if (compatDir != null) {
            ldLibraryPath = compatDir + ":" + ldLibraryPath;
        }

        String loader = new java.io.File(nativeLibraryDir, "libproot-loader.so").getAbsolutePath();
        args.add("-w");
        args.add("/root");
        args.add("/bin/sh");
        args.add("-l");

        String[] env = new String[] {
            "HOME=/root",
            "USER=zmux",
            "LOGNAME=zmux",
            "SHELL=/bin/sh",
            "TERM=xterm-256color",
            "LANG=C.UTF-8",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PS1=zmux@linux:\\w$ ",
            "LD_LIBRARY_PATH=" + ldLibraryPath,
            "PROOT_LOADER=" + loader,
            "PROOT_TMP_DIR=" + cache.getAbsolutePath(),
        };
        return new TerminalSession(
            prootPath,
            filesDir,
            args.toArray(new String[0]),
            env,
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

    public static int getColumns(TerminalEmulator emulator) {
        if (emulator == null) return 80;
        try {
            Field f = TerminalEmulator.class.getDeclaredField("mColumns");
            f.setAccessible(true);
            return f.getInt(emulator);
        } catch (Exception e) {
            return 80;
        }
    }

    public interface ProgressCallback {
        void invoke(String msg);
    }
    public static int getRows(TerminalEmulator emulator) {
        if (emulator == null) return 24;
        try {
            Field f = TerminalEmulator.class.getDeclaredField("mRows");
            f.setAccessible(true);
            return f.getInt(emulator);
        } catch (Exception e) {
            return 24;
        }
    }
}
