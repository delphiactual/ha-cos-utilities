"""Colorado Springs Utilities API."""

import asyncio
import dataclasses
import json
import logging
import time
from datetime import datetime
from datetime import timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import arrow
from aiohttp.client_exceptions import ClientResponseError

from .const import TIME_ZONE
from .const import USER_AGENT
from .exceptions import CannotConnect
from .exceptions import InvalidAuth
from .exceptions import MfaRequired

TOKEN_URL = "https://myaccount.csu.org/rest/oauth/token"
# Accounts and services rarely change, and usage data lags by a day or more.
ACCOUNT_REFRESH_SECONDS = 24 * 60 * 60
# Seconds to wait before each retry of a failed history window.
RETRY_DELAYS = (30.0, 120.0, 300.0)
# Built once at import: constructing it per call reads tzdata from disk, which
# blocks the event loop when called from async code.
LOCAL_TZ = ZoneInfo(TIME_ZONE)

_LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class CsuCustomer:
    """Data about a customer."""

    customer_id: str
    customer_context: dict


class AggregateType(Enum):
    """How to aggregate historical data."""

    BILL = "bill"
    DAY = "day"
    HOUR = "hour"
    QUARTER = "quarter"

    def __str__(self):
        """Return the value of the enum."""
        return self.value


class MeterType(Enum):
    """Meter type. Electric, gas, or water."""

    ELEC = "ELEC"
    GAS = "GAS"
    WATER = "WATER"

    def __str__(self):
        """Return the value of the enum."""
        return self.value


class ReadResolution(Enum):
    """Minimum supported resolution."""

    BILLING = "BILLING"
    DAY = "DAY"
    HOUR = "HOUR"
    QUARTER_HOUR = "QUARTER_HOUR"

    def __str__(self):
        """Return the value of the enum."""
        return self.value


SUPPORTED_AGGREGATE_TYPES = {
    ReadResolution.BILLING: [AggregateType.BILL],
    ReadResolution.DAY: [AggregateType.BILL, AggregateType.DAY],
    ReadResolution.HOUR: [AggregateType.BILL, AggregateType.DAY, AggregateType.HOUR],
    ReadResolution.QUARTER_HOUR: [
        AggregateType.BILL,
        AggregateType.DAY,
        AggregateType.HOUR,
    ],
}


@dataclasses.dataclass
class Meter:
    """Data about a Meter."""

    customer: CsuCustomer
    meter_number: int
    service_number: str
    service_id: str
    contract_num: str
    contract_account_number: str
    meter_type: MeterType
    read_resolution: ReadResolution | None = None


@dataclasses.dataclass
class UsageRead:
    """A read from the meeter that has consumption data."""

    meter: Meter
    start_time: datetime
    end_time: datetime
    consumption: float
    received: float = 0.0  # kWh exported (net-metered ELEC only), positive


@dataclasses.dataclass
class CostRead:
    """A read from the meter that has both consumption and cost data."""

    meter: Meter
    start_time: datetime
    end_time: datetime
    consumption: float  # taken from value field, in KWH or CCF
    provided_cost: float  # in $


class CSU:
    """Class that can get historical usage/cost from Colorado Springs Utilities."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        mfa: str = "",
        access_token: str = "",
        token_expires_at: float = 0.0,
        customer_id: str = "",
    ) -> None:
        """Initialize the class."""

        self.session = session
        self.username = username
        self.password = password
        self.mfa = mfa
        # Epoch seconds, so a token saved by the config flow survives restarts.
        self.customer_id = customer_id
        self.access_token = access_token
        self.token_expires_at = token_expires_at
        self._accounts_fetched_at = 0.0
        self.customers = []
        self.meters = []

    async def _async_request_token(self, fields: dict[str, str]) -> dict[str, Any]:
        """Call the OAuth token endpoint, mapping failures to our exceptions."""

        data = aiohttp.FormData()
        for key, value in fields.items():
            data.add_field(key, value)
        try:
            async with self.session.post(
                TOKEN_URL,
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": "Basic d2ViQ2xpZW50SWRQYXNzd29yZDpzZWNyZXQ=",
                },
            ) as resp:
                status = resp.status
                result = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CannotConnect(err) from err
        if status in (400, 401, 403) or "error" in result or "errorMsg" in result:
            # The server reports a missing/needed one-time code as an invalid_grant
            # whose description starts with BUS-MFAR, and has already sent the code.
            description = str(
                result.get("error_description") or result.get("errorMsg") or status
            )
            if description.startswith("BUS-MFAR"):
                raise MfaRequired(description)
            raise InvalidAuth(description)
        if status >= 400:
            raise CannotConnect(f"HTTP {status}")
        return result

    async def async_send_mfa_code(self, mfa_token: str) -> None:
        """Ask CSU to email the one-time code for a BUS-MFAR challenge.

        The token request itself sends nothing; the portal makes this separate call.
        """
        try:
            async with self.session.post(
                "https://myaccount.csu.org/rest/auth/mfa/verificationcode",
                data=json.dumps({"mfaToken": mfa_token, "customerId": "guest"}),
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/json",
                },
                raise_for_status=True,
            ):
                pass
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(err) from err

    async def async_ensure_token(self, min_valid: float) -> bool:
        """Renew the token unless it stays valid for at least min_valid seconds."""
        return await self._async_ensure_token(min_valid)

    async def _async_ensure_token(self, min_valid: float = 60) -> bool:
        """Make sure the access token is valid; return True if a new one was issued."""

        # The server does not support refresh tokens, and a login may need an MFA
        # code, so keep logins to about one per token lifetime.
        if self.access_token and time.time() < self.token_expires_at - min_valid:
            return False
        result = await self._async_request_token(
            {
                "username": self.username,
                "password": self.password,
                # SKIP is what the portal sends when it has no code. The server
                # accepts it when no code is needed, whereas an empty value
                # always demands one.
                "mfa": self.mfa or "SKIP",
                "grant_type": "password",
            }
        )
        self.mfa = ""
        self.access_token = result["access_token"]
        self.token_expires_at = time.time() + float(result.get("expires_in", 3600))
        self.customer_id = result["user"]["customerId"]
        return True

    async def async_login(self) -> None:
        """Make sure we have a valid token and a current account context."""

        renewed = await self._async_ensure_token()
        if (
            not renewed
            and self.customers
            and time.time() - self._accounts_fetched_at < ACCOUNT_REFRESH_SECONDS
        ):
            return
        try:
            await self._async_fetch_accounts()
        except InvalidAuth:
            if renewed:
                raise
            # A saved token the server no longer accepts: log in once more.
            self.access_token = ""
            await self._async_ensure_token()
            await self._async_fetch_accounts()

    async def _async_fetch_accounts(self) -> None:
        """Fetch the account context and drop cached meters so they reload."""

        customerId = self.customer_id

        try:
            async with self.session.post(
                "https://myaccount.csu.org/rest/account/list/",
                data=json.dumps({"customerId": customerId}),
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": "Bearer " + self.access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                status = resp.status
                result = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise CannotConnect(err) from err
        if not isinstance(result, dict):
            raise CannotConnect("Unexpected accounts response")
        # Only a real rejection means the credentials need attention; anything
        # else (outage, error page, odd payload) should just be retried later.
        if (
            status in (401, 403)
            or result.get("error") == "invalid_token"
            or "errorMsg" in result
        ):
            raise InvalidAuth(result.get("errorMsg") or result.get("error") or status)
        if "account" not in result:
            raise CannotConnect(f"Unexpected accounts response (HTTP {status})")
        customerContext = result["account"][0]
        # login() runs on every refresh; don't accumulate duplicate customers
        self.customers = []
        _LOGGER.info("Customer ID: %s", customerId)
        self.customers.append(
            CsuCustomer(customer_id=customerId, customer_context=customerContext)
        )
        # async_get_meters appends, so clear first; callers reload lazily.
        self.meters = []
        self._accounts_fetched_at = time.time()

    async def async_get_meters(self) -> None:
        """Get meters for the customer."""

        try:
            async with self.session.post(
                "https://myaccount.csu.org/rest/account/services/",
                data=json.dumps(
                    {
                        "customerId": self.customers[0].customer_id,
                        "multiAcctLimit": 10,
                        "accountContext": self.customers[0].customer_context,
                    }
                ),
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": "Bearer " + self.access_token,
                    "Content-Type": "application/json",
                },
            ) as resp:
                result = await resp.json()
                if "errorMsg" in result:
                    raise InvalidAuth(result["errorMsg"])

                meterResult = result["accountSummaryType"]["servicesForGraph"]
                # _LOGGER.info("Meters: %s", meterResult)
                for meter in meterResult:
                    # Classify by prefix: accounts use E-TYPICAL/E-NET (net metering), W-TYPICAL/W-STRAIGHT, etc.
                    svc = str(meter.get("serviceNumber") or "")
                    if svc.startswith("G-"):
                        meterType = MeterType.GAS
                        readFrequency = ReadResolution.HOUR
                    elif svc.startswith("W-"):
                        meterType = MeterType.WATER
                        readFrequency = ReadResolution.DAY
                    elif svc.startswith("E-"):
                        meterType = MeterType.ELEC
                        readFrequency = ReadResolution.QUARTER_HOUR
                    else:
                        _LOGGER.warning("Skipping unknown CSU service type %s", svc)
                        continue
                    self.meters.append(
                        Meter(
                            customer=self.customers[0],
                            meter_number=meter["meterNumber"],
                            service_number=meter["serviceNumber"],
                            service_id=meter["serviceId"],
                            contract_num=meter["serviceContract"],
                            contract_account_number=meter["contractAccountNumber"],
                            meter_type=meterType,
                            read_resolution=readFrequency,
                        )
                    )
            # _LOGGER.info("Meters Object: %s", self.meters)
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise InvalidAuth(err) from err
            raise CannotConnect(err) from err

    async def async_get_bill_history(
        self,
        meter: Meter,
    ) -> list[CostRead]:
        """Get Billing History for a Meter"""

        reads = await self._async_fetch(
            meter=meter,
            url="https://myaccount.csu.org/rest/usage/",
            body={
                "customerId": meter.customer.customer_id,
                "serviceId": meter.service_id,
                "accountContext": {
                    "serviceId": meter.contract_num,  # servicecontract
                    "accountNumber": meter.contract_account_number,  # contractaccountnumber
                },
            },
        )

        result = []

        for read in reads:
            end_date = datetime.strptime(read["chargeDateRaw"], "%d-%b-%Y")
            start_date = end_date - timedelta(days=int(read["daysOfService"]))
            consumption = read["rawBilledConsumption"]
            cost = read["billedCharge"]
            result.append(
                CostRead(
                    meter=meter,
                    start_time=start_date,
                    end_time=end_date,
                    consumption=consumption,
                    provided_cost=cost,
                )
            )

        return result

    async def async_get_usage_reads(
        self,
        meter: Meter,
        aggregate_type: AggregateType,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        window_days: int | None = None,
        strict: bool = False,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> list[UsageRead]:
        """Get usage reads for a meter.

        strict is for long history walks: errors raise (after retries) instead of
        looking like the end of the history, so a partial result is never mistaken
        for a complete one.
        """

        if not meter.read_resolution:
            raise ValueError("Meter does not have a read resolution")
        if aggregate_type not in SUPPORTED_AGGREGATE_TYPES[meter.read_resolution]:
            raise ValueError(f"Unsupported aggregate type for {meter.read_resolution}")
        if not start_date:
            raise ValueError("start_date is required")
        if not end_date:
            raise ValueError("end_date is required")
        if start_date > end_date:
            raise ValueError("start_date must be before end_date")

        reads = await self.async_get_dated_data(
            meter=meter,
            url="https://myaccount.csu.org/rest/usage/detail/",
            aggregate_type=aggregate_type,
            start_date=start_date,
            end_date=end_date,
            window_days=window_days,
            strict=strict,
            retry_delays=retry_delays,
        )
        # Key on usageDate, which is always populated. readDateTime is
        # null for export-only (net-metered) intervals, which broke hourly bucketing and dropped
        # export entirely. Interval labels are interval END times; daily labels are the day.
        # Missing reads (no import and no received value) are skipped, never date-shifted.
        DEN = LOCAL_TZ
        seen: dict[str, dict] = {}
        for read in reads:
            label = read.get("usageDate")
            if label:
                seen[label] = read  # dedupe overlapping request windows
        buckets: dict[datetime, list[float]] = {}
        for label, read in seen.items():
            imp = read.get("usageConsumptionValue")
            rec = read.get("netReceivedValue")
            if imp is None and rec in (None, ""):
                continue
            imp = float(imp or 0.0)
            rec = -float(rec) if rec not in (None, "") else 0.0
            if aggregate_type == AggregateType.DAY:
                start = datetime.fromisoformat(label[:10]).replace(tzinfo=DEN)
            else:
                end = datetime.fromisoformat(label).replace(tzinfo=DEN)
                step = int(read.get("intervalType") or 60)
                start = (end - timedelta(minutes=step)).replace(minute=0)
            bucket = buckets.setdefault(start, [0.0, 0.0])
            bucket[0] += imp
            bucket[1] += rec
        result = []
        span = (
            timedelta(days=1)
            if aggregate_type == AggregateType.DAY
            else timedelta(hours=1)
        )
        for start in sorted(buckets):
            imp, rec = buckets[start]
            result.append(
                UsageRead(
                    meter=meter,
                    start_time=start,
                    end_time=start + span,
                    consumption=round(imp, 4),
                    received=round(rec, 4),
                )
            )
        return result

    async def async_get_dated_data(
        self,
        meter: Meter,
        url: str,
        aggregate_type: AggregateType,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        window_days: int | None = None,
        strict: bool = False,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> list[Any]:
        """Break large _async_fetch requests into smaller batches."""

        if start_date is None:
            raise ValueError("start_date is required")
        if end_date is None:
            raise ValueError("end_date is required")

        start = start_date.date()
        end = end_date.date()

        max_request_days = 30
        if aggregate_type == AggregateType.DAY:
            max_request_days = 60
            url_end = "month"
        if aggregate_type in {AggregateType.QUARTER, AggregateType.HOUR}:
            max_request_days = 1
            url_end = "day"

        if window_days is not None:
            max_request_days = window_days

        url = url + url_end

        result: list[Any] = []
        req_end = end
        while True:
            req_start = start
            if max_request_days is not None:
                req_start = max(start, (req_end - timedelta(days=max_request_days)))
            if req_start >= req_end:
                return result
            if strict:
                reads = await self._async_fetch_strict(
                    meter, url, req_start, req_end, retry_delays
                )
            else:
                reads = await self._async_fetch(meter, url, req_start, req_end)
            if not reads:
                return result
            result = reads + result
            req_end = req_end - timedelta(days=max_request_days)

    async def _async_fetch(
        self,
        meter: Meter,
        url: str,
        start_date: datetime | arrow.Arrow | None = None,
        end_date: datetime | arrow.Arrow | None = None,
        body: dict | None = None,
    ) -> list[Any]:
        if not body:
            data = {
                "customerId": meter.customer.customer_id,
                "meterNumber": meter.meter_number,
                "serviceNumber": meter.service_number,
                "serviceId": meter.service_id,
                "accountContext": meter.customer.customer_context,
                "contractNum": meter.contract_num,
            }
        else:
            data = body
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"

        if start_date:
            data["fromDate"] = start_date.strftime("%Y-%m-%d %H:%M")
        if end_date:
            data["toDate"] = end_date.strftime("%Y-%m-%d %H:%M")
        try:
            async with self.session.post(
                url, data=json.dumps(data), headers=headers, raise_for_status=True
            ) as resp:
                result = await resp.json()
                # if DEBUG_LOG_RESPONSE:
                #   _LOGGER.debug("Fetched: %s", json.dumps(result,indent=2))
                return result["history"]
        except ClientResponseError as err:
            # Ignore server errors for BILL requests
            # that can happen if end_date is before account activation
            if err.status == 500:
                return []
            raise

    async def _async_fetch_strict(
        self,
        meter: Meter,
        url: str,
        start_date: datetime,
        end_date: datetime,
        retry_delays: tuple[float, ...],
    ) -> list[Any]:
        """Fetch one window, retrying failures; never report an error as empty."""
        for attempt in range(len(retry_delays) + 1):
            try:
                async with self.session.post(
                    url,
                    data=json.dumps(self._fetch_body(meter, start_date, end_date)),
                    headers=self._json_headers(),
                ) as resp:
                    if resp.status in (401, 403):
                        raise InvalidAuth(f"HTTP {resp.status}")
                    if resp.status >= 400:
                        raise CannotConnect(f"HTTP {resp.status}")
                    result = await resp.json(content_type=None)
                    if not isinstance(result, dict) or "history" not in result:
                        raise CannotConnect("unexpected usage response")
                    return result["history"]
            except (
                CannotConnect,
                aiohttp.ClientError,
                TimeoutError,
                ValueError,
            ) as err:
                if attempt == len(retry_delays):
                    if isinstance(err, CannotConnect):
                        raise
                    raise CannotConnect(err) from err
                await asyncio.sleep(retry_delays[attempt])
        raise AssertionError("unreachable")

    def _fetch_body(self, meter: Meter, start_date, end_date) -> dict:
        return {
            "customerId": meter.customer.customer_id,
            "meterNumber": meter.meter_number,
            "serviceNumber": meter.service_number,
            "serviceId": meter.service_id,
            "accountContext": meter.customer.customer_context,
            "contractNum": meter.contract_num,
            "fromDate": start_date.strftime("%Y-%m-%d %H:%M"),
            "toDate": end_date.strftime("%Y-%m-%d %H:%M"),
        }

    def _json_headers(self) -> dict:
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        return headers

    def _get_headers(self):
        headers = {"User-Agent": USER_AGENT}
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        return headers
