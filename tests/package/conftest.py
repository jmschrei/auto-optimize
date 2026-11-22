import os
import sys

# Import the installer from this checkout, never from an installed copy.
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(
	__file__)), os.pardir, os.pardir))
sys.path.insert(0, os.path.join(ROOT, 'src'))
