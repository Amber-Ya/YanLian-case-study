#!/usr/bin/env python3
"""Build and solve the weighted-stability refinery scheduling Baseline.

The model uses the static snapshot in ``case_data.py`` and does not read Excel
at solve time. Its daily target for each unit is clipped from rated capacity,
all effective tank pools must recover their initial inventory by the end of the
horizon, and the weighted objective penalizes both inventory and unit-load
variation. The untouched historical implementation remains under ``基础模型/``.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import case_data as static_case
import config as cfg


DEFAULT_TARGET_FACTOR = 1.0
DEFAULT_INVENTORY_CHANGE_WEIGHT = cfg.OBJECTIVE_WEIGHTS["inventory_change"]
DEFAULT_LOAD_VARIATION_WEIGHT = cfg.OBJECTIVE_WEIGHTS["load_variation"]


@dataclass
class UnitCapacity:
    unit: str
    initial: float
    rated: float
    min_load: float
    max_load: float
    source: str


@dataclass
class TankPool:
    material: str
    tanks: list[str] = field(default_factory=list)
    initial: float = 0.0
    min_inventory: float = 0.0
    max_inventory: float = 0.0
    density: float | None = None


@dataclass
class CaseData:
    unit_capacities: dict[str, UnitCapacity]
    tank_pools: dict[str, TankPool]
    cdu_side_map: dict[str, str]
    cdu_yields: dict[tuple[str, str], float]
    cdu_properties: dict[tuple[str, str, str], float]
    secondary_yields: dict[str, dict[str, float]]
    target_loads: dict[str, float]
    unit_feeds: dict[str, list[str]]
    feed_ratios: dict[str, dict[str, float]]
    warnings: list[str] = field(default_factory=list)


def build_static_case_data(yco_share: float = cfg.DEFAULT_YCO_SHARE) -> CaseData:
    capacities = {
        unit: UnitCapacity(**values)
        for unit, values in static_case.UNIT_CAPACITIES.items()
    }
    tank_pools = {
        material: TankPool(**values)
        for material, values in static_case.TANK_POOLS.items()
    }
    apply_crude_split(tank_pools, yco_share)
    cdu_yields = {
        (row["crude"], row["side_id"]): cdu_yield_fraction(row)
        for row in static_case.CDU_YIELDS
    }
    cdu_properties = {
        (row["crude"], row["side_id"], row["property"]): row["value"]
        for row in static_case.CDU_PROPERTIES
    }
    return CaseData(
        unit_capacities=capacities,
        tank_pools=tank_pools,
        cdu_side_map=dict(static_case.CDU_SIDE_TO_MATERIAL),
        cdu_yields=cdu_yields,
        cdu_properties=cdu_properties,
        secondary_yields={unit: dict(yields) for unit, yields in static_case.SECONDARY_YIELDS.items()},
        target_loads=dict(static_case.TARGET_LOADS),
        unit_feeds={unit: list(feeds) for unit, feeds in static_case.UNIT_FEEDS.items()},
        feed_ratios={unit: dict(ratios) for unit, ratios in static_case.FEED_RATIOS.items()},
        warnings=list(static_case.WARNINGS),
    )


def cdu_yield_fraction(row: dict[str, Any]) -> float:
    if "yield_fraction" in row:
        return float(row["yield_fraction"])
    if "yield" in row:
        return float(row["yield"])
    return float(row["yield_percent"]) / 100.0


def apply_crude_split(tank_pools: dict[str, TankPool], yco_share: float) -> None:
    if "YCO" not in tank_pools or "RCO" not in tank_pools:
        return
    yco = tank_pools["YCO"]
    rco = tank_pools["RCO"]
    total_initial = yco.initial + rco.initial
    total_min = yco.min_inventory + rco.min_inventory
    total_max = yco.max_inventory + rco.max_inventory
    all_tanks = sorted(set(yco.tanks + rco.tanks))
    avg_density = next((density for density in (yco.density, rco.density) if density is not None), None)
    for crude, share in [("YCO", yco_share), ("RCO", 1.0 - yco_share)]:
        pool = tank_pools[crude]
        pool.tanks = [tank for tank in all_tanks if tank.startswith(f"{crude}:")] or [f"{crude}:static_crude_pool"]
        pool.initial = total_initial * share
        pool.min_inventory = total_min * share
        pool.max_inventory = total_max * share
        pool.density = avg_density


def default_feed_options(case: CaseData) -> dict[str, list[str]]:
    units = set(case.unit_capacities) | set(case.secondary_yields) | set(case.target_loads)
    return {unit: feeds for unit, feeds in case.unit_feeds.items() if unit in units}


def build_rated_target_loads(
    case: CaseData,
    alpha: float = DEFAULT_TARGET_FACTOR,
) -> dict[str, float]:
    """Return ``clip(alpha * rated, minimum, maximum)`` for each modeled unit."""

    if alpha <= 0.0:
        raise ValueError("alpha must be positive")
    targets: dict[str, float] = {}
    for unit in sorted(default_feed_options(case)):
        capacity = case.unit_capacities.get(unit)
        if capacity is None:
            targets[unit] = max(float(case.target_loads.get(unit, 0.0)), 0.0)
            continue
        raw_target = alpha * capacity.rated
        targets[unit] = min(
            max(raw_target, capacity.min_load),
            capacity.max_load,
        )
    return targets


def effective_tank_materials(case: CaseData) -> list[str]:
    """Return material pools with a positive working-inventory range."""

    return [
        material
        for material, pool in sorted(case.tank_pools.items())
        if pool.max_inventory > pool.min_inventory + 1e-9
    ]


def build_and_solve(
    case: CaseData,
    horizon_days: int,
    output_dir: Path,
    time_limit: float | None,
    mip_gap: float | None,
    write_lp: bool,
    alpha: float = DEFAULT_TARGET_FACTOR,
    inventory_change_weight: float = DEFAULT_INVENTORY_CHANGE_WEIGHT,
    load_variation_weight: float = DEFAULT_LOAD_VARIATION_WEIGHT,
    enforce_terminal_inventory: bool = True,
) -> dict[str, Any]:
    """Solve the canonical weighted-stability Baseline.

    The six original positional arguments remain unchanged. New controls are
    optional so existing callers continue to work.
    """

    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if inventory_change_weight < 0.0:
        raise ValueError("inventory_change_weight must be nonnegative")
    if load_variation_weight < 0.0:
        raise ValueError("load_variation_weight must be nonnegative")
    original_case_targets = dict(case.target_loads)

    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError as exc:
        raise RuntimeError(
            "gurobipy is not installed in this Python environment. "
            "Install Gurobi/gurobipy, or run with --inspect-data to verify parsing only."
        ) from exc

    days = range(1, horizon_days + 1)
    feed_options = default_feed_options(case)
    units = sorted(feed_options)
    target_by_unit = build_rated_target_loads(case, alpha)
    initial_loads = {
        unit: float(
            case.unit_capacities[unit].initial
            if unit in case.unit_capacities
            else target_by_unit[unit]
        )
        for unit in units
    }
    materials = set(case.tank_pools)
    materials.update(cfg.CRUDES)
    for feeds in feed_options.values():
        materials.update(feeds)
    for yields in case.secondary_yields.values():
        materials.update(yields)
    materials.update(case.cdu_side_map.values())
    materials.discard("损失")
    materials = sorted(materials)
    terminal_materials = (
        [material for material in effective_tank_materials(case) if material in materials]
        if enforce_terminal_inventory
        else []
    )
    objective_weights = {
        "load_deviation": float(cfg.OBJECTIVE_WEIGHTS["load_deviation"]),
        "unit_switch": float(cfg.OBJECTIVE_WEIGHTS["unit_switch"]),
        "inventory_change": float(inventory_change_weight),
        "external_source": float(cfg.OBJECTIVE_WEIGHTS["external_source"]),
        "shipment": float(cfg.OBJECTIVE_WEIGHTS["shipment"]),
        "load_variation": float(load_variation_weight),
    }

    model = gp.Model("yanlian_weighted_stability_baseline")
    if time_limit is not None:
        model.Params.TimeLimit = time_limit
    if mip_gap is not None:
        model.Params.MIPGap = mip_gap

    load = model.addVars(units, days, lb=0.0, name="unit_load")
    on = model.addVars(units, days, vtype=GRB.BINARY, name="unit_on")
    switch = model.addVars(units, days, vtype=GRB.BINARY, name="unit_switch")
    feed_keys = [(u, m, d) for u in units for m in feed_options[u] for d in days]
    feed = model.addVars(feed_keys, lb=0.0, name="feed")
    prod_keys = [(u, m, d) for u in units for m in materials for d in days]
    prod = model.addVars(prod_keys, lb=0.0, name="production")
    inv = model.addVars(materials, days, lb=0.0, name="inventory")
    shipment = model.addVars(materials, days, lb=0.0, name="shipment")
    source_material_list = sorted(cfg.EXTERNAL_SOURCE_MATERIALS & set(materials))
    external_source = model.addVars(source_material_list, days, lb=0.0, name="external_source")
    load_short = model.addVars(units, days, lb=0.0, name="load_shortfall")
    load_over = model.addVars(units, days, lb=0.0, name="load_overage")
    load_change_up = model.addVars(units, days, lb=0.0, name="load_change_up")
    load_change_down = model.addVars(units, days, lb=0.0, name="load_change_down")
    inv_up = model.addVars(materials, days, lb=0.0, name="inventory_increase_abs")
    inv_down = model.addVars(materials, days, lb=0.0, name="inventory_decrease_abs")

    for u in units:
        cap = case.unit_capacities.get(u)
        target = target_by_unit[u]
        max_load = cap.max_load if cap else max(target * 1.2, 1.0)
        min_load = cap.min_load if cap and target > 1e-6 else 0.0
        for d in days:
            model.addConstr(load[u, d] == gp.quicksum(feed[u, m, d] for m in feed_options[u]), name=f"load_def[{u},{d}]")
            model.addConstr(load[u, d] <= max_load * on[u, d], name=f"unit_max[{u},{d}]")
            if min_load > 0:
                model.addConstr(load[u, d] >= min_load * on[u, d], name=f"unit_min[{u},{d}]")
            model.addConstr(load[u, d] + load_short[u, d] - load_over[u, d] == target, name=f"target_load[{u},{d}]")
            previous_load = initial_loads[u] if d == 1 else load[u, d - 1]
            model.addConstr(
                load[u, d] - previous_load == load_change_up[u, d] - load_change_down[u, d],
                name=f"load_change[{u},{d}]",
            )
            initial_on = 1 if initial_loads[u] > 1e-6 else 0
            previous_on = initial_on if d == 1 else on[u, d - 1]
            model.addConstr(switch[u, d] >= on[u, d] - previous_on, name=f"switch_up[{u},{d}]")
            model.addConstr(switch[u, d] >= previous_on - on[u, d], name=f"switch_down[{u},{d}]")

    for u, ratios in case.feed_ratios.items():
        if u not in units:
            continue
        relevant = {m: r for m, r in ratios.items() if m in feed_options[u]}
        if len(relevant) <= 1:
            continue
        total_ratio = sum(relevant.values())
        for m, ratio in relevant.items():
            normalized = ratio / total_ratio
            for d in days:
                model.addConstr(feed[u, m, d] == normalized * load[u, d], name=f"feed_ratio[{u},{m},{d}]")

    for u in units:
        if u in cfg.CDU_UNITS:
            for material in materials:
                for d in days:
                    terms = []
                    for crude in cfg.CRUDES:
                        if crude not in feed_options[u]:
                            continue
                        for side_id, material_from_side in case.cdu_side_map.items():
                            if material_from_side != material:
                                continue
                            yield_value = case.cdu_yields.get((crude, side_id), 0.0)
                            if yield_value:
                                terms.append(feed[u, crude, d] * yield_value)
                    model.addConstr(prod[u, material, d] == gp.quicksum(terms), name=f"cdu_prod[{u},{material},{d}]")
        else:
            yields = case.secondary_yields.get(u, {})
            for material in materials:
                coeff = yields.get(material, 0.0)
                for d in days:
                    model.addConstr(prod[u, material, d] == coeff * load[u, d], name=f"fixed_yield[{u},{material},{d}]")

    for material in materials:
        pool = case.tank_pools.get(material)
        initial = pool.initial if pool else 0.0
        min_inv = pool.min_inventory if pool else 0.0
        has_working_inventory = bool(
            pool and pool.max_inventory > pool.min_inventory + 1e-9
        )
        max_inv = pool.max_inventory if has_working_inventory else min_inv
        if initial < min_inv - 1e-6 or initial > max_inv + 1e-6:
            raise ValueError(
                f"initial inventory outside bounds for {material}: "
                f"{initial} not in [{min_inv}, {max_inv}]"
            )
        for d in days:
            prev_inv = initial if d == 1 else inv[material, d - 1]
            production = gp.quicksum(prod[u, material, d] for u in units)
            consumption = gp.quicksum(feed[u, material, d] for u in units if material in feed_options[u])
            source = external_source[material, d] if (material, d) in external_source else 0.0
            model.addConstr(
                inv[material, d] == prev_inv + production + source - consumption - shipment[material, d],
                name=f"tank_balance[{material},{d}]",
            )
            model.addConstr(inv[material, d] >= min_inv, name=f"tank_min[{material},{d}]")
            model.addConstr(inv[material, d] <= max_inv, name=f"tank_max[{material},{d}]")

            prev_for_change = initial if d == 1 else inv[material, d - 1]
            model.addConstr(inv[material, d] - prev_for_change == inv_up[material, d] - inv_down[material, d], name=f"inv_change[{material},{d}]")

    for material in terminal_materials:
        model.addConstr(
            inv[material, horizon_days] >= case.tank_pools[material].initial,
            name=f"terminal_inventory_min[{material}]",
        )

    objective = (
        objective_weights["load_deviation"] * gp.quicksum(load_short[u, d] + load_over[u, d] for u in units for d in days)
        + objective_weights["unit_switch"] * gp.quicksum(switch[u, d] for u in units for d in days)
        + objective_weights["inventory_change"] * gp.quicksum(inv_up[m, d] + inv_down[m, d] for m in materials for d in days)
        + objective_weights["external_source"] * gp.quicksum(external_source[m, d] for m in source_material_list for d in days)
        + objective_weights["shipment"] * gp.quicksum(shipment[m, d] for m in materials for d in days)
        + objective_weights["load_variation"] * gp.quicksum(load_change_up[u, d] + load_change_down[u, d] for u in units for d in days)
    )
    model.setObjective(objective, GRB.MINIMIZE)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_target_loads_csv(output_dir, case, alpha, target_by_unit)
    if write_lp:
        model.write(str(output_dir / "refinery_scheduling.rlp"))

    model.optimize()

    solution_metrics = None
    if model.SolCount:
        solution_metrics = compute_solution_metrics(
            case=case,
            days=days,
            units=units,
            materials=materials,
            source_materials=source_material_list,
            switch=switch,
            shipment=shipment,
            external_source=external_source,
            load_short=load_short,
            load_over=load_over,
            load=load,
            target_by_unit=target_by_unit,
            initial_loads=initial_loads,
            inv=inv,
            inv_up=inv_up,
            inv_down=inv_down,
            objective_weights=objective_weights,
            terminal_materials=terminal_materials,
        )

    result = {
        "status": int(model.Status),
        "status_name": status_name(model.Status),
        "objective": model.ObjVal if model.SolCount else None,
        "optimization_mode": "weighted_stability_baseline",
        "horizon_days": horizon_days,
        "alpha": alpha,
        "target_load_rule": "clip(alpha * rated_load, min_load, max_load)",
        "target_loads_t_per_d": {
            unit: round(target, 6)
            for unit, target in target_by_unit.items()
        },
        "original_case_targets_unchanged": case.target_loads == original_case_targets,
        "objective_weights": objective_weights,
        "terminal_inventory_rule": (
            "ending_inventory >= initial_inventory for every effective tank pool"
            if enforce_terminal_inventory
            else "disabled"
        ),
        "terminal_inventory_materials": terminal_materials,
        "units": units,
        "materials": materials,
        "solution_count": int(model.SolCount),
        "runtime_seconds": round(float(model.Runtime), 6),
        "node_count": round(float(model.NodeCount), 6),
        "solution_metrics": solution_metrics,
        "warnings": case.warnings,
    }
    if model.SolCount:
        write_solution_csv(
            output_dir=output_dir,
            case=case,
            days=days,
            units=units,
            materials=materials,
            source_materials=source_material_list,
            load=load,
            on=on,
            inv=inv,
            shipment=shipment,
            external_source=external_source,
            target_by_unit=target_by_unit,
            initial_loads=initial_loads,
            terminal_materials=terminal_materials,
        )
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def compute_solution_metrics(
    case: CaseData,
    days: range,
    units: list[str],
    materials: list[str],
    source_materials: list[str],
    switch: Any,
    shipment: Any,
    external_source: Any,
    load_short: Any,
    load_over: Any,
    load: Any,
    target_by_unit: dict[str, float],
    initial_loads: dict[str, float],
    inv: Any,
    inv_up: Any,
    inv_down: Any,
    objective_weights: dict[str, float],
    terminal_materials: list[str],
) -> dict[str, Any]:
    load_shortfall_by_unit = {
        u: sum(load_short[u, d].X for d in days)
        for u in units
    }
    load_overage_by_unit = {
        u: sum(load_over[u, d].X for d in days)
        for u in units
    }
    external_by_material = {
        m: sum(external_source[m, d].X for d in days)
        for m in source_materials
    }
    shipment_by_material = {
        m: sum(shipment[m, d].X for d in days)
        for m in materials
    }
    inventory_bound_hits = []
    eps = 1e-5
    for m in materials:
        pool = case.tank_pools.get(m)
        if not pool or pool.max_inventory <= pool.min_inventory + eps:
            continue
        for d in days:
            value = inv[m, d].X
            if value <= pool.min_inventory + eps:
                inventory_bound_hits.append({
                    "day": d,
                    "material": m,
                    "bound": "min",
                    "inventory_t": value,
                    "bound_t": pool.min_inventory,
                })
            if value >= pool.max_inventory - eps:
                inventory_bound_hits.append({
                    "day": d,
                    "material": m,
                    "bound": "max",
                    "inventory_t": value,
                    "bound_t": pool.max_inventory,
                })

    load_deviation_total = sum(load_shortfall_by_unit.values()) + sum(load_overage_by_unit.values())
    inventory_change_total = 0.0
    for material in materials:
        pool = case.tank_pools.get(material)
        previous_inventory = pool.initial if pool else 0.0
        for day in days:
            current_inventory = float(inv[material, day].X)
            inventory_change_total += abs(current_inventory - previous_inventory)
            previous_inventory = current_inventory
    external_total = sum(external_by_material.values())
    shipment_total = sum(shipment_by_material.values())
    switch_total = sum(switch[u, d].X for u in units for d in days)
    completion_by_unit: dict[str, float | None] = {}
    load_variation_by_unit: dict[str, float] = {}
    total_credited_load = 0.0
    total_target_load = 0.0
    equal_unit_day_completion = 0.0
    equal_unit_day_count = 0
    for unit in units:
        target = target_by_unit[unit]
        previous_load = initial_loads[unit]
        unit_credited = 0.0
        unit_target = 0.0
        unit_variation = 0.0
        for day in days:
            actual_load = float(load[unit, day].X)
            credited_load = min(actual_load, target) if target > 1e-9 else 0.0
            unit_credited += credited_load
            unit_target += target
            total_credited_load += credited_load
            total_target_load += target
            if target > 1e-9:
                equal_unit_day_completion += credited_load / target
                equal_unit_day_count += 1
            unit_variation += abs(actual_load - previous_load)
            previous_load = actual_load
        completion_by_unit[unit] = (
            unit_credited / unit_target if unit_target > 1e-9 else None
        )
        load_variation_by_unit[unit] = unit_variation
    load_variation_total = sum(load_variation_by_unit.values())
    terminal_inventory_rows = []
    for material in terminal_materials:
        initial = case.tank_pools[material].initial
        ending = float(inv[material, days.stop - 1].X)
        terminal_inventory_rows.append({
            "material": material,
            "initial_inventory_t": round(initial, 6),
            "ending_inventory_t": round(ending, 6),
            "terminal_minimum_t": round(initial, 6),
            "slack_t": round(ending - initial, 6),
            "satisfied": ending + eps >= initial,
        })
    objective_components = {
        "load_deviation": objective_weights["load_deviation"] * load_deviation_total,
        "unit_switch": objective_weights["unit_switch"] * switch_total,
        "inventory_change": objective_weights["inventory_change"] * inventory_change_total,
        "external_source": objective_weights["external_source"] * external_total,
        "shipment": objective_weights["shipment"] * shipment_total,
        "load_variation": objective_weights["load_variation"] * load_variation_total,
    }
    return {
        "load_deviation_total_t": round(load_deviation_total, 6),
        "load_shortfall_total_t": round(sum(load_shortfall_by_unit.values()), 6),
        "load_overage_total_t": round(sum(load_overage_by_unit.values()), 6),
        "unit_switch_count": round(switch_total, 6),
        "inventory_change_total_t": round(inventory_change_total, 6),
        "external_source_total_t": round(external_total, 6),
        "shipment_total_t": round(shipment_total, 6),
        "plan_completion_rate_equal_unit": (
            equal_unit_day_completion / equal_unit_day_count
            if equal_unit_day_count
            else None
        ),
        "plan_completion_rate_throughput": (
            total_credited_load / total_target_load
            if total_target_load > 1e-9
            else None
        ),
        "credited_load_total_t": round(total_credited_load, 6),
        "planned_load_total_t": round(total_target_load, 6),
        "plan_completion_by_unit": completion_by_unit,
        "load_variation_total_t": round(load_variation_total, 6),
        "load_variation_by_unit_t": {
            unit: round(value, 6)
            for unit, value in load_variation_by_unit.items()
        },
        "load_shortfall_by_unit_t": {u: round(v, 6) for u, v in load_shortfall_by_unit.items() if abs(v) > 1e-6},
        "load_overage_by_unit_t": {u: round(v, 6) for u, v in load_overage_by_unit.items() if abs(v) > 1e-6},
        "external_source_by_material_t": {m: round(v, 6) for m, v in external_by_material.items() if abs(v) > 1e-6},
        "shipment_by_material_t": {m: round(v, 6) for m, v in shipment_by_material.items() if abs(v) > 1e-6},
        "inventory_bound_hit_count": len(inventory_bound_hits),
        "inventory_bound_hits": inventory_bound_hits[:100],
        "terminal_inventory_constraint_count": len(terminal_inventory_rows),
        "terminal_inventory_all_satisfied": all(
            row["satisfied"] for row in terminal_inventory_rows
        ),
        "terminal_inventories": terminal_inventory_rows,
        "objective_components": {k: round(v, 6) for k, v in objective_components.items()},
        "objective_component_total": round(sum(objective_components.values()), 6),
    }


def status_name(status_code: int) -> str:
    names = {
        1: "LOADED",
        2: "OPTIMAL",
        3: "INFEASIBLE",
        4: "INF_OR_UNBD",
        5: "UNBOUNDED",
        9: "TIME_LIMIT",
    }
    return names.get(status_code, f"STATUS_{status_code}")


def write_solution_csv(
    output_dir: Path,
    case: CaseData,
    days: range,
    units: list[str],
    materials: list[str],
    source_materials: list[str],
    load: Any,
    on: Any,
    inv: Any,
    shipment: Any,
    external_source: Any,
    target_by_unit: dict[str, float],
    initial_loads: dict[str, float],
    terminal_materials: list[str],
) -> None:
    with (output_dir / "unit_loads.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "day",
            "unit",
            "load_t_per_d",
            "target_t_per_d",
            "credited_load_t_per_d",
            "completion_rate",
            "load_change_t_per_d",
            "on",
        ])
        for d in days:
            for u in units:
                actual_load = float(load[u, d].X)
                target = target_by_unit[u]
                previous_load = initial_loads[u] if d == 1 else float(load[u, d - 1].X)
                credited_load = min(actual_load, target) if target > 1e-9 else 0.0
                writer.writerow([
                    d,
                    u,
                    round(actual_load, 6),
                    round(target, 6),
                    round(credited_load, 6),
                    round(credited_load / target, 9) if target > 1e-9 else "",
                    round(abs(actual_load - previous_load), 6),
                    round(on[u, d].X),
                ])

    with (output_dir / "inventories.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["day", "material", "inventory_t", "shipment_t_per_d", "external_source_t_per_d"])
        source_keys = set(source_materials)
        for d in days:
            for m in materials:
                source_value = external_source[m, d].X if m in source_keys else 0.0
                writer.writerow([d, m, round(inv[m, d].X, 6), round(shipment[m, d].X, 6), round(source_value, 6)])

    with (output_dir / "terminal_inventories.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "material",
            "initial_inventory_t",
            "ending_inventory_t",
            "terminal_minimum_t",
            "slack_t",
            "satisfied",
        ])
        for material in terminal_materials:
            initial = case.tank_pools[material].initial
            ending = float(inv[material, days.stop - 1].X)
            writer.writerow([
                material,
                round(initial, 6),
                round(ending, 6),
                round(initial, 6),
                round(ending - initial, 6),
                ending + 1e-5 >= initial,
            ])


def write_target_loads_csv(
    output_dir: Path,
    case: CaseData,
    alpha: float,
    target_by_unit: dict[str, float],
) -> None:
    """Write the rated-target calculation independently of solver status."""

    with (output_dir / "target_loads.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "unit",
            "alpha",
            "initial_load_t_per_d",
            "rated_load_t_per_d",
            "minimum_load_t_per_d",
            "maximum_load_t_per_d",
            "raw_target_t_per_d",
            "target_t_per_d",
            "clipped_at",
        ])
        for unit, target in sorted(target_by_unit.items()):
            capacity = case.unit_capacities.get(unit)
            if capacity is None:
                writer.writerow([
                    unit,
                    alpha,
                    "",
                    "",
                    "",
                    "",
                    round(target, 6),
                    round(target, 6),
                    "fallback_case_target",
                ])
                continue
            raw_target = alpha * capacity.rated
            if target < raw_target - 1e-9:
                clipped_at = "max"
            elif target > raw_target + 1e-9:
                clipped_at = "min"
            else:
                clipped_at = "none"
            writer.writerow([
                unit,
                alpha,
                round(capacity.initial, 6),
                round(capacity.rated, 6),
                round(capacity.min_load, 6),
                round(capacity.max_load, 6),
                round(raw_target, 6),
                round(target, 6),
                clipped_at,
            ])


def inspect_data(case: CaseData) -> dict[str, Any]:
    return {
        "unit_count": len(case.unit_capacities),
        "tank_pool_count": len(case.tank_pools),
        "cdu_yield_count": len(case.cdu_yields),
        "secondary_side_yield_count": len(getattr(static_case, "SECONDARY_UNIT_SIDE_YIELDS", [])),
        "secondary_yield_units": sorted(case.secondary_yields),
        "target_load_units": sorted(case.target_loads),
        "unit_feed_count": len(case.unit_feeds),
        "feed_ratio_units": sorted(case.feed_ratios),
        "tank_pools": {
            material: {
                "tank_count": len(pool.tanks),
                "initial_t": round(pool.initial, 3),
                "min_t": round(pool.min_inventory, 3),
                "max_t": round(pool.max_inventory, 3),
            }
            for material, pool in sorted(case.tank_pools.items())
        },
        "warnings": case.warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solve the Yanlian weighted-stability Baseline.")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/gurobi_schedule"), help="Folder for LP and solution outputs.")
    parser.add_argument("--horizon-days", type=int, default=cfg.DEFAULT_HORIZON_DAYS, help="Number of daily time periods.")
    parser.add_argument("--yco-share", type=float, default=cfg.DEFAULT_YCO_SHARE, help="Share of aggregate crude inventory assigned to YCO; the rest is RCO.")
    parser.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_TARGET_FACTOR,
        help="Rated-load factor used by clip(alpha * rated, minimum, maximum).",
    )
    parser.add_argument(
        "--inventory-change-weight",
        type=float,
        default=DEFAULT_INVENTORY_CHANGE_WEIGHT,
        help="Weight of absolute inventory changes in the single objective.",
    )
    parser.add_argument(
        "--load-variation-weight",
        type=float,
        default=DEFAULT_LOAD_VARIATION_WEIGHT,
        help="Weight of absolute unit-load changes in the single objective.",
    )
    parser.add_argument(
        "--no-terminal-inventory",
        action="store_true",
        help="Disable the default ending-inventory-at-least-initial requirement.",
    )
    parser.add_argument("--inspect-data", action="store_true", help="Parse inputs and print a JSON summary without requiring gurobipy.")
    parser.add_argument("--write-lp", action="store_true", help="Write an LP file before optimizing.")
    parser.add_argument("--time-limit", type=float, default=None, help="Optional Gurobi time limit in seconds.")
    parser.add_argument("--mip-gap", type=float, default=None, help="Optional Gurobi MIP gap.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0.0 <= args.yco_share <= 1.0:
        raise ValueError("--yco-share must be between 0 and 1")
    if args.alpha <= 0.0:
        raise ValueError("--alpha must be positive")
    if args.inventory_change_weight < 0.0:
        raise ValueError("--inventory-change-weight must be nonnegative")
    if args.load_variation_weight < 0.0:
        raise ValueError("--load-variation-weight must be nonnegative")
    case = build_static_case_data(args.yco_share)
    if args.inspect_data:
        print(json.dumps(inspect_data(case), ensure_ascii=False, indent=2))
        return
    try:
        result = build_and_solve(
            case=case,
            horizon_days=args.horizon_days,
            output_dir=args.output_dir,
            time_limit=args.time_limit,
            mip_gap=args.mip_gap,
            write_lp=args.write_lp,
            alpha=args.alpha,
            inventory_change_weight=args.inventory_change_weight,
            load_variation_weight=args.load_variation_weight,
            enforce_terminal_inventory=not args.no_terminal_inventory,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
