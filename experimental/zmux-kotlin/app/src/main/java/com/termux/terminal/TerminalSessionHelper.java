package com.termux.terminal;

import java.lang.reflect.Field;
import java.lang.reflect.Method;

public class TerminalSessionHelper {
    public static void injectEmulator(TerminalSession session, TerminalEmulator emulator) {
        try {
            Field emulatorField = TerminalSession.class.getDeclaredField("mEmulator");
            emulatorField.setAccessible(true);
            emulatorField.set(session, emulator);

            Field pidField = TerminalSession.class.getDeclaredField("mShellPid");
            pidField.setAccessible(true);
            pidField.set(session, 1);
        } catch (Exception e) {
            e.printStackTrace();
        }
    }
    
    public static int readQueue(TerminalSession session, byte[] buffer, boolean block) {
        try {
            Field queueField = TerminalSession.class.getDeclaredField("mTerminalToProcessIOQueue");
            queueField.setAccessible(true);
            Object queue = queueField.get(session);
            if (queue != null) {
                Method readMethod = queue.getClass().getDeclaredMethod("read", byte[].class, boolean.class);
                readMethod.setAccessible(true);
                return (Integer) readMethod.invoke(queue, buffer, block);
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return -1;
    }

    public static void closeQueue(TerminalSession session) {
        try {
            Field queueField = TerminalSession.class.getDeclaredField("mTerminalToProcessIOQueue");
            queueField.setAccessible(true);
            Object queue = queueField.get(session);
            if (queue != null) {
                Method closeMethod = queue.getClass().getDeclaredMethod("close");
                closeMethod.setAccessible(true);
                closeMethod.invoke(queue);
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
    }

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
            fw.write("export PS1='" + esc + "[32mzmux" + esc + "[0m~" + esc + "[34m:" + esc + "[0m$ '\n");
            fw.write("alias ls='ls --color=auto'\n");
            fw.write("alias clear='clear; printf \"\\033[3J\"'\n");
            fw.close();

            java.io.File setup = new java.io.File(binDir, "linux-setup");
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
            fws.write("    am broadcast -a com.zmux.terminal.INSTALL_OS --es os \"alpine\" >/dev/null 2>&1\n");
            fws.write("elif [ \"$choice\" = \"2\" ]; then\n");
            fws.write("    echo ''\n");
            fws.write("    echo '" + esc + "[32m[*]" + esc + "[0m Triggering Debian installation...'\n");
            fws.write("    am broadcast -a com.zmux.terminal.INSTALL_OS --es os \"debian\" >/dev/null 2>&1\n");
            fws.write("else\n");
            fws.write("    echo 'Cancelled.'\n");
            fws.write("fi\n");
            fws.close();
            setup.setExecutable(true);
        } catch (Exception e) {
            e.printStackTrace();
        }

        String[] env = new String[] {
            "HOME=" + filesDir,
            "PATH=" + binDir.getAbsolutePath() + ":/system/bin:/system/xbin:/vendor/bin",
            "ENV=" + filesDir + "/.zmuxrc"
        };
        return new TerminalSession("/system/bin/sh", filesDir, new String[0], env, 2000, client);
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
