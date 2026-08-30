# Granada Weather

Granada Weather is a lightweight, dependency-free weather-station receiver and
dashboard designed to run comfortably on a Raspberry Pi Zero. It receives local
uploads from an Ambient Weather WS-2000, preserves observations on disk, serves
current and historical charts, collects several forecast providers, and can
forward data to Weather Underground and TRMNL.

## Repository layout

```text
server.py                 Receiver, APIs, history storage, and web server
forecast_collector.py     Forecast collection and forecast-history archive
accuracy_collector.py     Daily forecast verification against observations
trmnl_sender.py           Optional TRMNL sender
static/                   Dashboard and PWA assets
systemd/                  Production services and timers
config/                   Safe configuration example (never real credentials)
```

## Production layout

The repository is source code, not the live runtime. Production intentionally
uses standard Linux locations:

```text
/opt/pi-weather           Installed application
/var/lib/pi-weather       Observations, rollups, and SQLite history
/etc/pi-weather.env       Private configuration
/etc/pi-weather/          Private key material
/etc/systemd/system/      Installed services and timers
```

Keeping these separate allows code updates without touching historical data or
credentials. The running Granada Weather installation continues to use these
locations; this checkout is the source used to prepare deployments.

The accuracy collector runs once daily. It stores completed local-day station
summaries alongside forecast history in SQLite and publishes a replaceable JSON
scorecard for the read-only API. It uses the last forecast collected before each
local day began, preventing same-day updates from improving a provider's result.

## Local smoke test

```sh
WEATHER_STATION_ID=teststation \
WEATHER_STATION_KEY=testkey \
WEATHER_DATA_DIR=./data \
python3 server.py --port 8080
```

Open `http://localhost:8080`, then send a sample observation:

```sh
curl -X POST http://localhost:8080/data/report \
  -d 'ID=teststation&PASSWORD=testkey&dateutc=now&tempf=72.4&humidity=58&windspeedmph=4.2&windgustmph=7.1&winddir=225&baromrelin=29.92&dailyrainin=0.12&solarradiation=450&uv=3'
```

## Configuration

Copy `config/pi-weather.env.example` to `/etc/pi-weather.env`, replace only the
values needed by the enabled integrations, and protect it from other users.
WeatherKit private keys belong under `/etc/pi-weather/`, not in this repository.

The checked-in systemd units reflect the current Raspberry Pi deployment and use
the `jakekit` account. Change the `User=` entries when installing on another Pi.

## Tests

The server-side suite uses only Python's standard library and covers station
authentication and normalization, public API security, rainfall counters and
rollups, forecast-provider normalization, local-date accuracy scoring, and the
TRMNL payload contract.

Normalized daily forecasts include morning (6–10 AM) and afternoon (2–6 PM)
median dew points when the provider supplies hourly or gridded dew-point data.

```sh
python3 -m unittest discover -s tests -v
```

GitHub Actions runs the same suite automatically on pushes and pull requests.

## Deployment rule

Deploy application files from this repository into `/opt/pi-weather`; never copy
runtime data or credentials into the repository. Back up `/var/lib/pi-weather`
and `/etc/pi-weather*` separately as private disaster-recovery material.
