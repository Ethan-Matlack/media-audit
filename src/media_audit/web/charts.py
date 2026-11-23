"""The Files page's Chart view: the filtered files, grouped by one property, measured.

    Group by   release group, resolution, DV profile, audio language, …
    Measure    files, total size, share, median/average GB/h or bitrate, running time
    Split by   an optional second property: stacked bars for measures that add up
               (files, size, share, running time); sub-rows for medians and averages

The same filters as the file list apply, so a chart answers questions like "which
release groups have the highest bitrates" or "what share of my 4K movies is DV P7".
Every bar links back to the matching files.

Aggregation happens in Python over one row per file (medians aren't available in
SQLite, and a file can belong to several groups when grouping by its audio or subtitle
tracks). That's a few tens of milliseconds for a ~20k-file library.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from media_audit.web import queries as q

NONE = q.NONE


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    expr: str  # SQL for the value (NULL = unknown); track dimensions use alias t
    track_table: str | None = None
    facet: str | None = None  # the Files filter that selects one value (for links)
    none_label: str = "(unknown)"
    natural: dict[str, int] | None = None  # fixed order instead of by size

    @property
    def is_track(self) -> bool:
        return self.track_table is not None


_RES = {"4320p": 0, "2160p": 1, "1080p": 2, "720p": 3, "SD": 4}

DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("library", "Library", "l.name", facet="library"),
    Dimension("resolution", "Resolution", "v.resolution_class", facet="resolution",
              natural=_RES),
    Dimension("dr", "Dynamic range", "v.dynamic_range", facet="dr"),
    Dimension("dv", "Dolby Vision profile", "'P' || v.dv_profile", facet="dv",
              none_label="(no DV)"),
    Dimension("codec", "Video codec", "v.codec", facet="codec"),
    Dimension("depth", "Bit depth", "v.bit_depth || '-bit'", facet="depth"),
    Dimension("audio", "Audio format", "coalesce(t.commercial_name, t.codec)",
              "audio_tracks", facet="audio", none_label="(no audio)"),
    Dimension("alang", "Audio language", f"coalesce(t.language, '{q.UNKNOWN_LANG}')",
              "audio_tracks", facet="alang", none_label="(no audio)"),
    Dimension("slang", "Subtitle language", f"coalesce(t.language, '{q.UNKNOWN_LANG}')",
              "subtitle_tracks", facet="slang", none_label="(no subtitles)"),
    Dimension("container", "Container", "v.container", facet="container"),
    Dimension("source", "Source", "r.source", facet="source", none_label="(none)"),
    Dimension("group", "Release group", "r.release_group", facet="group",
              none_label="(none)"),
    Dimension("remux", "Remux", "CASE WHEN r.is_remux = 1 THEN 'Remux' ELSE 'Not remux' END"),
    Dimension("edition", "Edition", "r.edition", facet="edition", none_label="(none)"),
    Dimension("decade", "Decade", "(ti.year / 10 * 10) || 's'", natural={}),
    Dimension("year", "Year", "CAST(ti.year AS TEXT)", facet="year", natural={}),
)  # fmt: skip
DIM_KEYS = {d.key: d for d in DIMENSIONS}


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    additive: bool  # sums of parts make the whole: bars can be stacked
    unit: str  # files | bytes | gbh | bitrate | hours | percent


MEASURES: tuple[Measure, ...] = (
    Measure("files", "Files", True, "files"),
    Measure("size", "Total size", True, "bytes"),
    Measure("share", "Share", True, "percent"),
    Measure("gbh_median", "Median GB/h", False, "gbh"),
    Measure("gbh_avg", "Average GB/h", False, "gbh"),
    Measure("bitrate_median", "Median bitrate", False, "bitrate"),
    Measure("bitrate_avg", "Average bitrate", False, "bitrate"),
    Measure("duration", "Running time", True, "hours"),
)
MEASURE_KEYS = {m.key: m for m in MEASURES}

# One categorical colour per split value (styled in app.css as .series-0 … .series-7);
# anything past the 7 largest goes into "Other".
SPLIT_SLOTS = 7
DEFAULT_TOP = 25


@dataclass
class Item:
    """One file (or, counting each title once, one title) in one group."""

    size: int = 0
    duration: float = 0.0
    gbh: list[float] = field(default_factory=list)
    bitrate: list[float] = field(default_factory=list)


def _measure(m: Measure, items: list[Item], denominator: int) -> float | None:
    if m.key == "files":
        return float(len(items))
    if m.key == "size":
        return float(sum(i.size for i in items))
    if m.key == "duration":
        return sum(i.duration for i in items) / 3600
    if m.key == "share":
        return 100.0 * len(items) / denominator if denominator else 0.0
    values = [v for i in items for v in (i.gbh if m.key.startswith("gbh") else i.bitrate)]
    if not values:
        return None
    return float(
        statistics.median(values) if m.key.endswith("median") else statistics.fmean(values)
    )


def params(qs: Mapping[str, str]) -> dict[str, Any]:
    """The chart's settings from the URL, with defaults."""
    dim = DIM_KEYS.get(qs.get("dim", ""), DIM_KEYS["resolution"])
    measure = MEASURE_KEYS.get(qs.get("measure", ""), MEASURE_KEYS["files"])
    split = DIM_KEYS.get(qs.get("split", ""))
    if split is not None and split.key == dim.key:
        split = None
    try:
        minimum = max(1, int(qs.get("min", "")))
    except ValueError:
        minimum = 1 if measure.additive else 5
    per_title = qs.get("per") == "title"
    order = qs.get("csort") if qs.get("csort") in {"value", "name"} else (
        "name" if dim.natural is not None else "value")  # fmt: skip
    top = None if qs.get("top") == "all" else DEFAULT_TOP
    return {"dim": dim, "measure": measure, "split": split, "min": minimum,
            "per_title": per_title, "order": order, "top": top}  # fmt: skip


def _track_values(conn: sqlite3.Connection, dim: Dimension, where: str,
                  params_: list[Any]) -> dict[int, list[str]]:  # fmt: skip
    out: dict[int, list[str]] = defaultdict(list)
    sql = (f"SELECT DISTINCT f.id, {dim.expr} {q._FILE_FROM} "
           f"JOIN {dim.track_table} t ON t.file_id = f.id WHERE {where}")  # fmt: skip
    for fid, value in conn.execute(sql, params_):
        out[fid].append(NONE if value is None else str(value))
    return out


def chart_data(conn: sqlite3.Connection, qs: Mapping[str, str]) -> dict[str, Any]:
    p = params(qs)
    dim, measure, split = p["dim"], p["measure"], p["split"]
    where, params_ = q._file_filters(qs)
    exprs = [d.expr if not d.is_track else "NULL" for d in (dim, split) if d is not None]
    cols = ", ".join(f"{e} AS v{i}" for i, e in enumerate(exprs))
    rows = conn.execute(
        f"SELECT f.id, f.title_id, f.size_bytes, v.duration_s, v.gb_per_hour, "
        f"v.overall_bitrate, {cols} {q._FILE_FROM} WHERE {where}", params_
    ).fetchall()  # fmt: skip
    tracks = {d.key: _track_values(conn, d, where, params_)
              for d in (dim, split) if d is not None and d.is_track}  # fmt: skip

    def values_of(d: Dimension, row: sqlite3.Row, i: int) -> list[str]:
        if d.is_track:
            return tracks[d.key].get(row["id"]) or [NONE]
        v = row[f"v{i}"]
        return [NONE if v is None else str(v)]

    def add(item: Item, row: sqlite3.Row) -> None:
        item.size += row["size_bytes"] or 0
        item.duration += row["duration_s"] or 0
        if (row["gb_per_hour"] or 0) > 0:
            item.gbh.append(row["gb_per_hour"])
        if (row["overall_bitrate"] or 0) > 0:
            item.bitrate.append(row["overall_bitrate"])

    # A unit is a file, or a title when each title counts once. Each file is added once
    # to each of its groups, and once to each split part within a group.
    by_group: dict[str, dict[Any, Item]] = defaultdict(dict)
    cells: dict[tuple[str, str], dict[Any, Item]] = defaultdict(dict)
    units_total: set[Any] = set()
    for row in rows:
        unit = (("t", row["title_id"]) if p["per_title"] and row["title_id"] is not None
                else ("f", row["id"]))  # fmt: skip
        units_total.add(unit)
        for g in values_of(dim, row, 0):
            add(by_group[g].setdefault(unit, Item()), row)
            if split is not None:
                for s in values_of(split, row, 1):
                    add(cells[(g, s)].setdefault(unit, Item()), row)
    total_units = len(units_total)

    groups: list[dict[str, Any]] = []
    hidden_small = 0
    for g, units in by_group.items():
        items = list(units.values())
        n = len(items)
        value = _measure(measure, items, total_units)
        if n < p["min"] or value is None:
            hidden_small += 1
            continue
        groups.append({"value": g, "label": _label(dim, g), "n": n, "amount": value})

    groups.sort(key=_sorter(dim, p["order"]))
    shown, rest = (groups, []) if p["top"] is None else (groups[: p["top"]], groups[p["top"] :])

    # Split: the largest split values get a colour each; the rest are "Other".
    legend: list[dict[str, Any]] = []
    slot_of: dict[str, int] = {}
    if split is not None:
        weight: dict[str, int] = defaultdict(int)
        for (_g, s), units in cells.items():
            weight[s] += len(units)
        ordered = sorted(weight, key=lambda s: (s == NONE, -weight[s], _label(split, s)))
        if split.natural is not None:
            ordered = sorted(weight, key=_natural_key(split))
        for i, s in enumerate(ordered):
            slot_of[s] = i if i < SPLIT_SLOTS else SPLIT_SLOTS
        legend = [{"value": s, "label": _label(split, s), "slot": slot_of[s]}
                  for s in ordered if slot_of[s] < SPLIT_SLOTS]  # fmt: skip
        if any(slot_of[s] == SPLIT_SLOTS for s in ordered):
            legend.append({"value": None, "label": "Other", "slot": SPLIT_SLOTS})

    for grp in shown:
        grp["link"] = link(qs, dim, grp["value"])
        if split is None:
            continue
        parts: dict[int, dict[str, Any]] = {}
        for s, slot in slot_of.items():
            cell = cells.get((grp["value"], s))
            if not cell:
                continue
            items = list(cell.values())
            if measure.additive:
                denominator = grp["n"] if measure.key == "share" else total_units
                amount = _measure(measure, items, denominator) or 0.0
                part = parts.setdefault(slot, {"slot": slot, "amount": 0.0, "n": 0, "values": []})
                part["amount"] += amount
                part["n"] += len(items)
                part["values"].append(s)
            elif len(items) >= p["min"]:
                value = _measure(measure, items, total_units)
                if value is not None:
                    parts[len(parts) + 100 * slot] = {
                        "slot": slot,
                        "amount": value,
                        "n": len(items),
                        "values": [s],
                    }
        segs = sorted(parts.values(), key=lambda x: (x["slot"], -x["amount"]))
        for seg in segs:
            seg["label"] = (_label(split, seg["values"][0]) if len(seg["values"]) == 1
                            else f"Other ({len(seg['values'])})")  # fmt: skip
            seg["link"] = (link(qs, dim, grp["value"], split, seg["values"][0])
                           if len(seg["values"]) == 1 else None)  # fmt: skip
        if measure.key == "share":  # a 100% breakdown of each bar
            grp["amount"] = 100.0
        grp["segments"] = segs

    other = None
    if rest and measure.additive and measure.key != "share":
        other = {"label": f"Other ({len(rest)} more)", "amount": sum(g["amount"] for g in rest),
                 "n": sum(g["n"] for g in rest)}  # fmt: skip
    top_value = max([g["amount"] for g in shown] + ([other["amount"]] if other else []) + [0])
    if not measure.additive and split is not None:
        top_value = max([top_value] + [s["amount"] for g in shown for s in g.get("segments", [])])
    return {
        "dim": dim, "measure": measure, "split": split, "min": p["min"],
        "per_title": p["per_title"], "order": p["order"], "groups": shown, "other": other,
        "more": len(rest) if not other else 0, "hidden_small": hidden_small,
        "max": top_value or 1, "legend": legend, "units": total_units,
        "unit_word": "title" if p["per_title"] else "file",
    }  # fmt: skip


def _label(d: Dimension, value: str) -> str:
    if value == NONE:
        return d.none_label
    if d.key in {"alang", "slang"} and value == q.UNKNOWN_LANG:
        return "(unknown)"
    return value


def _natural_key(d: Dimension) -> Callable[[str], tuple[Any, ...]]:
    order = d.natural or {}

    def key(value: str) -> tuple[Any, ...]:
        if value == NONE:
            return (2, 0, "")
        if value in order:
            return (0, order[value], "")
        if d.key in {"year", "decade"}:  # newest first
            return (1, -int("".join(c for c in value if c.isdigit()) or 0), "")
        return (1, 0, value.lower())

    return key


def _sorter(d: Dimension, order: str) -> Callable[[dict[str, Any]], tuple[Any, ...]]:
    if order == "name":
        natural = _natural_key(d)
        return lambda g: natural(g["value"])
    return lambda g: (g["value"] == NONE, -g["amount"], g["label"].lower())


def link(qs: Mapping[str, str], dim: Dimension, value: str,
         split: Dimension | None = None, split_value: str | None = None) -> str | None:  # fmt: skip
    """The Files list for one bar (or one part of it): the current filters plus its value."""
    from media_audit.web.app import query_string

    changes: dict[str, Any] = {"by": None, "page": None, "dim": None, "measure": None,
                               "split": None, "min": None, "per": None, "csort": None,
                               "top": None}  # fmt: skip
    for d, v in ((dim, value), (split, split_value)):
        if d is None or v is None:
            continue
        target = _filter_for(d, v)
        if target is None:
            return None
        changes.update(target)
    return "/files" + query_string(qs, **changes)


def _filter_for(d: Dimension, value: str) -> dict[str, Any] | None:
    if d.key == "remux":
        return {"remux": "1"} if value == "Remux" else None
    if d.key == "decade":
        if value == NONE:
            return None
        start = int(value.rstrip("s"))
        return {"year": [str(y) for y in range(start, start + 10)], "year_not": None}
    if d.facet is None:
        return None
    return {d.facet: [value], f"{d.facet}_not": None}


def csv_rows(conn: sqlite3.Connection, qs: Mapping[str, str]) -> list[list[Any]]:
    data = chart_data(conn, qs)
    dim, measure, split = data["dim"], data["measure"], data["split"]
    header = [dim.label, *([split.label] if split else []), measure.label,
              data["unit_word"] + "s"]  # fmt: skip
    out: list[list[Any]] = [header]
    for g in data["groups"]:
        if split is None:
            out.append([g["label"], _plain(measure, g["amount"]), g["n"]])
            continue
        for s in g.get("segments", []):
            out.append([g["label"], s["label"], _plain(measure, s["amount"]), s["n"]])
    if data["other"]:
        out.append([data["other"]["label"], *([""] if split else []),
                    _plain(measure, data["other"]["amount"]), data["other"]["n"]])  # fmt: skip
    return out


def _plain(m: Measure, value: float) -> str:
    if m.unit in {"files"}:
        return str(round(value))
    if m.unit == "bytes":
        return str(int(value))
    return f"{value:.3f}"
