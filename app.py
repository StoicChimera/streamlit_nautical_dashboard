import sys
import os

# Force src onto path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from nautical_dashboard.main import *
