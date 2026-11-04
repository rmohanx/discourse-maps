"""Test-wide setup: tests always use the local database, never the cloud project."""

import os

os.environ["DM_TARGET"] = "local"
