"""Coordinator for CSU data."""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import cast

from homeassistant.components.recorder import get_instance  # type: ignore
from homeassistant.components.recorder.models import StatisticData  # type: ignore
from homeassistant.components.recorder.models import StatisticMeanType  # type: ignore
from homeassistant.components.recorder.models import StatisticMetaData  # type: ignore
from homeassistant.util.unit_conversion import EnergyConverter  # type: ignore
from homeassistant.util.unit_conversion import VolumeConverter  # type: ignore
from homeassistant.components.recorder.statistics import async_add_external_statistics  # type: ignore
from homeassistant.components.recorder.statistics import get_last_statistics  # type: ignore
from homeassistant.components.recorder.statistics import statistics_during_period  # type: ignore
from homeassistant.components.recorder.statistics import valid_statistic_id  # type: ignore
from homeassistant.config_entries import ConfigEntry  # type: ignore
from homeassistant.const import CONF_PASSWORD  # type: ignore
from homeassistant.const import CONF_USERNAME  # type: ignore
from homeassistant.const import UnitOfEnergy  # type: ignore
from homeassistant.const import UnitOfVolume  # type: ignore
from homeassistant.core import callback  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.exceptions import ConfigEntryAuthFailed  # type: ignore
from homeassistant.helpers import aiohttp_client  # type: ignore
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator  # type: ignore
from homeassistant.helpers.update_coordinator import UpdateFailed  # type: ignore
from homeassistant.util import dt as dt_util  # type: ignore

from .const import CONF_ACCESS_TOKEN
from .const import CONF_CUSTOMER_ID
from .const import CONF_HOURLY_REBUILD_DONE
from .const import CONF_HOURLY_REBUILD_REFUSED
from .const import CONF_TOKEN_EXPIRES_AT
from .const import DOMAIN
from .const import TIME_ZONE
from .csu import AggregateType
from .csu import CSU
from .csu import Meter
from .csu import MeterType
from .csu import ReadResolution
from .csu import reconcile_hourly_with_daily
from .csu import SUPPORTED_AGGREGATE_TYPES
from .csu import UsageRead
from .exceptions import CannotConnect
from .exceptions import InvalidAuth

_LOGGER = logging.getLogger(__name__)

# The daily endpoint only keeps a rolling two years; older days come back empty.
DAILY_HISTORY = timedelta(days=730)
# The interval endpoint reaches back to service start. Walk in month-sized windows
# and stop at the first empty one; the cap only guards against a runaway loop.
HOURLY_WINDOW_DAYS = 30
HOURLY_MAX_HISTORY = timedelta(days=3650)
# One token must outlast the whole rebuild, because the server has no refresh
# and a new login could ask for an MFA code.
REBUILD_TOKEN_MARGIN = 40 * 60
# A series whose history can never be rebuilt safely stops being retried.
REBUILD_MAX_REFUSALS = 3


class CsuCoordinator(DataUpdateCoordinator[dict[str, UsageRead]]):
    """Handle fetching CSU data, updating sensors and inserting statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the data handler."""

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Csu",
            update_interval=timedelta(hours=3),
        )
        self.api = CSU(
            aiohttp_client.async_get_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            access_token=entry.data.get(CONF_ACCESS_TOKEN, ""),
            token_expires_at=entry.data.get(CONF_TOKEN_EXPIRES_AT, 0.0),
            customer_id=entry.data.get(CONF_CUSTOMER_ID, ""),
        )
        # The update listener compares against this so token saves (data only)
        # don't trigger a reload.
        self.options = dict(entry.options)
        # A failed rebuild is retried on the next start, not on every poll.
        self._rebuild_attempted: set[str] = set()
        self.entities = []
        self.data = {
            "monthly_totals": {
                "ELEC": {"usage": None, "cost": None},
                "GAS": {"usage": None, "cost": None},
                "WATER": {"usage": None, "cost": None},
            }
        }

        @callback
        def _dummy_listener() -> None:
            pass

        self.async_add_listener(_dummy_listener)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from CSU."""

        try:
            await self.api.async_login()
        except InvalidAuth as err:
            # Includes MfaRequired: a new one-time code needs the reauth flow.
            raise ConfigEntryAuthFailed from err
        except CannotConnect as err:
            raise UpdateFailed(f"Cannot reach CSU: {err}") from err
        self._save_token()
        # TODO: Usage Reads works great, but for useful sensors we need to take that data and get current bill usage.
        # If we poll last 12 months we can get 'typical' usage and cost.
        # That will let us make the sensors.
        # usage_reads: list[UsageRead] = await self.api.async_get_usage_reads()
        # _LOGGER.debug("Updating sensor data with: %s", usage_reads)
        if not self.api.meters:
            await self.api.async_get_meters()
        for meter in self.api.meters:
            bill_reads = await self.api.async_get_bill_history(meter)
            last_read = bill_reads.pop()
            # start_time = datetime.today().replace(day=1)
            start_time = last_read.end_time + timedelta(days=1)
            end_time = datetime.today()
            sum_usage = 0.0

            usage_reads = await self.api.async_get_usage_reads(
                meter, AggregateType.DAY, start_time, end_time
            )
            for usage_read in usage_reads:
                sum_usage += usage_read.consumption
            _LOGGER.debug(
                "Updating montly total data for %s with: %s",
                meter.meter_type.name,
                sum_usage,
            )
            self.data["monthly_totals"][meter.meter_type.name]["usage"] = sum_usage
            match meter.meter_type:
                case MeterType.ELEC:
                    total_cost = sum_usage * self.config_entry.options.get(
                        "electric_rate_per_kwh", 0.0
                    )
                    total_cost = total_cost + (
                        len(usage_reads)
                        * self.config_entry.options.get("electric_rate_per_day", 0.0)
                    )
                case MeterType.GAS:
                    total_cost = sum_usage * self.config_entry.options.get(
                        "gas_rate_per_ccf", 0.0
                    )
                    total_cost = total_cost + (
                        len(usage_reads)
                        * self.config_entry.options.get("gas_rate_per_day", 0.0)
                    )
                case MeterType.WATER:
                    total_cost = sum_usage * self.config_entry.options.get(
                        "water_rate_per_cf", 0.0
                    )
                    total_cost = total_cost + (
                        len(usage_reads)
                        * self.config_entry.options.get("water_rate_per_day", 0.0)
                    )
            self.data["monthly_totals"][meter.meter_type.name]["cost"] = total_cost
        # The first import walks the whole history; never block HA startup on it.
        if not getattr(self, "_stats_task", None) or self._stats_task.done():
            # Tied to the config entry so unload/reload cancels an in-flight import.
            self._stats_task = self.config_entry.async_create_background_task(
                self.hass, self._insert_statistics(), "csu_insert_statistics"
            )
        return self.data

    def _save_token(self) -> None:
        """Keep the current token so a restart can reuse it while it is valid."""
        saved = {
            CONF_ACCESS_TOKEN: self.api.access_token,
            CONF_TOKEN_EXPIRES_AT: self.api.token_expires_at,
            CONF_CUSTOMER_ID: self.api.customer_id,
        }
        if any(self.config_entry.data.get(k) != v for k, v in saved.items()):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **saved}
            )

    async def _insert_statistics(self) -> None:
        """Insert CSU statistics; net-metered electric also gets an export series."""
        if not self.api.meters:
            await self.api.async_get_meters()
        token_ready = False
        for meter in self.api.meters:
            id_prefix = (
                f"csu_{meter.meter_type.name.lower()}_{meter.customer.customer_id}"
            )
            if meter.meter_type == MeterType.ELEC:
                series = [
                    ("energy_consumption", "consumption", "consumption"),
                    ("energy_production", "received", "returned to grid"),
                ]
            else:
                # Gas and water are volumes, so "energy" in the id would be wrong.
                kind = meter.meter_type.name.lower()
                series = [(f"{kind}_consumption", "consumption", "consumption")]
                await self._migrate_legacy_statistic_id(
                    f"{DOMAIN}:{id_prefix}_energy_consumption",
                    f"{DOMAIN}:{id_prefix}_{series[0][0]}",
                )
            self._reads_cache = {}
            if self._can_rebuild_hourly(meter, id_prefix, series):
                if not token_ready:
                    token_ready = await self._async_prepare_rebuild_token()
                if token_ready:
                    await self._async_rebuild_hourly(meter, id_prefix, series)
            for suffix, attr, label in series:
                await self._insert_series(meter, id_prefix, suffix, attr, label)

    def _pending_rebuild(self, id_prefix, series) -> dict:
        """Series not yet rebuilt and not already tried since this start."""
        done = set(self.config_entry.data.get(CONF_HOURLY_REBUILD_DONE, []))
        refused = self.config_entry.data.get(CONF_HOURLY_REBUILD_REFUSED, {})
        pending = {}
        for suffix, attr, label in series:
            sid = f"{DOMAIN}:{id_prefix}_{suffix}"
            if sid in done or sid in self._rebuild_attempted:
                continue
            if refused.get(sid, 0) >= REBUILD_MAX_REFUSALS:
                _LOGGER.info("Not retrying the history rebuild of %s", sid)
                self._rebuild_attempted.add(sid)  # say so once per start
                continue
            pending[sid] = (attr, label)
        return pending

    def _can_rebuild_hourly(self, meter, id_prefix, series) -> bool:
        if AggregateType.HOUR not in SUPPORTED_AGGREGATE_TYPES[meter.read_resolution]:
            return False
        return bool(self._pending_rebuild(id_prefix, series))

    async def _async_prepare_rebuild_token(self) -> bool:
        """Get one token for the whole rebuild; a failure only skips the rebuild."""
        try:
            await self.api.async_ensure_token(REBUILD_TOKEN_MARGIN)
        except Exception as err:  # noqa: BLE001
            # Never start a reauth from a background backfill; try next start.
            _LOGGER.warning("Skipping the history rebuild for now: %s", err)
            return False
        # A restart before the next poll should reuse this token, not log in again.
        self._save_token()
        return True

    async def _async_rebuild_hourly(self, meter, id_prefix, series) -> None:
        """Replace each series with true hourly history, written in one go.

        The sums are recomputed from zero over the full history and written in a
        single call, so a failure leaves the old, internally consistent rows. The
        old daily-resolution rows are overwritten, never prepended to.
        """
        pending = self._pending_rebuild(id_prefix, series)
        self._rebuild_attempted.update(pending)
        if not pending:
            return
        end = dt_util.now(await dt_util.async_get_time_zone(TIME_ZONE))
        try:
            reads = await self.api.async_get_usage_reads(
                meter,
                AggregateType.HOUR,
                end - HOURLY_MAX_HISTORY,
                end,
                window_days=HOURLY_WINDOW_DAYS,
                strict=True,
            )
            daily = await self.api.async_get_usage_reads(
                meter, AggregateType.DAY, end - DAILY_HISTORY, end, strict=True
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Hourly history for %s could not be fetched, will retry on the "
                "next start: %s",
                meter.meter_type.name,
                err,
            )
            return
        if not reads:
            _LOGGER.warning("No hourly history returned for %s", meter.meter_type.name)
            for statistic_id in pending:
                self._count_refusal(statistic_id)
            return
        # Electric interval data has holes that the daily totals still cover. Gas
        # hours without a read are real "no usage" hours, so gas is used as is.
        if meter.meter_type == MeterType.ELEC:
            reads = reconcile_hourly_with_daily(reads, daily)
        for statistic_id, (attr, label) in pending.items():
            if not valid_statistic_id(statistic_id):
                _LOGGER.warning("Invalid statistic id: %s", statistic_id)
                continue
            points = self._hourly_points(reads, attr)
            if not await self._rebuild_is_safe(statistic_id, points):
                self._count_refusal(statistic_id)
                continue
            metadata = self._statistic_metadata(meter, statistic_id, label)
            _LOGGER.info(
                "Rebuilding %s with %d hourly points", statistic_id, len(points)
            )
            async_add_external_statistics(self.hass, metadata, points)
            await get_instance(self.hass).async_block_till_done()
            self._mark_rebuilt(statistic_id)

    @staticmethod
    def _hourly_points(reads: list[UsageRead], attr: str) -> list[StatisticData]:
        """Dense hourly points with sums from zero; missing hours count as zero.

        Hours without a read must still get a row, otherwise an old daily-resolution
        row sitting on that hour would keep its stale sum.
        """
        # Step in UTC: wall-clock arithmetic is wrong across DST changes.
        value_at = {
            r.start_time.astimezone(timezone.utc): getattr(r, attr) for r in reads
        }
        current = min(value_at)
        last = max(value_at)
        points = []
        running_sum = 0.0
        while current <= last:
            value = value_at.get(current, 0.0)
            running_sum += value
            points.append(StatisticData(start=current, state=value, sum=running_sum))
            current += timedelta(hours=1)
        return points

    async def _rebuild_is_safe(self, statistic_id, points) -> bool:
        """Refuse if old rows would survive outside the new range with stale sums."""
        recorder = get_instance(self.hass)
        first, last = points[0]["start"], points[-1]["start"]
        before = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime.fromtimestamp(0, tz=timezone.utc),
            first,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        newest = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, set()
        )
        newest_start = newest[statistic_id][0]["start"] if newest else None
        if before.get(statistic_id) or (
            newest_start is not None and newest_start > last.timestamp()
        ):
            oldest = (
                before[statistic_id][0]["start"] if before.get(statistic_id) else None
            )
            _LOGGER.warning(
                "Not rebuilding %s: the history walk covered %s to %s, but existing "
                "rows reach outside that (oldest row before it: %s)",
                statistic_id,
                first.date(),
                last.date(),
                datetime.fromtimestamp(oldest, tz=timezone.utc).date()
                if oldest is not None
                else "none",
            )
            return False
        return True

    def _count_refusal(self, statistic_id: str) -> None:
        """Remember a refusal so a series that can never rebuild stops retrying."""
        refused = dict(self.config_entry.data.get(CONF_HOURLY_REBUILD_REFUSED, {}))
        refused[statistic_id] = refused.get(statistic_id, 0) + 1
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_HOURLY_REBUILD_REFUSED: refused},
        )

    def _mark_rebuilt(self, statistic_id: str) -> None:
        done = list(self.config_entry.data.get(CONF_HOURLY_REBUILD_DONE, []))
        if statistic_id not in done:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    CONF_HOURLY_REBUILD_DONE: [*done, statistic_id],
                },
            )

    async def _migrate_legacy_statistic_id(self, old_id: str, new_id: str) -> None:
        """Rename pre-0.2.0 gas/water statistics so their history is kept."""
        recorder = get_instance(self.hass)
        new_stat, old_stat = [
            await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, sid, False, set()
            )
            for sid in (new_id, old_id)
        ]
        if new_stat or not old_stat:
            return
        _LOGGER.info("Renaming statistic %s to %s", old_id, new_id)
        recorder.async_update_statistics_metadata(old_id, new_statistic_id=new_id)
        # The rename is queued; wait so the lookups below see the new id.
        await recorder.async_block_till_done()

    async def _cached_reads(self, meter, start_time):
        cache = getattr(self, "_reads_cache", {})
        # None (full history) satisfies any later-start request for the same meter
        for (sid, st), reads in cache.items():
            if sid == meter.service_id and (
                st is None or (start_time is not None and st <= start_time)
            ):
                return reads
        reads = await self._async_get_usage_reads(meter, TIME_ZONE, start_time)
        cache[(meter.service_id, start_time)] = reads
        self._reads_cache = cache
        return reads

    async def _insert_series(self, meter, id_prefix, suffix, attr, label) -> None:
        statistic_id = f"{DOMAIN}:{id_prefix}_{suffix}"
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not last_stat:
            _LOGGER.info("Updating statistics for the first time: %s", statistic_id)
            usage_reads = await self._cached_reads(meter, None)
            running_sum = 0.0
            last_stats_time = None
        else:
            usage_reads = await self._cached_reads(
                meter, last_stat[statistic_id][0]["start"]
            )
            if not usage_reads:
                return
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                usage_reads[0].start_time,
                None,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            if stats.get(statistic_id):
                running_sum = cast(float, stats[statistic_id][0]["sum"])
                last_stats_time = stats[statistic_id][0]["start"]
            else:
                running_sum = cast(float, last_stat[statistic_id][0]["sum"])
                last_stats_time = last_stat[statistic_id][0]["start"]
        points = []
        for usage_read in usage_reads:
            start = usage_read.start_time
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue
            value = getattr(usage_read, attr)
            running_sum += value
            points.append(StatisticData(start=start, state=value, sum=running_sum))
        metadata = self._statistic_metadata(meter, statistic_id, label)
        if not valid_statistic_id(statistic_id):
            _LOGGER.warning("Invalid statistic id: %s", statistic_id)
            return
        _LOGGER.info("Inserting %d points into %s", len(points), statistic_id)
        async_add_external_statistics(self.hass, metadata, points)

    @staticmethod
    def _statistic_metadata(meter, statistic_id, label) -> StatisticMetaData:
        unit = {
            MeterType.ELEC: UnitOfEnergy.KILO_WATT_HOUR,
            MeterType.GAS: UnitOfVolume.CENTUM_CUBIC_FEET,
            MeterType.WATER: UnitOfVolume.CUBIC_FEET,
        }[meter.meter_type]
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            unit_class=(
                EnergyConverter.UNIT_CLASS
                if meter.meter_type == MeterType.ELEC
                else VolumeConverter.UNIT_CLASS
            ),
            name=(
                f"CSU {meter.meter_type.name.lower()} "
                f"{meter.customer.customer_id} {label}"
            ),
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=unit,
        )

    async def _async_get_usage_reads(
        self, meter: Meter, time_zone_str: str, start_time: float | None = None
    ) -> list[UsageRead]:
        """Get usage reads.

        If start_time is None, get usage reads since account activation, otherwise since start_time - 30 days to allow corrections in data from utilities.

        We read at different resolutions depending on age:
        - day resolution for the past 2 years (all the daily endpoint keeps)
        - hour resolution for past 2 months
        """

        def _update_with_finer_usage_reads(
            usage_reads: list[UsageRead], finer_usage_reads: list[UsageRead]
        ) -> None:
            for i, usage_read in enumerate(usage_reads):
                for j, finer_usage_read in enumerate(finer_usage_reads):
                    _LOGGER.debug(
                        "Read end time is %s and fine read start time is %s",
                        usage_read.end_time,
                        finer_usage_read.start_time,
                    )
                    if usage_read.start_time == finer_usage_read.start_time:
                        usage_reads[i:] = finer_usage_reads[j:]
                        return
                    if usage_read.end_time == finer_usage_read.start_time:
                        usage_reads[i + 1 :] = finer_usage_reads[j:]
                        return
                    if usage_read.end_time < finer_usage_read.start_time:
                        break
            usage_reads += finer_usage_reads

        tz = await dt_util.async_get_time_zone(time_zone_str)
        end = dt_util.now(tz)
        if start_time is None:
            start = end - DAILY_HISTORY
        else:
            start = datetime.fromtimestamp(start_time, tz=tz) - timedelta(days=30)
            # Older days only return empty rows, so don't request them.
            start = max(start, end - DAILY_HISTORY)
        usage_reads = await self.api.async_get_usage_reads(
            meter, AggregateType.DAY, start, end
        )
        if meter.read_resolution == ReadResolution.DAY:
            _LOGGER.debug("Meter is Daily only. Returning Usages")
            return usage_reads

        if start_time is None:
            start = end - timedelta(days=2 * 30)
        else:
            assert start
            start = max(start, end - timedelta(days=2 * 30))
        _LOGGER.debug(
            "Meter is hourly. Getting finer data stats for %s until %s", start, end
        )
        hourly_usage_reads = await self.api.async_get_usage_reads(
            meter, AggregateType.HOUR, start, end
        )
        # Only days inside the hourly window; older days stay daily.
        if meter.meter_type == MeterType.ELEC:
            hourly_usage_reads = reconcile_hourly_with_daily(
                hourly_usage_reads,
                [r for r in usage_reads if r.start_time.date() >= start.date()],
            )
        _update_with_finer_usage_reads(usage_reads, hourly_usage_reads)
        return usage_reads

        # add in additional update for quarter-hour reads?


type CsuConfigEntry = ConfigEntry[CsuCoordinator]
