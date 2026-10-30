"""The simulated week (RAI-273): 40 Coral Lane shops, 7 days, the real HTTP API.

``python -m mimi.api.week`` runs it offline (₦0, the playbook in Muse Spark's
seat) and writes a run report; ``--soak-hours H --soak-kills M`` loops the
week, time-compressed, with forced worker kills. ``--live`` needs
``--max-naira`` and a key. See ``docs/WEEK.md``.
"""
