"""Weather station, reporting the ``[wind]`` model.

A thin adapter like every other device: the wind lives on the rig, and this only
publishes it. Without it the wind is still fully simulated - the guide star moves
and a client's RMS spikes - but nothing *reports* it, so no client can react. A
scheduler that suspends a sequence when it gets rough is the behaviour this
exists to make testable.

Off by default (``server.weather``), because a client's profile enumerates
devices and an unexpected seventh one turns up in every existing Ekos profile.

``WEATHER_STATUS`` is the load-bearing property, not ``WEATHER_PARAMETERS``.
Ekos reads the *vector state* to answer "is it safe" - Ok, Busy for a warning,
Alert past the limit - so a device that publishes readings and no status is
decoration. The thresholds are writable for the same reason they are in
``INDI::WeatherInterface``: the client decides what counts as too much wind, and
the status is derived from them rather than from anything in the rig config.
"""

from __future__ import annotations

from ..indi.device import WEATHER_INTERFACE, Device
from ..indi.protocol import (
    LightItem,
    LightVector,
    NumberItem,
    NumberVector,
    Perm,
    PropState,
    Vector,
    parse_number,
)


class Weather(Device):
    device_name = "AstroSkySim Weather"
    interface = WEATHER_INTERFACE

    def setup(self) -> None:
        self.parameters = self.add(
            NumberVector(
                name="WEATHER_PARAMETERS",
                label="Parameters",
                perm=Perm.RO,
                items=[
                    NumberItem("WEATHER_WIND_SPEED", "Wind (km/h)", 0.0, "%.1f", 0, 200, 0),
                    NumberItem("WEATHER_WIND_GUST", "Gust (km/h)", 0.0, "%.1f", 0, 200, 0),
                    NumberItem("WEATHER_TEMPERATURE", "Temperature (C)", 0.0, "%.1f", -50, 60, 0),
                ],
            )
        )
        # One light per parameter, which is how a client attributes an Alert to
        # the thing that caused it rather than to "the weather".
        self.status = self.add(
            LightVector(
                name="WEATHER_STATUS",
                label="Status",
                items=[
                    LightItem("WEATHER_WIND_SPEED", "Wind"),
                    LightItem("WEATHER_WIND_GUST", "Gust"),
                ],
            )
        )
        self.wind_limits = self.add(
            NumberVector(
                name="WEATHER_WIND_SPEED",
                label="Wind (km/h)",
                group="Parameters",
                items=[
                    NumberItem("MIN_OK", "Min OK", 0.0, "%.1f", 0, 200, 0),
                    NumberItem("MAX_OK", "Max OK", 40.0, "%.1f", 0, 200, 0),
                    NumberItem("PERCENT_WARNING", "Warning (%)", 15.0, "%.0f", 0, 100, 0),
                ],
            )
        )
        self.gust_limits = self.add(
            NumberVector(
                name="WEATHER_WIND_GUST",
                label="Gust (km/h)",
                group="Parameters",
                items=[
                    NumberItem("MIN_OK", "Min OK", 0.0, "%.1f", 0, 200, 0),
                    NumberItem("MAX_OK", "Max OK", 60.0, "%.1f", 0, 200, 0),
                    NumberItem("PERCENT_WARNING", "Warning (%)", 15.0, "%.0f", 0, 100, 0),
                ],
            )
        )
        self.update = self.add(
            NumberVector(
                name="WEATHER_UPDATE",
                label="Update",
                group="Options",
                items=[NumberItem("PERIOD", "Period (s)", 4.0, "%.0f", 0, 3600, 1)],
            )
        )

        self.writer("WEATHER_WIND_SPEED", self._w_limits)
        self.writer("WEATHER_WIND_GUST", self._w_limits)
        self.writer("WEATHER_UPDATE", self._w_update)

        self._elapsed = 0.0

    async def _w_limits(self, vec: Vector, values: dict[str, str]) -> None:
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        self.push(vec, state=PropState.OK)
        self._publish(force=True)

    async def _w_update(self, vec: Vector, values: dict[str, str]) -> None:
        vec["PERIOD"].value = max(parse_number(values.get("PERIOD", "4")), 0.0)
        self.push(vec, state=PropState.OK)

    def _light(self, value: float, limits: Vector) -> PropState:
        """Derive one parameter's light from its own thresholds.

        Idle rather than Ok below ``MIN_OK``: a reading under the floor is a
        sensor that is not reporting, not a calm night.
        """
        low = float(limits["MIN_OK"].value)
        high = float(limits["MAX_OK"].value)
        warn = float(limits["PERCENT_WARNING"].value) / 100.0
        if value > high:
            return PropState.ALERT
        if high > low and value >= high - warn * (high - low):
            return PropState.BUSY
        return PropState.OK

    def _publish(self, force: bool = False) -> None:
        wind = self.rig.wind
        speed = 0.0 if wind is None else wind.speed_kmh
        gust = 0.0 if wind is None else wind.reported_gust_kmh

        self.parameters["WEATHER_WIND_SPEED"].value = speed
        self.parameters["WEATHER_WIND_GUST"].value = gust
        self.parameters["WEATHER_TEMPERATURE"].value = self.rig.cfg.focuser.temperature
        # No wind model means no sensor, which is Idle - distinct from a calm
        # night, where the sensor reports zero and is working.
        self.push(self.parameters, state=PropState.IDLE if wind is None else PropState.OK)

        if wind is None:
            for item in self.status.items:
                item.value = PropState.IDLE
            self.push(self.status, state=PropState.IDLE)
            return

        lights = (
            self._light(speed, self.wind_limits),
            self._light(gust, self.gust_limits),
        )
        for item, light in zip(self.status.items, lights, strict=True):
            item.value = light
        # The vector state is what a client actually reads for "is it safe", so
        # it is the worst of the parameters, not a separate judgement.
        if PropState.ALERT in lights:
            overall = PropState.ALERT
        elif PropState.BUSY in lights:
            overall = PropState.BUSY
        else:
            overall = PropState.OK
        self.push(self.status, state=overall)

    async def step(self, dt: float) -> None:
        # A real station reports on its own cadence, not at the tick rate, and
        # the coalescing output queue means a client would only ever see the
        # newest value anyway - so publishing at 10 Hz would be noise on the
        # wire for nothing.
        self._elapsed += dt
        period = float(self.update["PERIOD"].value)
        if self._elapsed < period:
            return
        self._elapsed = 0.0
        self._publish()
