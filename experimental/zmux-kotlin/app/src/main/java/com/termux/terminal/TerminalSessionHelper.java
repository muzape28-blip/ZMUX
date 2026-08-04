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
        } catch (Exception e) {
            e.printStackTrace();
        }

        String[] env = new String[] {
            "HOME=" + filesDir,
            "PATH=/system/bin:/system/xbin:/vendor/bin",
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
