package com.termux.terminal;

public class TerminalSessionHelper {
    public static void injectEmulator(TerminalSession session, TerminalEmulator emulator) {
        session.mEmulator = emulator;
        session.mShellPid = 1;
    }
    
    public static ByteQueue getQueue(TerminalSession session) {
        return session.mTerminalToProcessIOQueue;
    }

    public static int getColumns(TerminalEmulator emulator) {
        return emulator != null ? emulator.mColumns : 80;
    }

    public static int getRows(TerminalEmulator emulator) {
        return emulator != null ? emulator.mRows : 24;
    }
}
