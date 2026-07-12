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

from django.db.models import Avg
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
# driven by the first stable window that has data (_PRED_DRIVER); "today" and
# "yesterday" are comparison-only (too sparse to drive a reliable estimate).
_PRED_WINDOWS = ("today", "yesterday", "3days", "7days", "14days", "30days")
_PRED_DRIVER = ("3days", "7days", "14days", "30days")
_PRED_LABELS = {
    "today": _("Today"),
    "yesterday": _("Yesterday"),
    "3days": _("3 days"),
    "7days": _("7 days"),
    "14days": _("14 days"),
    "30days": _("30 days"),
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
        "14days": (today0 - timedelta(days=13), None),
        "30days": (today0 - timedelta(days=29), None),
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
        "used_label": _PRED_LABELS[used],
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


def _breastfeeding_series(feedings, end_date, days=7):
    """Per-day breastfeeding split by breast (left/right counts + duration)."""
    raw = [
        {"left": 0, "right": 0, "count": 0, "duration": timedelta(0)}
        for _ in range(days)
    ]
    for f in feedings:
        idx = (end_date - timezone.localtime(f.start).date()).days
        if 0 <= idx < days:
            slot = raw[days - 1 - idx]
            slot["count"] += 1
            if f.method in ("left breast", "both breasts"):
                slot["left"] += 1
            if f.method in ("right breast", "both breasts"):
                slot["right"] += 1
            if f.duration:
                slot["duration"] += f.duration
    return [
        {
            "left": raw[i]["left"],
            "right": raw[i]["right"],
            "count": raw[i]["count"],
            "duration": raw[i]["duration"],
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
# All-time averages ("Averages" cards)
# ---------------------------------------------------------------------------
# Number of days each averaging window spans (for "per day" figures).
_AVG_WINDOW_DAYS = {
    "today": 1,
    "yesterday": 1,
    "3days": 3,
    "7days": 7,
    "14days": 14,
    "30days": 30,
}


def _fmt_dur(td):
    """Compact duration for table cells: ``2h05`` / ``25min`` / ``—``."""
    if not td:
        return "—"
    secs = int(td.total_seconds())
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h{m:02d}" if h else f"{m}min"


def _fmt_float(value, decimals=1):
    return f"{value:.{decimals}f}" if value else "—"


def _win_filter(qs, field, bounds):
    start, end = bounds
    if start is not None:
        qs = qs.filter(**{f"{field}__gte": start})
    if end is not None:
        qs = qs.filter(**{f"{field}__lt": end})
    return qs


def _feeding_averages(child, now):
    qs = models.Feeding.objects.filter(child=child)
    if not qs.exists():
        return None
    ranges = _pred_ranges(now)
    use_end = Feeding.settings.feeding_diff_end
    points = [
        timezone.localtime(f.end if use_end else f.start) for f in qs.order_by("start")
    ]
    intervals = _average_intervals(points, now)
    rows = []
    for w in _PRED_WINDOWS:
        wqs = _win_filter(qs, "start", ranges[w])
        agg = wqs.aggregate(duration=Avg("duration"), amount=Avg("amount"))
        count = wqs.count()
        rows.append(
            {
                "label": _PRED_LABELS[w],
                "cells": [
                    _fmt_dur(intervals.get(w)),
                    _fmt_dur(agg["duration"]),
                    (f"{round(agg['amount'])}" if agg["amount"] else "—"),
                    _fmt_float(count / _AVG_WINDOW_DAYS[w] if count else None),
                ],
            }
        )
    return {
        "cols": [_("Interval"), _("Duration"), _("Amount"), _("/day")],
        "rows": rows,
    }


def _diaper_averages(child, now):
    qs = models.DiaperChange.objects.filter(child=child)
    if not qs.exists():
        return None
    ranges = _pred_ranges(now)
    points = [timezone.localtime(c.time) for c in qs.order_by("time")]
    intervals = _average_intervals(points, now)
    rows = []
    for w in _PRED_WINDOWS:
        wqs = _win_filter(qs, "time", ranges[w])
        count = wqs.count()
        rows.append(
            {
                "label": _PRED_LABELS[w],
                "cells": [
                    _fmt_dur(intervals.get(w)),
                    _fmt_float(count / _AVG_WINDOW_DAYS[w] if count else None),
                ],
            }
        )
    return {"cols": [_("Interval"), _("/day")], "rows": rows}


def _sleep_averages(child, now):
    qs = models.Sleep.objects.filter(child=child)
    if not qs.exists():
        return None
    ranges = _pred_ranges(now)
    rows = []
    for w in _PRED_WINDOWS:
        wqs = _win_filter(qs, "start", ranges[w])
        nap_qs = wqs.filter(nap=True)
        nap_count = nap_qs.count()
        rows.append(
            {
                "label": _PRED_LABELS[w],
                "cells": [
                    _fmt_dur(nap_qs.aggregate(d=Avg("duration"))["d"]),
                    _fmt_dur(wqs.aggregate(d=Avg("duration"))["d"]),
                    _fmt_float(nap_count / _AVG_WINDOW_DAYS[w] if nap_count else None),
                ],
            }
        )
    return {"cols": [_("Nap"), _("Sleep"), _("Naps/day")], "rows": rows}


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

    # Last 7 days of breastfeedings for the per-day left/right breakdown.
    trend_start = _aware(
        datetime.combine(window["end_date"] - timedelta(days=6), dtime.min)
    )
    trend_end = _aware(datetime.combine(window["end_date"], dtime.max))
    breast_feedings = qs.filter(
        method__in=["left breast", "right breast", "both breasts"],
        start__gte=trend_start,
        start__lte=trend_end,
    ).only("start", "method", "duration")
    breast_series = _breastfeeding_series(breast_feedings, window["end_date"])
    has_breast = any(d["left"] or d["right"] for d in breast_series)

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
        "breast_trend": breast_series if has_breast else None,
        # Per-day amount chart (only meaningful when amounts are recorded).
        "amount_trend": (
            _amount_series(pairs, window["end_date"])
            if any(a for _dt, a in pairs)
            else None
        ),
        # Most recent feeding methods (newest first).
        "recent_methods": list(qs.order_by("-start")[:3]),
        "averages": _feeding_averages(child, now),
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
        "averages": _diaper_averages(child, now),
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

    sleeps_today = None
    if window["is_live"]:
        sleeps_today = list(
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
        "sleeps_today": sleeps_today,
        "trend": _duration_series(pairs, window["end_date"]),
        "averages": _sleep_averages(child, now),
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
# Day timeline (one 0–24h row per day)
# ---------------------------------------------------------------------------
# Long windows ("month" / "all") are capped to this many day-rows so the page
# stays reasonable; the template shows a "last N of M days" note when it bites.
_TIMELINE_CAP_DAYS = 31


def _hm(dt):
    """Local ``HH:MM`` for a marker/segment label."""
    return timezone.localtime(dt).strftime("%H:%M")


def _earliest_event_date(child):
    """Local date of the child's very first timeline-relevant event (or ``None``)."""
    firsts = []
    for model, field in (
        (models.Sleep, "start"),
        (models.Feeding, "start"),
        (models.DiaperChange, "time"),
        (models.Medication, "time"),
        (models.TummyTime, "start"),
    ):
        dt = (
            model.objects.filter(child=child)
            .order_by(field)
            .values_list(field, flat=True)
            .first()
        )
        if dt:
            firsts.append(timezone.localtime(dt).date())
    return min(firsts) if firsts else None


def _timeline_density(rendered):
    """(row height px, dense flag) so rows stay legible as the day count grows."""
    if rendered <= 1:
        return 42, False
    if rendered <= 3:
        return 32, False
    if rendered <= 7:
        return 24, False
    if rendered <= 14:
        return 17, True
    return 11, True


def _timeline_section(child, window):
    """
    Per-day rows for the horizontal day timeline.  Each row spans 00–24h with
    sleep drawn as filled blocks (night vs. nap), the awake time being the gaps,
    and feeding/diaper/medication/tummy-time as point markers.  Sleep that crosses
    midnight is clipped per day, so a 22h→06h sleep shows on both days.
    """
    end_date = window["end_date"]
    if window["start_date"] is not None:
        total_days = (end_date - window["start_date"]).days + 1
    else:
        earliest = _earliest_event_date(child)
        if earliest is None:
            return None
        total_days = (end_date - earliest).days + 1
    total_days = max(total_days, 1)

    rendered = min(total_days, _TIMELINE_CAP_DAYS)
    render_start = end_date - timedelta(days=rendered - 1)
    hidden = total_days - rendered

    range_start = _aware(datetime.combine(render_start, dtime.min))
    range_end = _aware(datetime.combine(end_date, dtime.max))

    today = timezone.localdate()
    rows = []
    index = {}
    for i in range(rendered):
        d = render_start + timedelta(days=i)
        row = {"date": d, "today": d == today, "sleeps": [], "markers": []}
        rows.append(row)
        index[d] = row

    def _day0(d):
        return _aware(datetime.combine(d, dtime.min))

    # Sleep segments, clipped to each day (this is what splits across midnight).
    sleeps = (
        models.Sleep.objects.filter(
            child=child, start__lte=range_end, end__gte=range_start
        )
        .only("start", "end", "nap")
        .order_by("start")
    )
    for s in sleeps:
        s_end = s.end or timezone.now()
        d = timezone.localtime(s.start).date()
        last = timezone.localtime(s_end).date()
        while d <= last:
            row = index.get(d)
            if row is not None:
                base = _day0(d)
                seg_start = max(s.start, base)
                seg_end = min(s_end, base + timedelta(days=1))
                if seg_end > seg_start:
                    start_h = (seg_start - base).total_seconds() / 3600
                    end_h = (seg_end - base).total_seconds() / 3600
                    row["sleeps"].append(
                        {
                            "left": round(start_h / 24 * 100, 3),
                            "width": round((end_h - start_h) / 24 * 100, 3),
                            "nap": s.nap,
                            "cat": _("Nap") if s.nap else _("Night sleep"),
                            "time": f"{_hm(seg_start)}–{_hm(seg_end)}",
                            "detail": _fmt_dur(seg_end - seg_start),
                        }
                    )
            d += timedelta(days=1)

    # Point markers (feeding / diaper / medication / tummy time).  Each carries a
    # short ``detail`` used by the hover tooltip.
    def _marker(dt, kind, cat, detail):
        lt = timezone.localtime(dt)
        row = index.get(lt.date())
        if row is None:
            return
        frac = (dt - _day0(lt.date())).total_seconds() / 3600
        row["markers"].append(
            {
                "left": round(frac / 24 * 100, 3),
                "kind": kind,
                "cat": cat,
                "time": lt.strftime("%H:%M"),
                "detail": detail,
            }
        )

    for f in models.Feeding.objects.filter(
        child=child, start__gte=range_start, start__lte=range_end
    ).only("start", "method", "amount"):
        detail = str(f.get_method_display())
        if f.amount:
            detail = f"{detail} · {round(f.amount)} ml"
        _marker(f.start, "feed", _("Feeding"), detail)

    for c in models.DiaperChange.objects.filter(
        child=child, time__gte=range_start, time__lte=range_end
    ).only("time", "wet", "solid"):
        attrs = []
        if c.wet:
            attrs.append(str(_("Wet")))
        if c.solid:
            attrs.append(str(_("Solid")))
        if not attrs:
            attrs.append(str(_("Dry")))
        _marker(c.time, "diap", _("Diaper"), " / ".join(attrs))

    for m in models.Medication.objects.filter(
        child=child, time__gte=range_start, time__lte=range_end
    ).only("time", "name", "dosage", "dosage_unit"):
        detail = m.name
        if m.dosage:
            detail = f"{detail} · {m.dosage:g} {m.get_dosage_unit_display()}"
        _marker(m.time, "med", _("Medication"), detail)

    for t in models.TummyTime.objects.filter(
        child=child, start__gte=range_start, start__lte=range_end
    ).only("start", "duration"):
        _marker(
            t.start,
            "tummy",
            _("Tummy time"),
            _fmt_dur(t.duration) if t.duration else "",
        )

    # Hide an empty timeline — except on the live day, where the empty 0–24h row
    # is the point (it fills in as the day goes), so "Today" always shows it.
    if not window["is_live"] and not any(
        row["sleeps"] or row["markers"] for row in rows
    ):
        return None

    rowh, dense = _timeline_density(rendered)
    return {
        "rows": rows,
        "axis": [
            {"label": f"{h:02d}", "left": round(h / 24 * 100, 3)}
            for h in range(0, 25, 3)
        ],
        "single": rendered == 1,
        "rendered": rendered,
        "total_days": total_days,
        "hidden": hidden,
        "rowh": rowh,
        "dense": dense,
    }


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
        "timeline": _timeline_section(child, window),
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
