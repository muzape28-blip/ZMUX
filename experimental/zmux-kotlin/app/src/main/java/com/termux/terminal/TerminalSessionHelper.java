package com.termux.terminal;

public class TerminalSessionHelper {
    public static void injectEmulator(TerminalSession session, TerminalEmulator emulator) {
        session.mEmulator = emulator;
        session.mShellPid = 1;
    }
    
    public static int readQueue(TerminalSession session, byte[] buffer, boolean block) {
        ByteQueue queue = session.mTerminalToProcessIOQueue;
        if (queue == null) return -1;
        return queue.read(buffer, block);
    }

    public static void closeQueue(TerminalSession session) {
        ByteQueue queue = session.mTerminalToProcessIOQueue;
        if (queue != null) queue.close();
    }

    public static int getColumns(TerminalEmulator emulator) {
        return emulator != null ? emulator.mColumns : 80;
    }

    public static int getRows(TerminalEmulator emulator) {
        return emulator != null ? emulator.mRows : 24;
    }
}
