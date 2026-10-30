"""PSEG API Client for Home Assistant (New Jersey).

Uses session cookies to communicate directly with the PSE&G MySmartEnergy portal (MyMeter platform).
Supports both Electric (kWh) and Natural Gas (therms) data extraction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
import time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

from .const import DEFAULT_ELECTRIC_RATE, DEFAULT_GAS_RATE

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://mysmartenergy.nj.pseg.com"
# The portal's local time zone; MyMeter timestamps are wall-clock times in it.
PORTAL_TIMEZONE = ZoneInfo("America/New_York")


def portal_time_to_utc(x_ms: float, fold: int = 0) -> datetime:
    """Convert a MyMeter chart timestamp to an aware UTC datetime."""
    wall_clock = datetime.fromtimestamp(x_ms / 1000, tz=timezone.utc).replace(
        tzinfo=PORTAL_TIMEZONE, fold=fold
    )
    return wall_clock.astimezone(timezone.utc)


def point_value(pt: Dict[str, Any]) -> Optional[float]:
    """Return a chart point's `y` as a float, or None when it has no reading."""
    y = pt.get("y")
    if y is None or isinstance(y, bool):
        return None
    try:
        value = float(y)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


class PsegAuthError(Exception):
    """Exception raised for authentication or session failures."""


class PsegConnectionError(Exception):
    """Exception raised for network or communication failures with PSE&G."""


class PsegClient:
    """Client for PSE&G MySmartEnergy (Electric & Natural Gas)."""

    def __init__(
        self,
        cookie: str,
        user_configured_rate: Optional[float] = None,
        user_configured_gas_rate: Optional[float] = None,
    ) -> None:
        self.cookie = cookie
        try:
            self.user_configured_rate = (
                float(user_configured_rate)
                if user_configured_rate and float(user_configured_rate) > 0
                else None
            )
        except (ValueError, TypeError):
            self.user_configured_rate = None

        try:
            self.user_configured_gas_rate = (
                float(user_configured_gas_rate)
                if user_configured_gas_rate and float(user_configured_gas_rate) > 0
                else None
            )
        except (ValueError, TypeError):
            self.user_configured_gas_rate = None

        self.effective_rate = self.user_configured_rate or DEFAULT_ELECTRIC_RATE
        self.rate_source = "user_configured" if self.user_configured_rate else "default"
        self.rate_7d_avg: Optional[float] = None
        self.days_used_for_rate: int = 0

        self.effective_gas_rate = self.user_configured_gas_rate or DEFAULT_GAS_RATE
        self.gas_rate_source = "user_configured" if self.user_configured_gas_rate else "default"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{BASE_URL}/Dashboard",
        })
        self.update_cookie(cookie)

    def update_cookie(self, new_cookie: str) -> None:
        """Update active session cookies."""
        self.cookie = new_cookie
        self.session.headers.update({"Cookie": new_cookie})

    def test_connection(self) -> bool:
        """Validate whether current session cookie is authorized."""
        try:
            resp = self.session.get(f"{BASE_URL}/Dashboard", allow_redirects=True, timeout=20)
            if resp.status_code in (401, 403):
                raise PsegAuthError(f"HTTP status {resp.status_code} received from portal")
            if resp.status_code != 200:
                raise PsegConnectionError(f"HTTP status {resp.status_code} received from portal")
            if "signin" in resp.url.lower() or "login" in resp.url.lower():
                raise PsegAuthError("Session cookie expired or rejected by portal")
            return True
        except requests.RequestException as err:
            raise PsegConnectionError(f"Failed to connect to PSE&G: {err}") from err

    def _get_verification_token(self) -> str:
        """Extract __RequestVerificationToken from the /Dashboard page."""
        try:
            resp = self.session.get(f"{BASE_URL}/Dashboard", timeout=25)
            if resp.status_code in (401, 403):
                raise PsegAuthError(f"HTTP status {resp.status_code} received from portal")
            if resp.status_code != 200:
                raise PsegConnectionError(f"HTTP status {resp.status_code} received from portal")
            if "signin" in resp.url.lower() or "login" in resp.url.lower():
                raise PsegAuthError("Session expired: Redirected to login")
            dash_html = resp.text
        except requests.RequestException as err:
            raise PsegConnectionError(f"Network error on Dashboard: {err}") from err

        token_match = re.search(
            r'name=[\"\']__RequestVerificationToken[\"\'][^>]*value=[\"\']([^\"\']+)[\"\']',
            dash_html,
        )
        if not token_match:
            raise PsegAuthError("Session invalid: __RequestVerificationToken not found on /Dashboard")
        return token_match.group(1)

    def _query_chart(
        self,
        token: str,
        start_str: str,
        end_str: str,
        usage_type: str = "1",
        usage_interval: str = "5",
    ) -> Optional[Dict[str, Any]]:
        """Query chart data from MyMeter for a given UsageType and interval."""
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
        }

        chart_setup_data = {
            "__RequestVerificationToken": token,
            "UsageInterval": usage_interval,
            "UsageType": usage_type,
            "jsTargetName": "StorageType",
            "EnableHoverChart": "true",
            "Start": start_str,
            "End": end_str,
            "IsRangeOpen": "False",
            "MaintainMaxDate": "true",
            "SelectedViaDateRange": "False",
            "ChartComparison": "1",
            "ChartComparison2": "0",
            "ChartComparison3": "0",
            "ChartComparison4": "0",
        }

        try:
            resp2 = self.session.post(
                f"{BASE_URL}/Dashboard/Chart",
                data=chart_setup_data,
                headers=headers,
                timeout=25,
            )
            resp2.raise_for_status()

            ts = int(time.time() * 1000)
            resp3 = self.session.get(
                f"{BASE_URL}/Dashboard/ChartData",
                params={"_": ts},
                headers=headers,
                timeout=25,
            )
            resp3.raise_for_status()
            data = resp3.json()
            return data
        except Exception as err:
            _LOGGER.debug("Error querying chart (usage_type=%s, interval=%s): %s", usage_type, usage_interval, err)
            return None

    def fetch_history_range(
        self,
        start_date_str: str,
        end_date_str: str,
        chunk_days: int = 90,
    ) -> list[dict[str, Any]]:
        """Fetch historical usage and cost data across a date range in polite chunks."""
        token = self._get_verification_token()

        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
        except ValueError as err:
            raise ValueError(f"Invalid date format (expected YYYY-MM-DD): {err}") from err

        if start_dt > end_dt:
            start_dt, end_dt = end_dt, start_dt

        records_map: dict[str, dict[str, Any]] = {}
        curr_start = start_dt

        while curr_start <= end_dt:
            curr_end = min(curr_start + timedelta(days=chunk_days), end_dt)
            chunk_start_str = curr_start.strftime("%Y-%m-%d")
            chunk_end_str = curr_end.strftime("%Y-%m-%d")
            _LOGGER.info("Fetching PSE&G history chunk from %s to %s...", chunk_start_str, chunk_end_str)

            data = self._query_chart(
                token,
                chunk_start_str,
                chunk_end_str,
                usage_type="1",
                usage_interval="6",
            )

            if not data:
                curr_start = curr_end + timedelta(days=1)
                continue

            chart_data = data.get("Data")
            if not chart_data or not isinstance(chart_data, dict):
                ajax_results = data.get("AjaxResults") or []
                for ar in ajax_results:
                    if isinstance(ar, dict) and ar.get("Action") == "Redirect":
                        raise PsegAuthError("Session expired during history backfill: Redirected to login")
                _LOGGER.warning("No chart data returned for range %s to %s", chunk_start_str, chunk_end_str)
            else:
                series_list = chart_data.get("series", [])
                meter_series = {}
                for s in series_list:
                    if "Meter" in str(s.get("name") or ""):
                        meter_series = s
                        break

                pts = meter_series.get("data", [])
                for pt in pts:
                    if isinstance(pt, dict):
                        value = point_value(pt)
                        if value is None:
                            continue
                        kwh = round(value, 4)
                        hs = pt.get("hs", {})
                        p_start = hs.get("start", "")
                        try:
                            dt = datetime.strptime(p_start, "%m/%d/%Y")
                            date_iso = dt.strftime("%Y-%m-%d")
                        except Exception:
                            date_iso = p_start

                        x = pt.get("x", 0)
                        start_utc = portal_time_to_utc(x) if x else None
                        cost_val = round(kwh * self.effective_rate, 4)

                        records_map[date_iso] = {
                            "date": date_iso,
                            "start": start_utc,
                            "kwh": kwh,
                            "cost": cost_val,
                        }

            curr_start = curr_end + timedelta(days=1)
            if curr_start <= end_dt:
                time.sleep(0.75)

        sorted_records = [records_map[d] for d in sorted(records_map.keys())]
        _LOGGER.info(
            "Fetched %d historical records across range %s to %s",
            len(sorted_records),
            start_date_str,
            end_date_str,
        )
        return sorted_records

    def fetch_energy_data(self, days_back: int = 14) -> Dict[str, Any]:
        """Fetch electricity and natural gas usage and cost data."""
        token = self._get_verification_token()

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days_back)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        # 1. Fetch Electricity Data (UsageType: 1)
        kwh_data = self._query_chart(
            token,
            start_str,
            end_str,
            usage_type="1",
            usage_interval="5",
        )
        if not kwh_data:
            raise PsegConnectionError("Failed to retrieve electricity chart data from PSE&G")

        # 2. Fetch Gas Data (UsageType: 2)
        gas_data = self._query_chart(
            token,
            start_str,
            end_str,
            usage_type="2",
            usage_interval="5",
        )
        if not gas_data or not gas_data.get("Data", {}).get("series"):
            # Try daily interval for gas
            gas_data = self._query_chart(
                token,
                start_str,
                end_str,
                usage_type="2",
                usage_interval="6",
            )

        return self._parse_chart_data(kwh_data, gas_data=gas_data)

    def _parse_series_data(
        self,
        chart_data: Dict[str, Any],
        is_gas: bool = False,
    ) -> Dict[str, Any]:
        """Parse raw chart series data into records map and daily totals."""
        series_list = chart_data.get("series", [])
        if not series_list:
            return {}

        target_series = {}
        temp_series = {}

        for s in series_list:
            name = str(s.get("name") or "")
            unit = str(s.get("yAxisTitle") or s.get("unit") or "").lower()
            if "Temperature" in name:
                temp_series = s
            elif is_gas and any(k in name.lower() or k in unit for k in ("gas", "therm", "ccf")):
                target_series = s
            elif not is_gas and any(k in name.lower() or k in unit for k in ("meter", "electric", "kwh")):
                target_series = s

        # Fallback to first non-temperature series if not matched by name
        if not target_series:
            for s in series_list:
                if "Temperature" not in str(s.get("name") or ""):
                    target_series = s
                    break

        default_meter = "PSE&G Gas Meter" if is_gas else "PSE&G Electric Meter"
        meter_name = target_series.get("name") or default_meter
        detailed_pts = target_series.get("data", [])
        temp_pts = temp_series.get("data", [])

        effective_rate = self.effective_gas_rate if is_gas else self.effective_rate

        daily_usage_map: dict[str, float] = {}
        parsed_pts = []
        prev_x = None

        for pt in detailed_pts:
            if isinstance(pt, dict):
                x = pt.get("x", 0)
                start_utc = portal_time_to_utc(x, fold=1 if x == prev_x else 0)
                prev_x = x
                val = point_value(pt)
                if val is None:
                    continue
                usage = round(val, 4)

                hs = pt.get("hs", {})
                start_str = hs.get("start", "")
                try:
                    date_iso = datetime.strptime(start_str, "%m/%d/%Y").strftime("%Y-%m-%d")
                except Exception:
                    date_iso = start_utc.astimezone(PORTAL_TIMEZONE).strftime("%Y-%m-%d")

                daily_usage_map[date_iso] = round(daily_usage_map.get(date_iso, 0.0) + usage, 4)
                parsed_pts.append({
                    "start": start_utc,
                    "date": date_iso,
                    "usage": usage,
                    "x": x,
                })

        sorted_dates = sorted(daily_usage_map.keys())

        hourly_records = []
        for p in parsed_pts:
            u = p["usage"]
            cost_val = round(u * effective_rate, 4)
            rec = {
                "start": p["start"],
                "date": p["date"],
                "cost": cost_val,
                "is_exact_cost": False,
            }
            if is_gas:
                rec["therms"] = u
            else:
                rec["kwh"] = u
            hourly_records.append(rec)

        history_14d = []
        for d in sorted_dates:
            day_usage = round(daily_usage_map[d], 2)
            day_cost = round(day_usage * effective_rate, 2)
            rec = {
                "date": d,
                "cost": day_cost,
                "is_exact_cost": False,
            }
            if is_gas:
                rec["therms"] = day_usage
            else:
                rec["kwh"] = day_usage
            history_14d.append(rec)

        latest_record = history_14d[-1] if history_14d else {"date": "None", "kwh": 0.0, "therms": 0.0, "cost": 0.0, "is_exact_cost": False}
        prev_record = history_14d[-2] if len(history_14d) > 1 else {"date": "None", "kwh": 0.0, "therms": 0.0, "cost": 0.0, "is_exact_cost": False}

        latest_temp = temp_pts[-1] if temp_pts else {}
        temp_range = latest_temp.get("tr", "")
        temp_value = point_value(latest_temp)

        res = {
            "meter": meter_name,
            "latest_date": latest_record["date"],
            "latest_cost": latest_record["cost"],
            "previous_date": prev_record["date"],
            "previous_cost": prev_record["cost"],
            "history_14d": history_14d,
            "hourly_records": hourly_records,
            "temperature_range": temp_range,
            "temperature_avg": round(temp_value, 1) if temp_value is not None else None,
        }

        if is_gas:
            res["latest_therms"] = latest_record.get("therms", 0.0)
            res["previous_therms"] = prev_record.get("therms", 0.0)
        else:
            res["latest_kwh"] = latest_record.get("kwh", 0.0)
            res["previous_kwh"] = prev_record.get("kwh", 0.0)

        return res

    def _parse_chart_data(
        self,
        data: Dict[str, Any],
        gas_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Parse electricity and gas series into clean sensor models."""
        chart_data_elec = data.get("Data")
        if not chart_data_elec or not isinstance(chart_data_elec, dict):
            ajax_results = data.get("AjaxResults") or []
            for ar in ajax_results:
                if isinstance(ar, dict) and ar.get("Action") == "Redirect":
                    raise PsegAuthError("Session expired: Redirected to login portal")
            raise PsegConnectionError("No chart data returned from MyMeter. Unexpected portal response.")

        elec_parsed = self._parse_series_data(chart_data_elec, is_gas=False)

        # Parse gas data if present
        gas_parsed: Dict[str, Any] = {}
        if gas_data and isinstance(gas_data.get("Data"), dict):
            gas_parsed = self._parse_series_data(gas_data["Data"], is_gas=True)

        # Also check if gas series was returned within the electricity chart call
        if not gas_parsed:
            for s in chart_data_elec.get("series", []):
                name = str(s.get("name") or "").lower()
                unit = str(s.get("yAxisTitle") or s.get("unit") or "").lower()
                if any(k in name or k in unit for k in ("gas", "therm", "ccf")):
                    gas_parsed = self._parse_series_data({"series": [s]}, is_gas=True)
                    break

        # Fallback empty gas record if account is electric-only
        empty_gas_record = {
            "meter": "PSE&G Gas Meter",
            "latest_date": "None",
            "latest_therms": 0.0,
            "latest_cost": 0.0,
            "previous_date": "None",
            "previous_therms": 0.0,
            "previous_cost": 0.0,
            "history_14d": [],
            "hourly_records": [],
        }
        if not gas_parsed:
            gas_parsed = empty_gas_record

        # Combined payload
        payload = {
            # Electric fields
            "meter": elec_parsed.get("meter"),
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "latest_date": elec_parsed.get("latest_date"),
            "latest_kwh": elec_parsed.get("latest_kwh", 0.0),
            "latest_cost": elec_parsed.get("latest_cost", 0.0),
            "is_exact_latest_cost": False,
            "previous_date": elec_parsed.get("previous_date"),
            "previous_kwh": elec_parsed.get("previous_kwh", 0.0),
            "previous_cost": elec_parsed.get("previous_cost", 0.0),
            "history_14d": elec_parsed.get("history_14d", []),
            "hourly_records": elec_parsed.get("hourly_records", []),
            "temperature_range": elec_parsed.get("temperature_range"),
            "temperature_avg": elec_parsed.get("temperature_avg"),
            "effective_rate": self.effective_rate,
            "rate_source": self.rate_source,
            "rate_7d_avg": None,
            "days_used_for_rate": 0,
            # Natural Gas fields
            "gas_meter": gas_parsed.get("meter"),
            "latest_gas_date": gas_parsed.get("latest_date"),
            "latest_therms": gas_parsed.get("latest_therms", 0.0),
            "latest_gas_cost": gas_parsed.get("latest_cost", 0.0),
            "previous_gas_date": gas_parsed.get("previous_date"),
            "previous_therms": gas_parsed.get("previous_therms", 0.0),
            "previous_gas_cost": gas_parsed.get("previous_cost", 0.0),
            "gas_history_14d": gas_parsed.get("history_14d", []),
            "gas_hourly_records": gas_parsed.get("hourly_records", []),
            "effective_gas_rate": self.effective_gas_rate,
            "gas_rate_source": self.gas_rate_source,
        }

        return payload
