"""Application configuration.

The system targets PostgreSQL (raw samples / drop point / manual marks are
production data).  For local development and CI where no server is available,
set DATABASE_URL to a SQLite URL — the ORM models and the numpy analysis
pipeline are database agnostic.
"""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    # Default: PostgreSQL as required by the spec.
    "postgresql+psycopg2://roast:roast@localhost:5432/roast",
)

# Interpolation is used ONLY for plotting a continuous guide line.  Measured
# samples are never overwritten or back-filled.
MAX_GAP_FILL_S = float(os.getenv("MAX_GAP_FILL_S", "45"))

CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
]
