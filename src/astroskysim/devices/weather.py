"""Weather station, reporting the ``[wind]`` and ``[temperature]`` models.

A thin adapter like every other device: the weather lives on the rig, and this
only publishes it. Without it the wind and the night's cooling are still fully
simulated - the guide star moves, a client's RMS spikes, focus drifts - but
nothing *reports* them, so no client can react. A scheduler that suspends a
sequence when it gets rough is the behaviour this exists to make testable.

``WEATHER_TEMPERATURE`` here is the **air**. It is deliberately not what the
focuser's ``FOCUS_TEMPERATURE`` reads, and neither is what actually sets focus;
see ``TemperatureConfig`` for why the three are separate.

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
                    # Seeded from the model rather than left at zero. A client
                    # sees the definition immediately and the first publish up
                    # to WEATHER_UPDATE seconds later, and 0 C is not a neutral
                    # placeholder the way a calm wind is - it reads as a freezing
                    # night and can trip a MIN_OK threshold before any real
                    # reading arrives.
                    NumberItem(
                        "WEATHER_TEMPERATURE",
                        "Temperature (C)",
                        self._air_c(),
                        "%.1f",
                        -50,
                        60,
                        0,
                    ),
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
                    LightItem("WEATHER_TEMPERATURE", "Temperature"),
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
        # Unlike the two wind limits, the interesting end of this one is the
        # bottom: a night that reaches freezing is the one a scheduler wants to
        # hear about. Hence ``_light`` alerting below MIN_OK as well as above
        # MAX_OK - a temperature parameter with a light that can only fire on a
        # heatwave would be the decoration this module's docstring warns about.
        self.temperature_limits = self.add(
            NumberVector(
                name="WEATHER_TEMPERATURE",
                label="Temperature (C)",
                group="Parameters",
                items=[
                    NumberItem("MIN_OK", "Min OK", -20.0, "%.1f", -50, 60, 0),
                    NumberItem("MAX_OK", "Max OK", 40.0, "%.1f", -50, 60, 0),
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
        self.writer("WEATHER_TEMPERATURE", self._w_limits)
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

    def _air_c(self) -> float:
        """Ambient, or the fixed config value when ``[temperature]`` is off.

        The fallback keeps a client seeing a plausible reading on a rig with no
        temperature model, which is what every config before it got.
        """
        model = self.rig.temperature
        return self.rig.cfg.focuser.temperature if model is None else model.air_c

    def _light(self, value: float, limits: Vector) -> PropState:
        """Derive one parameter's light from its own thresholds.

        Alert past ``MAX_OK`` *or* below ``MIN_OK``; Busy only inside the warning
        band below ``MAX_OK``. Wind only ever tests the top - speed cannot go
        negative and its floor is zero - but temperature's interesting end is the
        bottom, so a strictly one-sided rule would give it a light that could
        only fire in a heatwave.

        The band is deliberately *not* mirrored onto the floor. It is a
        percentage of the whole span, and the shipped wind range is 0-200 km/h,
        so a low-side band would call an ordinary 30 km/h breeze a warning for
        being within 15% of dead calm. A client that wants to be warned before it
        freezes sets ``MIN_OK`` where it wants the alert, which is what these
        thresholds are writable for.
        """
        low = float(limits["MIN_OK"].value)
        high = float(limits["MAX_OK"].value)
        warn = float(limits["PERCENT_WARNING"].value) / 100.0
        if value > high or value < low:
            return PropState.ALERT
        if high > low and value >= high - warn * (high - low):
            return PropState.BUSY
        return PropState.OK

    def _publish(self, force: bool = False) -> None:
        wind = self.rig.wind
        temperature = self.rig.temperature
        speed = 0.0 if wind is None else wind.speed_kmh
        gust = 0.0 if wind is None else wind.reported_gust_kmh
        # Its light stays Idle when the model is off, though - a constant is a
        # placeholder, not a working sensor.
        air = self._air_c()

        self.parameters["WEATHER_WIND_SPEED"].value = speed
        self.parameters["WEATHER_WIND_GUST"].value = gust
        self.parameters["WEATHER_TEMPERATURE"].value = air
        # Idle only when nothing on this station is simulated - distinct from a
        # calm, mild night, where the sensors report and are working.
        live = wind is not None or temperature is not None
        self.push(self.parameters, state=PropState.OK if live else PropState.IDLE)

        # "No sensor" is per parameter, not per station. A rig with the wind off
        # and the temperature on has two dead lights and one live one, and the
        # vector state has to come from the live one alone - a global Idle would
        # tell a scheduler the whole station is down.
        lights = (
            PropState.IDLE if wind is None else self._light(speed, self.wind_limits),
            PropState.IDLE if wind is None else self._light(gust, self.gust_limits),
            PropState.IDLE
            if temperature is None
            else self._light(air, self.temperature_limits),
        )
        for item, light in zip(self.status.items, lights, strict=True):
            item.value = light
        # The vector state is what a client actually reads for "is it safe", so
        # it is the worst of the *live* parameters, not a separate judgement.
        if PropState.ALERT in lights:
            overall = PropState.ALERT
        elif PropState.BUSY in lights:
            overall = PropState.BUSY
        elif all(light is PropState.IDLE for light in lights):
            overall = PropState.IDLE
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
