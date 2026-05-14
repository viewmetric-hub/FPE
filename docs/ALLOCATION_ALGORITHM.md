# Allocation Algorithm — Technical Reference

**Source of truth:** `allocation/ai_allocator.py`  
**Entry point for UI/API:** `compute_allocation_with_ai_overrides()`  
**Constants:** `BASE_PORTION = 0.55`, `AI_PORTION = 0.45` (used conceptually; the AI slice is “what remains after base,” capped by supply and demand).

---

## 1. Purpose

For a given **consumer** and **calendar date**, the algorithm:

1. Loads **96×15-minute** **gross demand** per plant (procurement-side MWh, consistent with Demand Entry uplift).
2. Loads **generator supply** **per slot** (total MWh scheduled for that consumer for that slot).
3. Splits each slot’s supply into:
   - **Base** allocation (~55% **of each plant’s gross demand** in that slot, scaled if supply is short).
   - **AI** allocation (remaining supply after base, assigned with a **greedy priority** across plants, **never exceeding** each plant’s remaining demand headroom in that slot).
4. Optionally reapplies **per-plant AI day-total overrides** (after manager approval) by **redistributing AI across slots** while respecting per-slot supply and per-plant per-slot caps.

Outputs include plant day totals, aggregate slot rows, raw greedy `allocations[slot][plant]`, and **`plant_slot_allocations`** (96 rows per plant: `base_mwh`, `ai_mwh`, `final_gross_mwh`).

---

## 2. Inputs

### 2.1 Demand (net → gross)

- **Stored:** `DemandSlot.demand_mw` per plant per slot — interpreted as **net at plant** (`_get_demand_by_plant_and_slot`).
- **For allocation:** `_get_demand_gross_by_plant_and_slot` converts net → **gross** using **`net_to_gross_additive`** (state % + central %):

\[
\text{gross} = \text{net} \times \bigl(1 + \frac{\text{state\%} + \text{central\%}}{100}\bigr)
\]

This matches the **additive** transmission model used in Demand Entry totals.

### 2.2 Supply

- `_get_supply_by_slot`: sums `GeneratorSupplySlot.supply_mwh` for the **consumer** and date, keyed by `slot_index` (1–96).

### 2.3 Optional MCP (IEX)

- `mcp_by_slot_index`: if present, used to sort plants for the **AI** slice by **(MCP − RE tariff)** as a proxy for export/sell margin.

---

## 3. Per-slot base allocation (55% layer)

For each slot \(s\) with total gross demand \(D_{\text{tot}} > 0\):

1. **Base need** per plant \(p\):  
   \(\text{need}_p = D_{p,\text{gross}} \times 0.55\).
2. **Total base need:** \(B_{\text{tot}} = \sum_p \text{need}_p\).
3. **Supply for the slot:** \(S\) (generator schedule).
4. If \(B_{\text{tot}} > S\), scale all base needs by \(\text{base\_ratio} = S / B_{\text{tot}}\); else \(\text{base\_ratio} = 1\).
5. **Base allocated** to plant \(p\): \(b_p = \text{need}_p \times \text{base\_ratio}\).

So base is **55% of each plant’s gross demand**, **proportionally curtailed** if the slot cannot serve full base need.

**AI supply pool for the slot:**

\[
S_{\text{AI}} = \max(0,\; S - \sum_p b_p)
\]

---

## 4. Per-slot AI allocation (greedy, demand-capped)

### 4.1 Remaining demand headroom per plant

After base:

\[
\text{remaining\_need\_AI}(p) = \max(0,\; D_{p,\text{gross}} - b_p)
\]

### 4.2 Plant ordering (priority)

- **If MCP known for slot:** sort plants by **descending** \((\text{MCP} - \text{RE\_tariff})\), tie-break by gross demand.
- **Else:** sort by **descending** slot-wise tariff difference `hourly_tariff_difference[slot_index-1]` (96 values, 15-minute slots); legacy 24 hourly values are expanded to four identical slots per hour. If missing, use `(grid_tariff - re_tariff)`; tie-break by gross demand.

### 4.3 Greedy assignment

Initialize `remaining_ai_supply = S_AI`.  
Visit plants in sorted order:

\[
a_p = \min(\text{remaining\_ai\_supply},\; \text{remaining\_need\_AI}(p))
\]

Subtract \(a_p\) from `remaining_ai_supply`. **No plant receives more than its slot demand minus base.**

Leftover supply after all plants are saturated stays **unallocated** at allocator level (45% “pool” may not be fully used if demand is binding).

---

## 5. Day totals and recommended AI

- **`plant_day[p]`** accumulates per slot: `demand` (gross), `base`, `ai` (recommended greedy AI).
- **`ai_recommended_total_mwh`** for UI = sum of greedy `ai` over the day for that plant.

---

## 6. AI overrides (after manager approval)

`compute_allocation_with_ai_overrides` receives `ai_override_total_by_plant_id` (from DB when run is **APPROVED**): **one number per plant** = target **AI MWh for the whole day**.

### 6.1 Trivial case

If there are **no** overrides, or every override **exactly equals** the greedy recommended AI total for that plant, the implementation **reuses** the greedy per-slot `allocations` (base + ai) — no rescheduling.

### 6.2 Non-trivial overrides

For each plant \(p\), target AI day total \(T_p\) is either the stored override or the recommended total.

1. **Denominator per plant (day):**  
   \(N_p = \max(0,\; \text{demand\_day\_gross}_p - \text{base\_day}_p)\)  
   (total “room” for AI across the day, using **same** base as greedy pass).

2. **For each slot** (in order):
   - Read **base** \(b_p\) from the **greedy** `allocations` (unchanged).
   - \(S_{\text{AI}} = \max(0,\; S - \sum_p b_p)\) as before.
   - **Proposed raw AI** for plant \(p\):  
     If \(N_p > 0\) and slot headroom \(h_{p,s} = \max(0, D_{p,s} - b_p) > 0\):  
     \(\text{raw}_{p} = T_p \times (h_{p,s} / N_p)\).  
     Else \(\text{raw}_{p} = 0\).
   - Cap: \(\text{raw}_{p} \leftarrow \min(\text{raw}_{p}, h_{p,s})\).
   - If \(\sum_p \text{raw}_{p} > S_{\text{AI}}\), scale all \(\text{raw}_p\) by \(S_{\text{AI}} / \sum \text{raw}\) (proportional cut to respect **slot supply**).

3. **Day final AI** for plant \(p\): sum of slot `raw_ai` after scaling — may be **less** than \(T_p\) if supply or per-slot caps bind.

**Important:** Overrides set **targets**; **supply and demand still cap** the realized AI.

---

## 7. Outputs (structures)

| Key | Meaning |
|-----|--------|
| `plants` | List of plant totals: `demand_total_mwh`, `base_total_mwh`, `ai_recommended_total_mwh`, `ai_final_total_mwh`, etc. |
| `slot_rows` | Per slot: aggregate `demand_mwh`, `allocated_mwh` (sum of base+final AI across plants). |
| `allocations` | Greedy slot map `allocations[slot][plant_id] = {base, ai}` (**ai** is recommended greedy; not rewritten in override branch). |
| `plant_slot_allocations` | Per plant, 96 entries: `base_mwh`, `ai_mwh`, `final_gross_mwh` (**final** uses override AI when applicable). |
| `demand_by_plant` | Gross demand by plant by slot (for savings / remaining-need in API layer). |

---

## 8. API layer (not in `ai_allocator.py`)

`ConsumerGeneratorAllocationRecommendationsView` adds:

- **Savings analysis** (hourly and per-slot): compare allocating unallocated energy to plants (tariff spread) vs **selling** on IEX using **MCP − contract tariff** (e.g. ₹3.58/kWh → Rs/MWh constant in views).
- **UI “base supply”** display may use business rules (e.g. 55% of total generator schedule split across plants) — distinct from **`base_total_mwh`** in plant totals, which comes from the allocator.

---

## 9. Transmission loss in the UI (post-approval modal)

The **“With transmission loss”** modal uses **per-slot** `final_gross_mwh` and applies a **combined multiplicative** view for display:

- \(L = \text{state\%} + \text{central\%}\)
- \(\text{net\_at\_plant} = \text{gross} \times (1 - L/100)\)
- \(\text{transmission\_loss\_mwh} = \text{gross} - \text{net\_at\_plant}\)

This is **consistent with the existing “View base allocation” modal** style; **demand gross** in the allocator still uses the **additive** `net_to_gross_additive` formula (see §2.1). Product owners should be aware these are **two different loss presentations** (procurement gross vs delivered net).

---

## 10. Quick reference (per slot)

```
base_need[p]     = gross_demand[p] * 0.55
base[p]          = base_need[p] * min(1, supply / sum(base_need))
ai_supply_left   = max(0, supply - sum(base))
Sort plants by MCP-RE or hourly tariff spread
ai[p]            = greedy min(ai_supply_left, gross_demand[p] - base[p])
```

**With day AI override targets:** replace per-slot `ai[p]` by proportional split of target across slots, then **scale down** if \(\sum_p ai[p]\) exceeds `ai_supply_left`.
