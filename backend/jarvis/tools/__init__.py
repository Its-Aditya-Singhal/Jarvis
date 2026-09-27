"""Tools the assistant can execute for the verified owner.

alarms/timers, calendar, notes (encrypted local store, optional Apple
Calendar / Apple Notes sync), opening installed apps, Spotlight file search
in folders the owner allowed. The LLM only proposes; ``runner`` validates
arguments and executes, and replies are built from the real results.
"""
