"""Scout enrichment: free Finnhub event context (earnings-in-horizon warnings +
a recent news headline) layered onto the options scout's reads.

ANALYSIS ONLY. Read-only public data; no order path. Everything here is
BEST-EFFORT and graceful: without a Finnhub API key (or on any API hiccup) the
enrichment is simply absent and the scout email is unchanged -- the email is the
product, enrichment is a bonus.
"""
