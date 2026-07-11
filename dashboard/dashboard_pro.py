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

from core import models
from core.models import Feeding

# Period filter options exposed in the UI (the querystring ``?period=``).
PERIODS = ("day", "week", "month", "all")

# The averaging windows used for predictions, mirroring the ones already used by
# ``dashboard.templatetags.cards`` (past 3 days / past 2 weeks / all-time).
_PRED_WINDOWS = ("day", "week", "all")


# ---------------------------------------------------------------------------
# Period window
# ---------------------------------------------------------------------------
def _aware(naive):
    return timezone.make_aware(naive, timezone.get_current_timezone())


def get_period_window(period, date_str):
    """
    Resolve the ``period`` / ``date`` querystring into a concrete time window.

    :returns: a dict with ``period``, ``anchor`` (date), ``start`` / ``end``
        (aware datetimes; ``start`` is ``None`` for "all"), ``is_live`` (whether
        predictions should be shown), navigation helpers and a label.
    """
    now = timezone.localtime()
    today = now.date()

    try:
        anchor = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else today
    except (ValueError, TypeError):
        anchor = today

    if period == "week":
        start = _aware(datetime.combine(anchor - timedelta(days=6), dtime.min))
        end = _aware(datetime.combine(anchor, dtime.max))
        unit = timedelta(days=7)
    elif period == "month":
        start = _aware(datetime.combine(anchor - timedelta(days=29), dtime.min))
        end = _aware(datetime.combine(anchor, dtime.max))
        unit = timedelta(days=30)
    elif period == "all":
        period = "all"
        start = None
        end = now
        unit = None
    else:
        period = "day"
        start = _aware(datetime.combine(anchor, dtime.min))
        end = _aware(datetime.combine(anchor, dtime.max))
        unit = timedelta(days=1)

    # Predictions are only meaningful "live" — a single day that is today.
    is_live = period == "day" and anchor == today

    prev_date = next_date = None
    can_go_next = False
    if unit is not None:
        prev_date = (anchor - unit).strftime("%Y-%m-%d")
        if anchor < today:
            next_anchor = min(anchor + unit, today)
            next_date = next_anchor.strftime("%Y-%m-%d")
            can_go_next = True

    return {
        "period": period,
        "anchor": anchor,
        "start": start,
        "end": end,
        "start_date": start.date() if start else None,
        "end_date": end.date(),
        "is_live": is_live,
        "prev_date": prev_date,
        "next_date": next_date,
        "can_go_next": can_go_next,
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
def _pred_bounds(now):
    return {
        "day": now - timedelta(days=3),
        "week": now - timedelta(weeks=2),
        "all": None,
    }


def _average_intervals(points, now):
    """
    Average gap between consecutive ``points`` (ascending datetimes) for each
    prediction window.  ``points[i] - points[i - 1]`` counts toward a window when
    the *previous* point falls inside it.
    """
    bounds = _pred_bounds(now)
    totals = {w: timedelta(0) for w in _PRED_WINDOWS}
    counts = {w: 0 for w in _PRED_WINDOWS}
    for i in range(1, len(points)):
        prev, cur = points[i - 1], points[i]
        gap = cur - prev
        for w, bound in bounds.items():
            if bound is None or prev >= bound:
                totals[w] += gap
                counts[w] += 1
    return {w: (totals[w] / counts[w] if counts[w] else None) for w in _PRED_WINDOWS}


def _average_gaps(pairs, now):
    """
    Average "away" gap for (start, end) ``pairs`` ascending by start:
    ``pairs[i].start - pairs[i - 1].end`` (e.g. awake duration between sleeps).
    """
    bounds = _pred_bounds(now)
    totals = {w: timedelta(0) for w in _PRED_WINDOWS}
    counts = {w: 0 for w in _PRED_WINDOWS}
    for i in range(1, len(pairs)):
        prev_end = pairs[i - 1][1]
        cur_start = pairs[i][0]
        gap = cur_start - prev_end
        if gap.total_seconds() < 0:
            continue
        for w, bound in bounds.items():
            if bound is None or prev_end >= bound:
                totals[w] += gap
                counts[w] += 1
    return {w: (totals[w] / counts[w] if counts[w] else None) for w in _PRED_WINDOWS}


def _build_prediction(anchor, averages, now):
    """Combine the last-event ``anchor`` with the best available average."""
    used = next((w for w in _PRED_WINDOWS if averages.get(w)), None)
    if anchor is None or used is None:
        return None
    predicted = anchor + averages[used]
    eta = predicted - now
    return {
        "predicted": predicted,
        "anchor": anchor,
        "used": used,
        "averages": averages,
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
# Per-day trend series (fixed 7-day mini chart ending at the anchor date)
# ---------------------------------------------------------------------------
def _trend(values, end_date, days=7, seconds=False):
    """
    Build a ``days``-long series ending at ``end_date``.

    :param values: list of datetimes (counts) or (datetime, timedelta) tuples
        when ``seconds`` is True (sums the duration seconds per day).
    """
    raw = [0] * days
    for value in values:
        dt = value[0] if seconds else value
        idx = (end_date - timezone.localtime(dt).date()).days
        if 0 <= idx < days:
            raw[days - 1 - idx] += value[1].total_seconds() if seconds else 1
    top = max(raw) or 1
    series = []
    for i, amount in enumerate(raw):
        day = end_date - timedelta(days=(days - 1 - i))
        series.append(
            {
                "pct": round(100 * amount / top),
                "date": day,
                "today": day == end_date,
            }
        )
    return series


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

    starts = list(qs.values_list("start", flat=True))
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
        "trend": _trend(starts, window["end_date"]),
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
    times = list(qs.values_list("time", flat=True))
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
        "trend": _trend(times, window["end_date"]),
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
        "trend": _trend(pairs, window["end_date"], seconds=True),
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_context(child, period, date_str):
    """Assemble the full Dashboard Pro context for ``child``."""
    now = timezone.localtime()
    window = get_period_window(period, date_str)

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
        "timers": _timers_section(child),
        "not_registered": _not_registered(child),
        "now": now,
    }
