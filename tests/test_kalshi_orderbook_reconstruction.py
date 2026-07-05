"""The NO-bid -> YES-ask reconstruction invariant (plan §2.1, §10).

Kalshi returns only bids for both sides; the YES ask book must be derived as
1 - NO bid. Getting this wrong makes every spread meaningless.
"""

import pytest

pytest.skip("Milestone 2 (Kalshi client) not yet implemented", allow_module_level=True)
