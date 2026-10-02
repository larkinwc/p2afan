"""Piecewise-linear temperature -> duty curves with hysteresis."""

from __future__ import annotations
import math


from .sources import Source


class Curve:
    """Clamped piecewise-linear curve with downward hysteresis.

    `duty_pct(temp)` interpolates between points and clamps outside the first
    and last point. A temperature drop smaller than `hysteresis_c` below the
    temperature that produced the current setpoint keeps the previous duty, so
    the fans do not oscillate around a knee.
    """

    def __init__(
        self, points: list[tuple[float, float]], hysteresis_c: float = 0.0
    ) -> None:
        if not isinstance(points, (list, tuple)) or not points:
            raise ValueError("curve needs at least one point")
        pts = []
        for point in points:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in point)):
                raise ValueError("curve points must contain finite temperature and duty")
            temp, duty = map(float, point)
            if not 0 <= duty <= 100:
                raise ValueError("curve duty must be between 0 and 100")
            pts.append((temp, duty))
        pts.sort()
        temps = [t for t, _ in pts]
        if len(set(temps)) != len(temps):
            raise ValueError("curve has duplicate temperatures")
        if any(d1 < d0 for (_, d0), (_, d1) in zip(pts, pts[1:])):
            raise ValueError("curve duty must be nondecreasing with temperature")
        if (isinstance(hysteresis_c, bool) or not isinstance(hysteresis_c, (int, float))
                or not math.isfinite(hysteresis_c) or hysteresis_c < 0):
            raise ValueError("hysteresis_c must be finite and nonnegative")
        self.points = pts
        self.hysteresis_c = float(hysteresis_c)
        self._last_temp: float | None = None
        self._last_duty: float | None = None

    def raw_duty_pct(self, temp: float) -> float:
        pts = self.points
        if temp <= pts[0][0]:
            return pts[0][1]
        if temp >= pts[-1][0]:
            return pts[-1][1]
        for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
            if t0 <= temp <= t1:
                if t1 == t0:
                    return d1
                return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
        return pts[-1][1]  # pragma: no cover - unreachable with sorted points

    def duty_pct(self, temp: float) -> float:
        duty = self.raw_duty_pct(temp)
        if (
            self._last_duty is not None
            and self._last_temp is not None
            and duty < self._last_duty
            and temp > self._last_temp - self.hysteresis_c
        ):
            return self._last_duty
        self._last_temp = temp
        self._last_duty = duty
        return duty

    def reset(self) -> None:
        self._last_temp = None
        self._last_duty = None


class Zone:
    def __init__(
        self,
        name: str,
        sources: list[Source],
        curve: Curve,
        critical_c: float,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("zone name must be nonempty")
        names = [source.name for source in sources]
        if (not names or any(not isinstance(n, str) or not n.strip() for n in names)
                or len(set(names)) != len(names)):
            raise ValueError("zone sources need unique nonempty names")
        if (isinstance(critical_c, bool) or not isinstance(critical_c, (int, float))
                or not math.isfinite(critical_c)):
            raise ValueError("critical_c must be finite")
        self.name = name
        self.sources = sources
        self.curve = curve
        self.critical_c = float(critical_c)
        self.fail_counts: dict[str, int] = {s.name: 0 for s in sources}
        self.last_temp: float | None = None
        self.last_readings: dict[str, float | None] = {}

    def sample(self) -> float | None:
        """Read all sources; return the hottest valid reading, or None."""
        readings: dict[str, float | None] = {}
        best: float | None = None
        for source in self.sources:
            value = source.read()
            if value is not None and not math.isfinite(value):
                value = None
            readings[source.name] = value
            if value is None:
                self.fail_counts[source.name] = self.fail_counts.get(source.name, 0) + 1
            else:
                self.fail_counts[source.name] = 0
                if best is None or value > best:
                    best = value
        self.last_readings = readings
        self.last_temp = best
        return best

    def stale_sources(self, limit: int = 3) -> list[str]:
        return [name for name, n in self.fail_counts.items() if n >= limit]
