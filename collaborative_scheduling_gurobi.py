#!/usr/bin/env python3
"""Yanlian--Yanshihua collaborative refinery scheduling model.

This model keeps plant-level material balances, activates the 14 Yanshihua
units calibrated from the December 2025 production reports, and represents
inter-plant movements with explicit finite-capacity transfer arcs.  The
original ``refinery_scheduling_gurobi.py`` remains the Yanlian-only baseline.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import case_data as static_case
import config as cfg
import refinery_scheduling_gurobi as baseline


DEFAULT_OUTPUT_DIR = Path("outputs/collaborative_schedule")


def build_collaborative_case(yco_share: float = cfg.DEFAULT_YCO_SHARE) -> baseline.CaseData:
    """Return the baseline case with Yanshihua feeds and report calibration."""

    case = baseline.build_static_case_data(yco_share)
    case.unit_feeds.update({
        unit: list(feeds)
        for unit, feeds in static_case.YANSHIHUA_UNIT_FEEDS.items()
    })
    case.feed_ratios.update({
        unit: dict(ratios)
        for unit, ratios in static_case.YANSHIHUA_FEED_RATIOS.items()
    })
    for unit, overrides in static_case.REPORT_CALIBRATED_CAPACITY_OVERRIDES.items():
        capacity = case.unit_capacities[unit]
        for field_name, value in overrides.items():
            setattr(capacity, field_name, float(value))
    adjusted_initial_units = []
    for capacity in case.unit_capacities.values():
        clipped_initial = min(max(capacity.initial, capacity.min_load), capacity.max_load)
        if abs(clipped_initial - capacity.initial) > 1e-9:
            adjusted_initial_units.append(capacity.unit)
            capacity.initial = clipped_initial
    case.warnings.extend([
        "延石化储罐缺失当前液位时使用安全区间中位值作为初始库存。",
        "跨厂转运能力为首版工程假设，投用前需替换为管输/装卸确认值。",
        "延石化装置固定收率由2025年12月生产完成情况表回算。",
    ])
    if adjusted_initial_units:
        case.warnings.append(
            "初始负荷按有效上下限裁剪：" + "、".join(sorted(adjusted_initial_units))
        )
    validate_collaborative_case(case)
    return case


def validate_collaborative_case(case: baseline.CaseData) -> None:
    units = set(case.unit_feeds)
    missing_plants = sorted(units - set(cfg.UNIT_PLANTS))
    missing_capacity = sorted(units - set(case.unit_capacities))
    missing_yields = sorted(
        unit for unit in units
        if unit not in cfg.CDU_UNITS and unit not in case.secondary_yields
    )
    if missing_plants or missing_capacity or missing_yields:
        raise ValueError(
            "collaborative case is incomplete: "
            f"missing_plants={missing_plants}, "
            f"missing_capacity={missing_capacity}, "
            f"missing_yields={missing_yields}"
        )
    for unit, feeds in case.unit_feeds.items():
        if not feeds:
            raise ValueError(f"unit has no feeds: {unit}")
        ratios = case.feed_ratios.get(unit)
        if ratios:
            missing = set(feeds) - set(ratios)
            total = sum(ratios.get(material, 0.0) for material in feeds)
            if missing or abs(total - 1.0) > 1e-6:
                raise ValueError(
                    f"invalid feed ratios for {unit}: missing={sorted(missing)}, total={total}"
                )
    for unit, yields in case.secondary_yields.items():
        if unit not in units:
            continue
        total = sum(value for material, value in yields.items() if material != "损失")
        if total > 1.000001:
            raise ValueError(f"non-loss yields exceed 100% for {unit}: {total}")
    for unit in units:
        capacity = case.unit_capacities[unit]
        if not capacity.min_load <= capacity.initial <= capacity.max_load:
            raise ValueError(
                f"initial load outside calibrated bounds for {unit}: "
                f"{capacity.initial} not in [{capacity.min_load}, {capacity.max_load}]"
            )


def target_loads(case: baseline.CaseData, mode: str, alpha: float) -> dict[str, float]:
    if mode == "rated":
        return baseline.build_rated_target_loads(case, alpha)
    targets = {}
    for unit in sorted(case.unit_feeds):
        capacity = case.unit_capacities[unit]
        historical = float(case.target_loads.get(unit, capacity.initial)) * alpha
        targets[unit] = min(max(historical, capacity.min_load), capacity.max_load)
    return targets


def collect_materials(case: baseline.CaseData) -> list[str]:
    materials = set(case.tank_pools) | set(cfg.CRUDES)
    for feeds in case.unit_feeds.values():
        materials.update(feeds)
    for yields in case.secondary_yields.values():
        materials.update(yields)
    materials.update(case.cdu_side_map.values())
    for _, _, material in cfg.TRANSFER_CAPACITIES:
        materials.add(material)
    materials.discard("损失")
    return sorted(materials)


def plant_pool(case: baseline.CaseData, plant: str, material: str) -> baseline.TankPool:
    if plant == "延炼" and material in case.tank_pools:
        return case.tank_pools[material]
    if plant == "延石化" and material in cfg.YANSHIHUA_TANK_POOLS:
        values = cfg.YANSHIHUA_TANK_POOLS[material]
        return baseline.TankPool(
            material=material,
            tanks=[f"延石化:{material}:汇总池"],
            initial=float(values["initial"]),
            min_inventory=float(values["min_inventory"]),
            max_inventory=float(values["max_inventory"]),
            density=None,
        )
    return baseline.TankPool(material=material)


def build_and_solve(
    case: baseline.CaseData,
    horizon_days: int,
    output_dir: Path,
    target_mode: str,
    alpha: float,
    time_limit: float | None,
    mip_gap: float | None,
    write_lp: bool,
    enforce_terminal_inventory: bool,
) -> dict[str, Any]:
    try:
        import gurobipy as gp
        from gurobipy import GRB
    except ImportError as exc:
        raise RuntimeError("gurobipy is required to solve the collaborative model") from exc

    days = range(1, horizon_days + 1)
    plants = list(cfg.PLANTS)
    units = sorted(case.unit_feeds)
    materials = collect_materials(case)
    targets = target_loads(case, target_mode, alpha)
    initial_loads = {unit: case.unit_capacities[unit].initial for unit in units}
    unit_plant = {unit: cfg.UNIT_PLANTS[unit] for unit in units}
    nodes = [(plant, material) for plant in plants for material in materials]
    pools = {(plant, material): plant_pool(case, plant, material) for plant, material in nodes}
    transfer_arcs = sorted(cfg.TRANSFER_CAPACITIES)
    source_nodes = sorted(
        (plant, material)
        for plant, allowed in cfg.EXTERNAL_SOURCE_BY_PLANT.items()
        for material in allowed
        if material in materials
    )
    sale_nodes = sorted(
        (plant, material)
        for plant, allowed in cfg.SALEABLE_MATERIALS_BY_PLANT.items()
        for material in allowed
        if material in materials
    )
    disposal_nodes = sorted(
        (plant, material)
        for plant in plants
        for material in cfg.DISPOSABLE_MATERIALS
        if material in materials
    )
    terminal_nodes = [
        node for node, pool in pools.items()
        if pool.max_inventory > pool.min_inventory + 1e-9
    ] if enforce_terminal_inventory else []

    model = gp.Model("yanlian_yanshihua_collaborative_schedule")
    if time_limit is not None:
        model.Params.TimeLimit = time_limit
    if mip_gap is not None:
        model.Params.MIPGap = mip_gap

    load = model.addVars(units, days, lb=0.0, name="unit_load")
    on = model.addVars(units, days, vtype=GRB.BINARY, name="unit_on")
    switch = model.addVars(units, days, vtype=GRB.BINARY, name="unit_switch")
    load_short = model.addVars(units, days, lb=0.0, name="load_shortfall")
    load_over = model.addVars(units, days, lb=0.0, name="load_overage")
    load_up = model.addVars(units, days, lb=0.0, name="load_change_up")
    load_down = model.addVars(units, days, lb=0.0, name="load_change_down")
    feed_keys = [(unit, material, day) for unit in units for material in case.unit_feeds[unit] for day in days]
    feed = model.addVars(feed_keys, lb=0.0, name="feed")
    prod = model.addVars(units, materials, days, lb=0.0, name="production")
    inv = model.addVars(nodes, days, lb=0.0, name="inventory")
    inv_up = model.addVars(nodes, days, lb=0.0, name="inventory_increase_abs")
    inv_down = model.addVars(nodes, days, lb=0.0, name="inventory_decrease_abs")
    transfer = model.addVars(transfer_arcs, days, lb=0.0, name="interplant_transfer")
    source = model.addVars(source_nodes, days, lb=0.0, name="external_source")
    sale = model.addVars(sale_nodes, days, lb=0.0, name="shipment")
    disposal = model.addVars(disposal_nodes, days, lb=0.0, name="controlled_disposal")

    for unit in units:
        capacity = case.unit_capacities[unit]
        for day in days:
            model.addConstr(
                load[unit, day] == gp.quicksum(feed[unit, material, day] for material in case.unit_feeds[unit]),
                name=f"load_def[{unit},{day}]",
            )
            model.addConstr(load[unit, day] <= capacity.max_load * on[unit, day], name=f"unit_max[{unit},{day}]")
            if capacity.min_load > 0:
                model.addConstr(load[unit, day] >= capacity.min_load * on[unit, day], name=f"unit_min[{unit},{day}]")
            model.addConstr(
                load[unit, day] + load_short[unit, day] - load_over[unit, day] == targets[unit],
                name=f"target_load[{unit},{day}]",
            )
            previous = initial_loads[unit] if day == 1 else load[unit, day - 1]
            model.addConstr(load[unit, day] - previous == load_up[unit, day] - load_down[unit, day], name=f"load_change[{unit},{day}]")
            previous_on = (1 if initial_loads[unit] > 1e-9 else 0) if day == 1 else on[unit, day - 1]
            model.addConstr(switch[unit, day] >= on[unit, day] - previous_on, name=f"switch_up[{unit},{day}]")
            model.addConstr(switch[unit, day] >= previous_on - on[unit, day], name=f"switch_down[{unit},{day}]")

    for unit, ratios in case.feed_ratios.items():
        if unit not in case.unit_feeds or len(case.unit_feeds[unit]) <= 1:
            continue
        for material in case.unit_feeds[unit]:
            ratio = ratios[material]
            for day in days:
                model.addConstr(feed[unit, material, day] == ratio * load[unit, day], name=f"feed_ratio[{unit},{material},{day}]")

    for unit in units:
        if unit in cfg.CDU_UNITS:
            for material in materials:
                for day in days:
                    terms = []
                    for crude in cfg.CRUDES:
                        if crude not in case.unit_feeds[unit]:
                            continue
                        for side_id, output_material in case.cdu_side_map.items():
                            if output_material == material:
                                coefficient = case.cdu_yields.get((crude, side_id), 0.0)
                                if coefficient:
                                    terms.append(feed[unit, crude, day] * coefficient)
                    model.addConstr(prod[unit, material, day] == gp.quicksum(terms), name=f"cdu_prod[{unit},{material},{day}]")
        else:
            yields = case.secondary_yields[unit]
            for material in materials:
                coefficient = yields.get(material, 0.0)
                for day in days:
                    model.addConstr(prod[unit, material, day] == coefficient * load[unit, day], name=f"fixed_yield[{unit},{material},{day}]")

    source_node_set = set(source_nodes)
    sale_node_set = set(sale_nodes)
    disposal_node_set = set(disposal_nodes)
    for source_plant, destination_plant, material in transfer_arcs:
        capacity = float(cfg.TRANSFER_CAPACITIES[(source_plant, destination_plant, material)])
        for day in days:
            model.addConstr(
                transfer[source_plant, destination_plant, material, day] <= capacity,
                name=f"transfer_capacity[{source_plant},{destination_plant},{material},{day}]",
            )

    for plant, material in nodes:
        pool = pools[(plant, material)]
        working_inventory = pool.max_inventory > pool.min_inventory + 1e-9
        min_inventory = pool.min_inventory if working_inventory else 0.0
        max_inventory = pool.max_inventory if working_inventory else 0.0
        if not min_inventory - 1e-9 <= pool.initial <= max_inventory + 1e-9:
            raise ValueError(f"invalid plant inventory for {(plant, material)}: {asdict(pool)}")
        producing_units = [unit for unit in units if unit_plant[unit] == plant]
        consuming_units = [unit for unit in producing_units if material in case.unit_feeds[unit]]
        incoming_arcs = [arc for arc in transfer_arcs if arc[1] == plant and arc[2] == material]
        outgoing_arcs = [arc for arc in transfer_arcs if arc[0] == plant and arc[2] == material]
        for day in days:
            previous_inventory = pool.initial if day == 1 else inv[plant, material, day - 1]
            production = gp.quicksum(prod[unit, material, day] for unit in producing_units)
            consumption = gp.quicksum(feed[unit, material, day] for unit in consuming_units)
            inbound = gp.quicksum(transfer[src, dst, mat, day] for src, dst, mat in incoming_arcs)
            outbound = gp.quicksum(transfer[src, dst, mat, day] for src, dst, mat in outgoing_arcs)
            external = source[plant, material, day] if (plant, material) in source_node_set else 0.0
            shipped = sale[plant, material, day] if (plant, material) in sale_node_set else 0.0
            disposed = disposal[plant, material, day] if (plant, material) in disposal_node_set else 0.0
            model.addConstr(
                inv[plant, material, day] == previous_inventory + production + external + inbound - consumption - outbound - shipped - disposed,
                name=f"plant_balance[{plant},{material},{day}]",
            )
            model.addConstr(inv[plant, material, day] >= min_inventory, name=f"inventory_min[{plant},{material},{day}]")
            model.addConstr(inv[plant, material, day] <= max_inventory, name=f"inventory_max[{plant},{material},{day}]")
            model.addConstr(
                inv[plant, material, day] - previous_inventory == inv_up[plant, material, day] - inv_down[plant, material, day],
                name=f"inventory_change[{plant},{material},{day}]",
            )

    for plant, material in terminal_nodes:
        model.addConstr(
            inv[plant, material, horizon_days] >= pools[(plant, material)].initial,
            name=f"terminal_inventory[{plant},{material}]",
        )

    weights = cfg.COLLABORATIVE_OBJECTIVE_WEIGHTS
    model.setObjective(
        weights["load_deviation"] * gp.quicksum(load_short[unit, day] + load_over[unit, day] for unit in units for day in days)
        + weights["unit_switch"] * gp.quicksum(switch[unit, day] for unit in units for day in days)
        + weights["inventory_change"] * gp.quicksum(inv_up[plant, material, day] + inv_down[plant, material, day] for plant, material in nodes for day in days)
        + weights["external_source"] * gp.quicksum(source[plant, material, day] for plant, material in source_nodes for day in days)
        + weights["shipment"] * gp.quicksum(sale[plant, material, day] for plant, material in sale_nodes for day in days)
        + weights["load_variation"] * gp.quicksum(load_up[unit, day] + load_down[unit, day] for unit in units for day in days)
        + weights["transfer"] * gp.quicksum(transfer[src, dst, material, day] for src, dst, material in transfer_arcs for day in days)
        + weights["disposal"] * gp.quicksum(disposal[plant, material, day] for plant, material in disposal_nodes for day in days),
        GRB.MINIMIZE,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    if write_lp:
        model.write(str(output_dir / "collaborative_schedule.lp"))
    model.optimize()

    result = summarize_solution(
        model=model,
        days=days,
        plants=plants,
        units=units,
        materials=materials,
        unit_plant=unit_plant,
        targets=targets,
        load=load,
        load_short=load_short,
        load_over=load_over,
        transfer_arcs=transfer_arcs,
        transfer=transfer,
        source_nodes=source_nodes,
        source=source,
        sale_nodes=sale_nodes,
        sale=sale,
        disposal_nodes=disposal_nodes,
        disposal=disposal,
        inv=inv,
        pools=pools,
        target_mode=target_mode,
        horizon_days=horizon_days,
        warnings=case.warnings,
    )
    if model.SolCount:
        write_solution_csvs(
            output_dir, days, units, materials, unit_plant, targets, load,
            plants, inv, source_nodes, source, sale_nodes, sale,
            transfer_arcs, transfer,
        )
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def summarize_solution(
    *, model: Any, days: range, plants: list[str], units: list[str],
    materials: list[str], unit_plant: dict[str, str], targets: dict[str, float],
    load: Any, load_short: Any, load_over: Any, transfer_arcs: list[tuple[str, str, str]],
    transfer: Any, source_nodes: list[tuple[str, str]], source: Any,
    sale_nodes: list[tuple[str, str]], sale: Any,
    disposal_nodes: list[tuple[str, str]], disposal: Any, inv: Any,
    pools: dict[tuple[str, str], baseline.TankPool], target_mode: str,
    horizon_days: int, warnings: list[str],
) -> dict[str, Any]:
    result = {
        "status": int(model.Status),
        "status_name": baseline.status_name(model.Status),
        "objective": float(model.ObjVal) if model.SolCount else None,
        "optimization_mode": "yanlian_yanshihua_collaborative",
        "target_mode": target_mode,
        "horizon_days": horizon_days,
        "plants": plants,
        "units_by_plant": {
            plant: [unit for unit in units if unit_plant[unit] == plant]
            for plant in plants
        },
        "unit_count": len(units),
        "material_count": len(materials),
        "transfer_arc_count": len(transfer_arcs),
        "solution_count": int(model.SolCount),
        "runtime_seconds": round(float(model.Runtime), 6),
        "warnings": warnings,
    }
    if not model.SolCount:
        return result
    completion = {}
    load_ratio = {}
    shortfall_by_plant = {plant: 0.0 for plant in plants}
    for unit in units:
        actual = sum(float(load[unit, day].X) for day in days)
        planned = targets[unit] * horizon_days
        load_ratio[unit] = actual / planned if planned > 1e-9 else None
        completion[unit] = min(1.0, load_ratio[unit]) if load_ratio[unit] is not None else None
        shortfall_by_plant[unit_plant[unit]] += sum(float(load_short[unit, day].X) for day in days)
    transfers = {
        f"{src}->{dst}:{material}": round(sum(float(transfer[src, dst, material, day].X) for day in days), 6)
        for src, dst, material in transfer_arcs
        if sum(float(transfer[src, dst, material, day].X) for day in days) > 1e-6
    }
    external = {
        f"{plant}:{material}": round(sum(float(source[plant, material, day].X) for day in days), 6)
        for plant, material in source_nodes
        if sum(float(source[plant, material, day].X) for day in days) > 1e-6
    }
    shipments = {
        f"{plant}:{material}": round(sum(float(sale[plant, material, day].X) for day in days), 6)
        for plant, material in sale_nodes
        if sum(float(sale[plant, material, day].X) for day in days) > 1e-6
    }
    disposals = {
        f"{plant}:{material}": round(sum(float(disposal[plant, material, day].X) for day in days), 6)
        for plant, material in disposal_nodes
        if sum(float(disposal[plant, material, day].X) for day in days) > 1e-6
    }
    terminal = []
    for (plant, material), pool in pools.items():
        if pool.max_inventory <= pool.min_inventory + 1e-9:
            continue
        terminal.append({
            "plant": plant,
            "material": material,
            "initial_inventory_t": round(pool.initial, 6),
            "ending_inventory_t": round(float(inv[plant, material, horizon_days].X), 6),
        })
    result["solution_metrics"] = {
        "plan_completion_by_unit": completion,
        "load_ratio_by_unit": load_ratio,
        "load_shortfall_by_plant_t": {plant: round(value, 6) for plant, value in shortfall_by_plant.items()},
        "external_source_total_t": round(sum(external.values()), 6),
        "shipment_total_t": round(sum(shipments.values()), 6),
        "transfer_total_t": round(sum(transfers.values()), 6),
        "controlled_disposal_total_t": round(sum(disposals.values()), 6),
        "transfers": transfers,
        "external_sources": external,
        "shipments": shipments,
        "controlled_disposals": disposals,
        "terminal_inventories": terminal,
    }
    return result


def write_solution_csvs(
    output_dir: Path, days: range, units: list[str], materials: list[str],
    unit_plant: dict[str, str], targets: dict[str, float], load: Any,
    plants: list[str], inv: Any, source_nodes: list[tuple[str, str]], source: Any,
    sale_nodes: list[tuple[str, str]], sale: Any,
    transfer_arcs: list[tuple[str, str, str]], transfer: Any,
) -> None:
    with (output_dir / "unit_loads.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "plant", "unit", "load_t_per_d", "target_t_per_d", "plan_completion_rate", "load_ratio"])
        for day in days:
            for unit in units:
                actual = float(load[unit, day].X)
                target = targets[unit]
                ratio = actual / target if target else None
                writer.writerow([
                    day, unit_plant[unit], unit, round(actual, 6), round(target, 6),
                    round(min(1.0, ratio), 9) if ratio is not None else "",
                    round(ratio, 9) if ratio is not None else "",
                ])
    source_set = set(source_nodes)
    sale_set = set(sale_nodes)
    with (output_dir / "plant_inventories.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "plant", "material", "inventory_t", "external_source_t", "shipment_t"])
        for day in days:
            for plant in plants:
                for material in materials:
                    source_value = float(source[plant, material, day].X) if (plant, material) in source_set else 0.0
                    sale_value = float(sale[plant, material, day].X) if (plant, material) in sale_set else 0.0
                    writer.writerow([day, plant, material, round(float(inv[plant, material, day].X), 6), round(source_value, 6), round(sale_value, 6)])
    with (output_dir / "interplant_transfers.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "source_plant", "destination_plant", "material", "transfer_t"])
        for day in days:
            for source_plant, destination_plant, material in transfer_arcs:
                writer.writerow([day, source_plant, destination_plant, material, round(float(transfer[source_plant, destination_plant, material, day].X), 6)])


def inspect_data(case: baseline.CaseData, target_mode: str, alpha: float) -> dict[str, Any]:
    materials = collect_materials(case)
    targets = target_loads(case, target_mode, alpha)
    return {
        "optimization_mode": "yanlian_yanshihua_collaborative",
        "plants": list(cfg.PLANTS),
        "unit_count": len(case.unit_feeds),
        "units_by_plant": {
            plant: sorted(unit for unit in case.unit_feeds if cfg.UNIT_PLANTS[unit] == plant)
            for plant in cfg.PLANTS
        },
        "material_count": len(materials),
        "target_mode": target_mode,
        "target_loads_t_per_d": {unit: round(value, 6) for unit, value in targets.items()},
        "transfer_capacities_t_per_d": {
            f"{src}->{dst}:{material}": capacity
            for (src, dst, material), capacity in sorted(cfg.TRANSFER_CAPACITIES.items())
        },
        "yanshihua_tank_pools": cfg.YANSHIHUA_TANK_POOLS,
        "warnings": case.warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solve the Yanlian--Yanshihua collaborative refinery schedule.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--horizon-days", type=int, default=cfg.DEFAULT_HORIZON_DAYS)
    parser.add_argument("--yco-share", type=float, default=cfg.DEFAULT_YCO_SHARE)
    parser.add_argument("--target-mode", choices=("historical", "rated"), default="historical")
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--no-terminal-inventory", action="store_true")
    parser.add_argument("--inspect-data", action="store_true")
    parser.add_argument("--write-lp", action="store_true")
    parser.add_argument("--time-limit", type=float, default=None)
    parser.add_argument("--mip-gap", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.horizon_days <= 0:
        raise ValueError("--horizon-days must be positive")
    if not 0.0 <= args.yco_share <= 1.0:
        raise ValueError("--yco-share must be between 0 and 1")
    if args.alpha <= 0.0:
        raise ValueError("--alpha must be positive")
    case = build_collaborative_case(args.yco_share)
    if args.inspect_data:
        print(json.dumps(inspect_data(case, args.target_mode, args.alpha), ensure_ascii=False, indent=2))
        return
    try:
        result = build_and_solve(
            case=case,
            horizon_days=args.horizon_days,
            output_dir=args.output_dir,
            target_mode=args.target_mode,
            alpha=args.alpha,
            time_limit=args.time_limit,
            mip_gap=args.mip_gap,
            write_lp=args.write_lp,
            enforce_terminal_inventory=not args.no_terminal_inventory,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
