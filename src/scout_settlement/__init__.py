"""Scout settlement / accuracy engine.

ANALYSIS ONLY. Reads delayed public price bars and decides an after-the-fact
verdict (did the underlying reach the play's TARGET before its STOP) so the
dashboard can score the scout's accuracy. No order path; nothing here can place,
review, or cancel a trade.
"""
