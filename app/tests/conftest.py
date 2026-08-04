"""Test configuration for ZMUX terminal."""
import os
import sys
from pathlib import Path

# Add app directory to path
APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))

# Set test environment
os.environ.setdefault("ZMUX_TEST", "1")
