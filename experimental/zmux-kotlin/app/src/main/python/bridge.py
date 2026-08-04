import pty
import os
import select
import threading
import fcntl
import termios
import struct
import time
import sys

class PtyBridge:
    def __init__(self, callback):
        self.callback = callback
        self.fd = None
        self.pid = None

    def start(self, cols, rows):
        pid, fd = pty.fork()
        if pid == 0:
            # Child process
            os.environ["TERM"] = "xterm-256color"
            
            # Start our custom setup or shell menu
            # We must use exec so it replaces the process
            # But for a simple python script, we can just run it inline.
            # Since we are in a fork, we can just run our python logic.
            try:
                self._run_menu_or_shell()
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(5)
            finally:
                os._exit(0)
                
        self.fd = fd
        self.pid = pid
        self.resize(cols, rows)
        
        def pump():
            while True:
                try:
                    r, _, _ = select.select([self.fd], [], [])
                    if self.fd in r:
                        data = os.read(self.fd, 4096)
                        if not data:
                            break
                        self.callback.onData(data)
                except Exception as e:
                    break
            self.callback.onClosed()

        threading.Thread(target=pump, daemon=True).start()

    def _run_menu_or_shell(self):
        # We are inside the PTY child! standard print/input works perfectly here.
        print("\033[33m=================================================\033[0m")
        print("\033[32m WELCOME TO ZMUX SYSTEM INITIALIZATION\033[0m")
        print("\033[33m=================================================\033[0m")
        print()
        print("Choose your preferred Linux Environment:")
        print("  1) Alpine Linux (Lightweight, APK)")
        print("  2) Debian (Robust, APT)")
        print("  3) Local Android Shell (Basic)")
        print()
        
        while True:
            try:
                sys.stdout.write("Enter choice [1/2/3]: ")
                sys.stdout.flush()
                choice = input().strip()
                if choice == "1":
                    print("\n\033[32m[+]\033[0m Installing Alpine Linux...")
                    # Mocking the install for now
                    time.sleep(1)
                    print("\033[32m[+]\033[0m Alpine installed successfully! Launching PRoot...")
                    time.sleep(1)
                    os.execv("/system/bin/sh", ["/system/bin/sh"])
                elif choice == "2":
                    print("\n\033[32m[+]\033[0m Installing Debian...")
                    time.sleep(1)
                    print("\033[32m[+]\033[0m Debian installed successfully! Launching PRoot...")
                    time.sleep(1)
                    os.execv("/system/bin/sh", ["/system/bin/sh"])
                elif choice == "3":
                    print("\n\033[34m[*]\033[0m Dropping to Local Android Shell...")
                    os.environ["PS1"] = "\033[32mzmux\033[0m~\033[34m:\033[0m$ "
                    os.execv("/system/bin/sh", ["/system/bin/sh"])
                else:
                    print("\033[31m[-]\033[0m Invalid choice. Try again.\n")
            except EOFError:
                break

    def write(self, data):
        if self.fd:
            try:
                os.write(self.fd, bytes(data))
            except OSError:
                pass

    def resize(self, cols, rows):
        if self.fd:
            try:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass

    def close(self):
        if self.fd:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
