import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

# Import your Flask app
from app.main import app as application