# ---------------------------------------------------------------------------
# Erg Nerd — runtime configuration flags
# ---------------------------------------------------------------------------

# Set to True to inject ~30k synthetic workouts after real data loads.
# Synthetic data is injected only into the in-memory dict; localStorage is
# never modified, so your real Concept2 data is always safe.
# See services/synthetic_data.py for generation details.
SYNTHETIC_MODE = False

# Set to True to record per-render call trees + a combined session report
# under .profiles/ via pyinstrument. See services/profiling.py for details.
PROFILE_ENABLE = False
