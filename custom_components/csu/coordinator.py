"""Coordinator for CSU data."""

import logging
from datetime import datetime
from datetime import time
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
from homeassistant.loader import async_get_integration  # type: ignore
from homeassistant.util import dt as dt_util  # type: ignore

from .const import CONF_ACCESS_TOKEN
from .const import CONF_CUSTOMER_ID
from .const import CONF_HOURLY_REBUILD_DONE
from .const import CONF_HOURLY_REBUILD_REFUSED
from .const import CONF_HOURLY_REBUILD_VERSION
from .const import CONF_TOKEN_EXPIRES_AT
from .const import DOMAIN
from .const import TIME_ZONE
from .csu import AggregateType
from .csu import CSU
from .csu import dense_hourly_reads
from .csu import LOCAL_TZ
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
# The interval endpoint reaches back to service start. Walk it until several
# empty windows in a row; the cap only guards against a runaway loop. Long
# ranges come back with missing reads and a stale tail, so the windows must
# stay short.
HOURLY_WINDOW_DAYS = 7
# A gap of a week or two in the interval data must not end the history early.
HOURLY_EMPTY_WINDOWS = 5
HOURLY_MAX_HISTORY = timedelta(days=3650)
# One token must outlast the whole rebuild, because the server has no refresh
# and a new login could ask for an MFA code.
REBUILD_TOKEN_MARGIN = 50 * 60
# A series whose history can never be rebuilt safely stops being retried.
REBUILD_MAX_REFUSALS = 3
# Existing rows this far past the walk's last point are data lag; more means
# the walk fell short.
REBUILD_MAX_TAIL = timedelta(hours=48)
# Releases before this wrote a broken sum chain in the regular update, so
# their rebuilt series are rebuilt once more.
CHAIN_FIXED_IN = (0, 3, 3)


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
        await self._async_sync_rebuild_version()
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

    async def _async_sync_rebuild_version(self) -> None:
        """Give a new release fresh rebuild tries, and redo series from old ones."""
        version = str((await async_get_integration(self.hass, DOMAIN)).version)
        data = self.config_entry.data
        stored = data.get(CONF_HOURLY_REBUILD_VERSION)
        if stored == version:
            return
        updated = {**data, CONF_HOURLY_REBUILD_VERSION: version}
        updated.pop(CONF_HOURLY_REBUILD_REFUSED, None)
        if CONF_HOURLY_REBUILD_DONE in data and (
            stored is None or self._version_tuple(stored) < CHAIN_FIXED_IN
        ):
            # Older releases left broken sums (and incomplete electric history).
            updated[CONF_HOURLY_REBUILD_DONE] = []
        self.hass.config_entries.async_update_entry(self.config_entry, data=updated)

    @staticmethod
    def _version_tuple(text: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in str(text).split(".")[:3])
        except ValueError:
            return ()  # unknown counts as older than any release

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
        now = dt_util.now(await dt_util.async_get_time_zone(TIME_ZONE))
        # Through tomorrow, so the walk certainly covers today's hours.
        end = now + timedelta(days=1)
        try:
            reads = await self.api.async_get_usage_reads(
                meter,
                AggregateType.HOUR,
                now - HOURLY_MAX_HISTORY,
                end,
                window_days=HOURLY_WINDOW_DAYS,
                strict=True,
                stop_after_empty=HOURLY_EMPTY_WINDOWS,
            )
            daily = await self.api.async_get_usage_reads(
                meter, AggregateType.DAY, now - DAILY_HISTORY, end, strict=True
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
        _LOGGER.info(
            "History walk for %s reached back to %s (last read %s)",
            meter.meter_type.name,
            self._local(reads[0].start_time),
            self._local(reads[-1].start_time),
        )
        # Electric hours without a read can still be covered by the daily totals.
        # Gas hours without a read are real "no usage" hours, so gas is used as
        # is. Today's daily total is partial, so it never takes part.
        if meter.meter_type == MeterType.ELEC:
            daily = [
                r
                for r in daily
                if r.start_time.astimezone(LOCAL_TZ).date() < now.date()
            ]
            reads = reconcile_hourly_with_daily(reads, daily)
        for statistic_id, (attr, label) in pending.items():
            if not valid_statistic_id(statistic_id):
                _LOGGER.warning("Invalid statistic id: %s", statistic_id)
                continue
            points = await self._plan_rebuild(
                statistic_id, self._hourly_points(reads, attr)
            )
            if points is None:
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

    @staticmethod
    def _local(moment: datetime) -> str:
        return moment.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")

    async def _plan_rebuild(self, statistic_id, points) -> list[StatisticData] | None:
        """The points to write, or None if old rows would keep a stale sum.

        Rows before the walk's first point would survive untouched, so refuse. Rows
        a few hours after its last point are data lag (live updates saw hours the
        walk did not), so they are carried into the same write with their own
        states, summed on from the rebuilt chain.
        """
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
        if before.get(statistic_id):
            oldest = datetime.fromtimestamp(
                before[statistic_id][0]["start"], tz=timezone.utc
            )
            _LOGGER.warning(
                "Not rebuilding %s: the history walk covered %s to %s, but there "
                "are rows before it (the oldest is %s)",
                statistic_id,
                self._local(first),
                self._local(last),
                self._local(oldest),
            )
            return None
        tail = await self._rows_after(statistic_id, last)
        if not tail:
            return points
        newest = datetime.fromtimestamp(tail[-1]["start"], tz=timezone.utc)
        if newest - last > REBUILD_MAX_TAIL:
            _LOGGER.warning(
                "Not rebuilding %s: the history walk ended at %s, but there are rows "
                "up to %s",
                statistic_id,
                self._local(last),
                self._local(newest),
            )
            return None
        carried = self._chain_rows(tail, points[-1]["sum"])
        _LOGGER.info(
            "%s: carrying %d existing rows after %s into the rebuild",
            statistic_id,
            len(carried),
            self._local(last),
        )
        return points + carried

    async def _rows_after(self, statistic_id: str, last: datetime) -> list[dict]:
        """Existing rows after the hour `last`, with their states."""
        after = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            last + timedelta(hours=1),
            None,
            {statistic_id},
            "hour",
            None,
            {"state"},
        )
        return after.get(statistic_id, [])

    @staticmethod
    def _chain_rows(rows: list[dict], running_sum: float) -> list[StatisticData]:
        """Keep each row's own state and sum them on from running_sum."""
        chained = []
        for row in rows:
            state = row["state"] or 0.0
            running_sum += state
            chained.append(
                StatisticData(
                    start=datetime.fromtimestamp(row["start"], tz=timezone.utc),
                    state=state,
                    sum=running_sum,
                )
            )
        return chained

    async def _sum_before(self, statistic_id: str, first: datetime) -> float:
        """The sum of the last row before `first`, or 0 when there is none."""
        first = first.astimezone(timezone.utc)
        for since in (
            first - timedelta(days=7),
            datetime.fromtimestamp(0, tz=timezone.utc),
        ):
            rows = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                since,
                first,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            if rows.get(statistic_id):
                return cast(float, rows[statistic_id][-1]["sum"])
        return 0.0

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
        else:
            usage_reads = await self._cached_reads(
                meter, last_stat[statistic_id][0]["start"]
            )
            if not usage_reads:
                return
            # Anchor on a row outside the window being rewritten, so every row in
            # it is replaced and none keeps a sum from another chain.
            running_sum = await self._sum_before(
                statistic_id, usage_reads[0].start_time
            )
        points = []
        for usage_read in usage_reads:
            value = getattr(usage_read, attr)
            running_sum += value
            points.append(
                StatisticData(start=usage_read.start_time, state=value, sum=running_sum)
            )
        if last_stat and points:
            # Rows already past the last hour read stay, summed on from this chain.
            points += self._chain_rows(
                await self._rows_after(statistic_id, points[-1]["start"]), running_sum
            )
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
        """Get usage reads, oldest first, never two reads for the same hour.

        If start_time is None, get usage reads since account activation, otherwise
        since start_time - 30 days to allow corrections in data from utilities.

        Hourly meters get hourly reads for the past 2 months, one per hour with zero
        for hours CSU has no read for. Daily reads only cover days before that
        window, and only on a first import: inside it, or on top of an hourly
        history, a daily total would overwrite hours that already have real reads.
        """
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
            start = max(start, end - timedelta(days=2 * 30))
        _LOGGER.debug(
            "Meter is hourly. Getting finer data stats for %s until %s", start, end
        )
        hourly_usage_reads = await self.api.async_get_usage_reads(
            meter, AggregateType.HOUR, start, end
        )
        if not hourly_usage_reads:
            # Nothing hourly to write; a first import still gets the daily history.
            return usage_reads if start_time is None else []
        # Only days inside the hourly window; older days stay daily.
        if meter.meter_type == MeterType.ELEC:
            hourly_usage_reads = reconcile_hourly_with_daily(
                hourly_usage_reads,
                [r for r in usage_reads if r.start_time.date() >= start.date()],
            )
        window_start = datetime.combine(start.date(), time.min, tzinfo=tz)
        hourly = dense_hourly_reads(meter, hourly_usage_reads, window_start)
        if start_time is not None:
            return hourly
        return [r for r in usage_reads if r.end_time <= window_start] + hourly


type CsuConfigEntry = ConfigEntry[CsuCoordinator]
