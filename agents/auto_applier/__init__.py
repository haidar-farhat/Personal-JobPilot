"""Auto-applier package — Playwright form fillers for Greenhouse, Ashby, and Lever.

This package handles the final step in the JobPilot pipeline: actually submitting
job applications on the company's website. It runs after the Tailor agent has
generated tailored resume + cover letter materials and the application has been
scored above the configured threshold.

The runner module is the public entry point — see runner.py.
"""
