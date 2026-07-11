# -*- coding: utf-8 -*-
"""
Data layer for the "Dashboard Pro" — a new, separate dashboard that groups the
existing Baby Buddy cards into categories (feeding, diaper, sleep, ...), adds a
global period filter and predicts the next feeding/diaper/nap from the averages
Baby Buddy already computes.

This module never touches the classic dashboard; it only reads data.  All values
are pre-computed here and rendered by ``dashboard/child_pro.html`` so the template
stays logic-free and this can be unit-tested in isolation.
"""

from datetime import datetime, time as dtime, timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core import models
from core.models import Feeding
from dashboard.templatetags import cards as _cards

# Period filter options exposed in the UI (the querystring ``?period=``).
PERIODS = (
    "day",
    "yesterday",
    "3days",
    "week",
    "lastweek",
    "month",
    "lastmonth",
    "all",
)

# Averaging windows shown on the prediction cards. The predicted time itself is
# driven by the most recent window that has data (_PRED_DRIVER).
_PRED_WINDOWS = ("today", "yesterday", "3days", "7days")
_PRED_DRIVER = ("today", "3days", "7days")
_PRED_LABELS = {
    "today": _("Today"),
    "yesterday": _("Yesterday"),
    "3days": _("3 days"),
    "7days": _("7 days"),
}


# ---------------------------------------------------------------------------
# Period window
# ---------------------------------------------------------------------------
def _aware(naive):
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _day_bounds(d):
    """Return the (start, end) aware datetimes spanning the whole day ``d``."""
    return (
        _aware(datetime.combine(d, dtime.min)),
        _aware(datetime.combine(d, dtime.max)),
    )


def get_period_window(period):
    """
    Resolve the ``period`` preset into a concrete time window.

    Presets (relative to now, calendar-aware): day (today), yesterday, 3days,
    week (current week), lastweek, month (current month), lastmonth, all.

    :returns: a dict with ``period``, ``start`` / ``end`` (aware datetimes;
        ``start`` is ``None`` for "all"), ``start_date`` / ``end_date`` and
        ``is_live`` (predictions are only shown on "day" / today).
    """
    now = timezone.localtime()
    today = now.date()
    if period not in PERIODS:
        period = "day"

    if period == "yesterday":
        start, end = _day_bounds(today - timedelta(days=1))
    elif period == "3days":
        start, _ = _day_bounds(today - timedelta(days=2))
        _, end = _day_bounds(today)
    elif period == "week":
        monday = today - timedelta(days=today.weekday())
        start, _ = _day_bounds(monday)
        _, end = _day_bounds(today)
    elif period == "lastweek":
        monday = today - timedelta(days=today.weekday())
        start, _ = _day_bounds(monday - timedelta(days=7))
        _, end = _day_bounds(monday - timedelta(days=1))
    elif period == "month":
        start, _ = _day_bounds(today.replace(day=1))
        _, end = _day_bounds(today)
    elif period == "lastmonth":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        start, _ = _day_bounds(last_month_end.replace(day=1))
        _, end = _day_bounds(last_month_end)
    elif period == "all":
        start = None
        end = now
    else:
        period = "day"
        start, end = _day_bounds(today)

    return {
        "period": period,
        "start": start,
        "end": end,
        "start_date": start.date() if start else None,
        "end_date": end.date(),
        # Predictions are only meaningful "live" — the current day.
        "is_live": period == "day",
    }


def _in_window(qs, field, window):
    """Filter a queryset to the window on the given datetime ``field``."""
    if window["start"] is not None:
        return qs.filter(
            **{f"{field}__gte": window["start"], f"{field}__lte": window["end"]}
        )
    return qs.filter(**{f"{field}__lte": window["end"]})


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------
def _pred_ranges(now):
    """(start, end) bounds for each prediction window (``None`` = open bound)."""
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": (today0, None),
        "yesterday": (today0 - timedelta(days=1), today0),
        "3days": (today0 - timedelta(days=2), None),
        "7days": (today0 - timedelta(days=6), None),
    }


def _in_pred_range(t, bounds):
    start, end = bounds
    if start is not None and t < start:
        return False
    if end is not None and t >= end:
        return False
    return True


def _average_intervals(points, now):
    """
    Average gap between consecutive ``points`` (ascending datetimes) for each
    prediction window.  ``points[i] - points[i - 1]`` counts toward a window when
    the *previous* point falls inside that window's date range.
    """
    ranges = _pred_ranges(now)
    totals = {w: timedelta(0) for w in _PRED_WINDOWS}
    counts = {w: 0 for w in _PRED_WINDOWS}
    for i in range(1, len(points)):
        prev, cur = points[i - 1], points[i]
        gap = cur - prev
        for w, bounds in ranges.items():
            if _in_pred_range(prev, bounds):
                totals[w] += gap
                counts[w] += 1
    return {w: (totals[w] / counts[w] if counts[w] else None) for w in _PRED_WINDOWS}


def _average_gaps(pairs, now):
    """
    Average "away" gap for (start, end) ``pairs`` ascending by start:
    ``pairs[i].start - pairs[i - 1].end`` (e.g. awake duration between sleeps).
    """
    ranges = _pred_ranges(now)
    totals = {w: timedelta(0) for w in _PRED_WINDOWS}
    counts = {w: 0 for w in _PRED_WINDOWS}
    for i in range(1, len(pairs)):
        prev_end = pairs[i - 1][1]
        cur_start = pairs[i][0]
        gap = cur_start - prev_end
        if gap.total_seconds() < 0:
            continue
        for w, bounds in ranges.items():
            if _in_pred_range(prev_end, bounds):
                totals[w] += gap
                counts[w] += 1
    return {w: (totals[w] / counts[w] if counts[w] else None) for w in _PRED_WINDOWS}


def _build_prediction(anchor, averages, now):
    """Combine the last-event ``anchor`` with the most recent window that has data."""
    used = next((w for w in _PRED_DRIVER if averages.get(w)), None)
    if anchor is None or used is None:
        return None
    predicted = anchor + averages[used]
    eta = predicted - now
    return {
        "predicted": predicted,
        "anchor": anchor,
        "used": used,
        "applied": averages[used],
        "averages": averages,
        "display": [
            {"label": _PRED_LABELS[w], "value": averages.get(w), "used": w == used}
            for w in _PRED_WINDOWS
        ],
        "is_late": eta.total_seconds() < 0,
        "eta": abs(eta),
    }


def _feeding_prediction(child, now):
    feedings = list(models.Feeding.objects.filter(child=child).order_by("start"))
    if not feedings:
        return None
    use_end = Feeding.settings.feeding_diff_end
    points = [timezone.localtime(f.end if use_end else f.start) for f in feedings]
    return _build_prediction(points[-1], _average_intervals(points, now), now)


def _diaper_prediction(child, now):
    changes = list(models.DiaperChange.objects.filter(child=child).order_by("time"))
    if not changes:
        return None
    points = [timezone.localtime(c.time) for c in changes]
    return _build_prediction(points[-1], _average_intervals(points, now), now)


def _nap_prediction(child, now):
    """Next sleep = last wake time + average awake duration between sleeps."""
    sleeps = list(models.Sleep.objects.filter(child=child).order_by("start"))
    if not sleeps:
        return None
    pairs = [(timezone.localtime(s.start), timezone.localtime(s.end)) for s in sleeps]
    last_wake = pairs[-1][1]
    return _build_prediction(last_wake, _average_gaps(pairs, now), now)


# ---------------------------------------------------------------------------
# Per-day trend series (fixed 7-day mini chart ending at the window's end date)
# ---------------------------------------------------------------------------
def _day_of(series_index, end_date, days):
    return end_date - timedelta(days=(days - 1 - series_index))


def _count_series(times, end_date, days=7):
    """Per-day event counts (bar height + the count shown on the bar)."""
    raw = [0] * days
    for t in times:
        idx = (end_date - timezone.localtime(t).date()).days
        if 0 <= idx < days:
            raw[days - 1 - idx] += 1
    top = max(raw) or 1
    return [
        {
            "pct": round(100 * raw[i] / top),
            "count": raw[i],
            "date": _day_of(i, end_date, days),
            "today": _day_of(i, end_date, days) == end_date,
        }
        for i in range(days)
    ]


def _duration_series(pairs, end_date, days=7):
    """Per-day total duration (bar height + an ``Nh``/``Nm`` label on the bar)."""
    raw = [0.0] * days
    for dt, dur in pairs:
        if not dur:
            continue
        idx = (end_date - timezone.localtime(dt).date()).days
        if 0 <= idx < days:
            raw[days - 1 - idx] += dur.total_seconds()
    top = max(raw) or 1
    out = []
    for i in range(days):
        secs = raw[i]
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        label = f"{h}h" if h else (f"{m}m" if m else "")
        out.append(
            {
                "pct": round(100 * secs / top),
                "label": label,
                "date": _day_of(i, end_date, days),
                "today": _day_of(i, end_date, days) == end_date,
            }
        )
    return out


def _amount_series(pairs, end_date, days=7):
    """Per-day summed amount (bar height + the rounded amount on the bar)."""
    raw = [0.0] * days
    for dt, amt in pairs:
        if not amt:
            continue
        idx = (end_date - timezone.localtime(dt).date()).days
        if 0 <= idx < days:
            raw[days - 1 - idx] += amt
    top = max(raw) or 1
    return [
        {
            "pct": round(100 * raw[i] / top),
            "amount": round(raw[i]),
            "date": _day_of(i, end_date, days),
            "today": _day_of(i, end_date, days) == end_date,
        }
        for i in range(days)
    ]


def _diaper_series(changes, end_date, days=7):
    """Per-day diaper counts split by type (stacked wet/solid/dry + total)."""
    raw = [{"wet": 0, "solid": 0, "dry": 0, "changes": 0} for _ in range(days)]
    for c in changes:
        idx = (end_date - timezone.localtime(c.time).date()).days
        if 0 <= idx < days:
            slot = raw[days - 1 - idx]
            slot["changes"] += 1
            if c.wet:
                slot["wet"] += 1
            if c.solid:
                slot["solid"] += 1
            if not c.wet and not c.solid:
                slot["dry"] += 1
    top = max(s["changes"] for s in raw) or 1
    return [
        {
            "pct": round(100 * raw[i]["changes"] / top),
            "changes": raw[i]["changes"],
            "wet": raw[i]["wet"],
            "solid": raw[i]["solid"],
            "dry": raw[i]["dry"],
            "date": _day_of(i, end_date, days),
            "today": _day_of(i, end_date, days) == end_date,
        }
        for i in range(days)
    ]


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _feeding_section(child, window, now, prediction):
    qs = models.Feeding.objects.filter(child=child)
    if not qs.exists():
        return None
    last = qs.order_by("-start").first()
    in_window = _in_window(qs, "start", window)
    amount = sum(f.amount for f in in_window if f.amount) or 0

    left = in_window.filter(method__in=["left breast", "both breasts"]).count()
    right = in_window.filter(method__in=["right breast", "both breasts"]).count()
    breast_total = left + right

    pairs = list(qs.values_list("start", "amount"))
    starts = [p[0] for p in pairs]
    return {
        "last": last,
        "last_base": (last.end if Feeding.settings.feeding_diff_end else last.start),
        "prediction": prediction if window["is_live"] else None,
        "count": in_window.count(),
        "amount": round(amount) if amount else 0,
        "breast": (
            {
                "left": left,
                "right": right,
                "left_pct": round(100 * left / breast_total) if breast_total else 0,
                "right_pct": round(100 * right / breast_total) if breast_total else 0,
            }
            if breast_total
            else None
        ),
        "trend": _count_series(starts, window["end_date"]),
        # Per-day amount chart (only meaningful when amounts are recorded).
        "amount_trend": (
            _amount_series(pairs, window["end_date"])
            if any(a for _dt, a in pairs)
            else None
        ),
        # Most recent feeding methods (newest first).
        "recent_methods": list(qs.order_by("-start")[:3]),
    }


def _diaper_section(child, window, now, prediction):
    qs = models.DiaperChange.objects.filter(child=child)
    if not qs.exists():
        return None
    last = qs.order_by("-time").first()
    in_window = _in_window(qs, "time", window)
    wet = in_window.filter(wet=True).count()
    solid = in_window.filter(solid=True).count()
    empty = in_window.filter(wet=False, solid=False).count()
    total = wet + solid + empty
    # Last 7 days of changes (with wet/solid) for the per-type stacked chart.
    trend_start = _aware(
        datetime.combine(window["end_date"] - timedelta(days=6), dtime.min)
    )
    trend_end = _aware(datetime.combine(window["end_date"], dtime.max))
    trend_changes = qs.filter(time__gte=trend_start, time__lte=trend_end).only(
        "time", "wet", "solid"
    )
    return {
        "last": last,
        "prediction": prediction if window["is_live"] else None,
        "count": in_window.count(),
        "wet": wet,
        "solid": solid,
        "empty": empty,
        "wet_pct": round(100 * wet / total) if total else 0,
        "solid_pct": round(100 * solid / total) if total else 0,
        "empty_pct": round(100 * empty / total) if total else 0,
        "trend": _diaper_series(trend_changes, window["end_date"]),
    }


def _sleep_section(child, window, now, prediction):
    qs = models.Sleep.objects.filter(child=child)
    if not qs.exists():
        return None
    last = qs.order_by("-end").first()
    in_window = _in_window(qs, "start", window)
    total = timedelta(0)
    for s in in_window:
        if s.duration:
            total += s.duration

    naps_today = None
    if window["is_live"]:
        naps_today = list(
            models.Sleep.objects.filter(
                child=child,
                start__gte=window["start"],
                start__lte=window["end"],
            ).order_by("start")
        )

    pairs = list(qs.values_list("start", "duration"))
    return {
        "last": last,
        "prediction": prediction if window["is_live"] else None,
        "count": in_window.count(),
        "total": total,
        "naps_today": naps_today,
        "trend": _duration_series(pairs, window["end_date"]),
    }


def _pumping_section(child, window):
    qs = models.Pumping.objects.filter(child=child)
    if not qs.exists():
        return None
    last = qs.order_by("-start").first()
    in_window = _in_window(qs, "start", window)
    amount = sum(p.amount for p in in_window if p.amount) or 0
    return {
        "last": last,
        "count": in_window.count(),
        "amount": round(amount) if amount else 0,
        "trend": _amount_series(
            list(qs.values_list("start", "amount")), window["end_date"]
        ),
    }


def _tummytime_section(child, window):
    qs = models.TummyTime.objects.filter(child=child)
    if not qs.exists():
        return None
    last = qs.order_by("-end").first()
    in_window = _in_window(qs, "end", window)
    total = timedelta(0)
    for t in in_window:
        if t.duration:
            total += t.duration
    milestone = (
        qs.exclude(milestone="")
        .order_by("-end")
        .values_list("milestone", flat=True)
        .first()
    )
    return {
        "last": last,
        "count": in_window.count(),
        "total": total,
        "milestone": milestone,
        "sessions": list(in_window.order_by("-end")[:8]),
    }


def _medication_section(child, window):
    qs = models.Medication.objects.filter(child=child)
    if not qs.exists():
        return None
    # Latest administration per distinct medication name.
    meds = []
    seen = set()
    for m in qs.order_by("-time"):
        if m.name in seen:
            continue
        seen.add(m.name)
        meds.append(m)
    upcoming = [m for m in meds if m.next_dose_time]
    next_dose = min(upcoming, key=lambda m: m.next_dose_time) if upcoming else None
    return {
        "last": qs.order_by("-time").first(),
        "meds": meds,
        "next_dose": next_dose,
        "count": _in_window(qs, "time", window).count(),
    }


def _notes_section(child):
    notes = list(models.Note.objects.filter(child=child).order_by("-time")[:3])
    return notes or None


def _temperature_section(child, window):
    qs = models.Temperature.objects.filter(child=child)
    if not qs.exists():
        return None
    return {
        "last": qs.order_by("-time").first(),
        "count": _in_window(qs, "time", window).count(),
    }


def _growth_section(child):
    weight = models.Weight.objects.filter(child=child).order_by("-date").first()
    height = models.Height.objects.filter(child=child).order_by("-date").first()
    head = (
        models.HeadCircumference.objects.filter(child=child).order_by("-date").first()
    )
    bmi = models.BMI.objects.filter(child=child).order_by("-date").first()
    if not any([weight, height, head, bmi]):
        return None
    return {"weight": weight, "height": height, "head": head, "bmi": bmi}


def _timers_section(child):
    timers = list(models.Timer.objects.filter(child=child).order_by("-start"))
    return timers or None


# Types tracked by the dashboard, for the "not registered yet" section.
_NOT_REGISTERED_TYPES = [
    ("feeding", "core:feeding-add", "icon-feeding"),
    ("diaperchange", "core:diaperchange-add", "icon-diaperchange"),
    ("sleep", "core:sleep-add", "icon-sleep"),
    ("tummytime", "core:tummytime-add", "icon-tummytime"),
    ("pumping", "core:pumping-add", "icon-pumping"),
    ("medication", "core:medication-add", "icon-medication"),
    ("temperature", "core:temperature-add", "icon-temperature"),
    ("note", "core:note-add", "icon-note"),
]

_MODEL_FOR_TYPE = {
    "feeding": models.Feeding,
    "diaperchange": models.DiaperChange,
    "sleep": models.Sleep,
    "tummytime": models.TummyTime,
    "pumping": models.Pumping,
    "medication": models.Medication,
    "temperature": models.Temperature,
    "note": models.Note,
}


def _not_registered(child):
    missing = []
    for key, url_name, icon in _NOT_REGISTERED_TYPES:
        model = _MODEL_FOR_TYPE[key]
        if not model.objects.filter(child=child).exists():
            missing.append(
                {
                    "key": key,
                    "url_name": url_name,
                    "icon": icon,
                    "label": model._meta.verbose_name,
                }
            )
    return missing


def _statistics_section(child):
    """
    Averages that are not surfaced by the prediction cards, reusing the classic
    dashboard's statistics helpers (nap/sleep durations, weekly growth changes).
    """
    stats = []

    nap = _cards._nap_statistics(child)
    if nap:
        stats.append(
            {
                "kind": "duration",
                "value": nap["average"],
                "title": _("Average nap duration"),
            }
        )
        stats.append(
            {
                "kind": "float",
                "value": nap["avg_per_day"],
                "title": _("Average naps per day"),
            }
        )

    sleep = _cards._sleep_statistics(child)
    if sleep:
        stats.append(
            {
                "kind": "duration",
                "value": sleep["average"],
                "title": _("Average sleep duration"),
            }
        )
        stats.append(
            {
                "kind": "duration",
                "value": sleep["btwn_average"],
                "title": _("Average awake duration"),
            }
        )

    for helper, title in (
        (_cards._weight_statistics, _("Weight change per week")),
        (_cards._height_statistics, _("Height change per week")),
        (
            _cards._head_circumference_statistics,
            _("Head circumference change per week"),
        ),
        (_cards._bmi_statistics, _("BMI change per week")),
    ):
        result = helper(child)
        if result:
            stats.append(
                {"kind": "float", "value": result["change_weekly"], "title": title}
            )

    return stats or None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_context(child, period):
    """Assemble the full Dashboard Pro context for ``child``."""
    now = timezone.localtime()
    window = get_period_window(period)

    feeding_pred = _feeding_prediction(child, now)
    diaper_pred = _diaper_prediction(child, now)
    nap_pred = _nap_prediction(child, now)

    feeding = _feeding_section(child, window, now, feeding_pred)
    diaper = _diaper_section(child, window, now, diaper_pred)
    sleep = _sleep_section(child, window, now, nap_pred)
    medication = _medication_section(child, window)

    return {
        "window": window,
        "predictions": {
            "feeding": feeding_pred if window["is_live"] else None,
            "diaper": diaper_pred if window["is_live"] else None,
            "nap": nap_pred if window["is_live"] else None,
            "medication": (
                (medication["next_dose"] if medication else None)
                if window["is_live"]
                else None
            ),
        },
        "feeding": feeding,
        "diaper": diaper,
        "sleep": sleep,
        "pumping": _pumping_section(child, window),
        "tummytime": _tummytime_section(child, window),
        "medication": medication,
        "notes": _notes_section(child),
        "temperature": _temperature_section(child, window),
        "growth": _growth_section(child),
        "statistics": _statistics_section(child),
        "timers": _timers_section(child),
        "not_registered": _not_registered(child),
        "now": now,
    }
