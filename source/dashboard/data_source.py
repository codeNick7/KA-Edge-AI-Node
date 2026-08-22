"""Data source interface for the board dashboard.

MockDataSource (mock_source.py) is the only implementation for now, since
there's no real SQLite evidence store yet (architecture doc Section 8 -
not built as of this prep pass) and no real sensor data (connectors not
arrived, per execution plan). Routes and templates depend only on this
Protocol, so swapping in a real store-backed source later touches nothing
else in source/dashboard/.
"""

from typing import Protocol

POTS = ("A", "B")


class DataSource(Protocol):
    def get_raw(self, pot: str, n: int, range: str | None = None, max_points: int | None = None) -> list[dict]:
        """Most recent n raw sensor readings for one pot, oldest first.

        Each dict: timestamp (ISO 8601 str), moisture_pct, gas_index,
        temp_c, light_lux.

        range/max_points only apply when n=0 (full-history request): scope
        to a time-range filter key (5m/15m/1h/today/week/overall) and/or
        decimate down to ~max_points, for chart rendering rather than exact
        export (see real_source.py, 2026-08-22 dashboard perf work).
        """
        ...

    def get_inference(self, pot: str, n: int, range: str | None = None, max_points: int | None = None) -> list[dict]:
        """Most recent n inference results for one pot, oldest first.

        Each dict: timestamp, rsmi, light_delta, gas_index_trend,
        rsmi_trend, flag, confidence, contributing_trend, model_version.

        range/max_points: see get_raw.
        """
        ...
