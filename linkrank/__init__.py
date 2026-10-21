"""linkrank — internal PageRank (Link Score) & cannibalization auditor.

Open-source crawler that builds a site's internal link graph, computes internal
PageRank on a 0–100 Link Score scale (Screaming Frog / OnCrawl InRank style), and
detects keyword cannibalization — no paid APIs required.
"""
from .core import main

__version__ = "0.1.1"
__all__ = ["main", "__version__"]
