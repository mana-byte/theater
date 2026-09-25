"""Observation package: watch what agents are doing by tailing transcripts.

Getting the text is the per-harness ``Source`` seam (``theater.harness``); deciding what
it means — the reducer, quiet timers, and job completion — lives here, once for all.
"""
