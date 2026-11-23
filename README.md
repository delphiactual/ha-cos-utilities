# HA Colorado Springs Utilities Integration

> **Fork notice.** This is a maintained fork of [HartD92/ha-cos-utilities](https://github.com/HartD92/ha-cos-utilities) (unmaintained since 2024-07), targeting Home Assistant 2026.11+. Install it as a HACS custom repository: `https://github.com/delphiactual/ha-cos-utilities`.
>
> **Credits.** The net-metering, interval parsing and first-import fixes are [mattsm](https://github.com/mattsm)'s work (upstream PR #71). The local brand assets are [frenck](https://github.com/frenck)'s (upstream PR #72).

## Long-term statistics (Energy dashboard)

| Meter | Statistic ID | Unit |
| ----- | ------------ | ---- |
| Electric import | `csu:csu_elec_<customer id>_energy_consumption` | kWh |
| Electric export (net metering) | `csu:csu_elec_<customer id>_energy_production` | kWh |
| Gas | `csu:csu_gas_<customer id>_gas_consumption` | CCF |
| Water | `csu:csu_water_<customer id>_water_consumption` | ft³ |

Before 0.2.0 gas and water were stored as `..._energy_consumption`. On first run the integration renames those existing statistics in place, so history is kept. You will need to re-select the gas and water sources once in the Energy dashboard settings.

Known limitation: reads are keyed on CSU's local-time labels, so the repeated hour at the autumn DST change may be merged.

### How far back the history goes

CSU keeps different amounts of history per resolution (checked against a live account):

| Data | History available |
| ---- | ----------------- |
| Electric and gas, hourly | From service start (a little over 3 years on the account that was checked). |
| Water, daily | A rolling 2 years. Older days come back empty. |
| Bills | Only the last 13. They are not imported into statistics. |

Electric (import and export) and gas are imported hourly for their whole history. Water has no hourly data, so it is imported daily for the last 2 years.

CSU's hourly electric data has holes: on the account that was checked, about 150 of the last 730 days had no interval data at all, even though the daily totals were present. Summing the raw hourly data would have been about 22% below the portal. So for every day that has a daily total (the last 2 years), the difference between the daily total and the hourly data is spread over that day's hours that have no data. Real hourly reads are never changed, and daily totals match the portal. Days older than 2 years have no daily total to compare against, so they are used as CSU reports them.

Upgrading from 0.2.x to 0.3.0 does a one-time rebuild of the electric and gas statistics in the background after startup. Each series is fetched in full, its sums are recomputed from zero and it is written in a single step, replacing the old one-row-per-day entries for older periods. Because of that, older periods in the Energy dashboard will change. If anything fails (an outage, an expired login), nothing is written and the rebuild is retried the next time Home Assistant starts. It never asks you to sign in again.

## Smoke test

`scripts/smoke_test.py` logs in with `CSU_USERNAME` / `CSU_PASSWORD` from the environment and prints recent hourly import, export, gas and water usage so it can be checked against the CSU portal:

```text
pip install aiohttp arrow python-dateutil
python scripts/smoke_test.py [days]
```

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![pre-commit][pre-commit-shield]][pre-commit]
[![Black][black-shield]][black]

[![hacs][hacsbadge]][hacs]
[![Project Maintenance][maintenance-shield]][user_profile]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

[![Discord][discord-shield]][discord]
[![Community Forum][forum-shield]][forum]

**This component will set up the following platforms.**

| Platform | Description                                    |
| -------- | ---------------------------------------------- |
| `sensor` | Show info from Colorado Springs Utilities API. |

![example][exampleimg]

## Installation

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
2. If you do not have a `custom_components` directory (folder) there, you need to create it.
3. In the `custom_components` directory (folder) create a new folder called `csu`.
4. Download _all_ the files from the `custom_components/csu/` directory (folder) in this repository.
5. Place the files you downloaded in the new directory (folder) you created.
6. Restart Home Assistant
7. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "Colorado Springs Utilities"

Using your HA configuration directory (folder) as a starting point you should now also have this:

```text
custom_components/csu/translations/en.json
custom_components/csu/translations/fr.json
custom_components/csu/translations/nb.json
custom_components/csu/translations/sensor.en.json
custom_components/csu/translations/sensor.fr.json
custom_components/csu/translations/sensor.nb.json
custom_components/csu/translations/sensor.nb.json
custom_components/csu/__init__.py
custom_components/csu/api.py
custom_components/csu/binary_sensor.py
custom_components/csu/config_flow.py
custom_components/csu/const.py
custom_components/csu/manifest.json
custom_components/csu/sensor.py
custom_components/csu/switch.py
```

## Configuration is done in the UI

<!---->

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

## Credits

This project was generated from [@oncleben31](https://github.com/oncleben31)'s [Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component) template.

Code template was mainly taken from [@Ludeeus](https://github.com/ludeeus)'s [integration_blueprint][integration_blueprint] template

---

[integration_blueprint]: https://github.com/custom-components/integration_blueprint
[black]: https://github.com/psf/black
[black-shield]: https://img.shields.io/badge/code%20style-black-000000.svg?style=for-the-badge
[buymecoffee]: https://www.buymeacoffee.com/HartD92
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/HartD92/ha-cos-utilities.svg?style=for-the-badge
[commits]: https://github.com/HartD92/ha-cos-utilities/commits/main
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[discord]: https://discord.gg/Qa5fW2R
[discord-shield]: https://img.shields.io/discord/330944238910963714.svg?style=for-the-badge
[exampleimg]: example.png
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[license-shield]: https://img.shields.io/github/license/HartD92/ha-cos-utilities.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40HartD92-blue.svg?style=for-the-badge
[pre-commit]: https://github.com/pre-commit/pre-commit
[pre-commit-shield]: https://img.shields.io/badge/pre--commit-enabled-brightgreen?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/HartD92/ha-cos-utilities.svg?style=for-the-badge
[releases]: https://github.com/HartD92/ha-cos-utilities/releases
[user_profile]: https://github.com/HartD92
