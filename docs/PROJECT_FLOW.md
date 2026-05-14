# Energy Intelligence Platform — Project Flow

This document describes how the Django project is structured and how data moves through the main user journeys. Code lives under the workspace root (e.g. `allocation/`, `dashboard/`, `core/`, `accounts/`).

## High-level architecture

- **Django** serves HTML dashboards (`dashboard/`) and **REST APIs** under `/api/` (`allocation/api`, `core/api`, `accounts/api`).
- **Roles** (simplified): platform admin, **consumer manager** (owns a `Consumer` and its plants), **plant user** (per-plant demand entry), **generator** (submits supply schedules for consumers).
- **Time model**: the trading day is **96 slots** of **15 minutes** (`allocation/slot_utils.generate_day_slots`).

## URL routing

- **Site pages**: `fpe/urls.py` mounts `dashboard.urls` at `/` and API apps at `/api/`.
- **Allocation & demand APIs**: `allocation/api/urls.py` — demand entry, generator supply, consumer allocation, plantwise recommendations, IEX helpers, etc.
- **Key pages** (`dashboard/urls.py`): login, consumer manager dashboard, demand entry, generator allocation, **plantwise allocation** (`/consumer-plantwise-allocation/`), IEX predictor, plant management.

## Core domain entities (`core`, `allocation`)

- **Consumer**: organizational bucket for multiple **Plants** and generator-facing workflows.
- **Plant**: tariffs, transmission loss (per year), hourly tariff difference; linked to demand schedules.
- **Demand**: `DemandSchedule` + **96×** `DemandSlot` per plant per day (`demand_mw` per slot — net at plant in the DB; gross demand for allocation uses transmission uplift — see allocation doc).
- **Generator supply**: `GeneratorSupplySchedule` + **96×** `GeneratorSupplySlot` (`supply_mwh` per slot) per **consumer** per day.
- **Approvals**:
  - `ConsumerDemandApproval` — consumer manager approves demand for a day.
  - `GeneratorScheduleApproval` — exposes approved demand to the generator UI.
  - `ConsumerGeneratorAllocationRun` + `ConsumerGeneratorAllocationOverride` — tracks **plantwise AI allocation** run status (`SUGGESTED` / `APPROVED`) and per-plant **AI day-total overrides** after approval.

## End-to-end business flow

### 1. Demand capture

1. Plant users or consumer managers enter **96-slot demand** per plant (`DemandEntryView`, consumer manager summary/plant slot APIs).
2. Consumer manager **approves demand** (`ConsumerManagerApproveDemandView`).
3. Optionally **approve for generator** (`ConsumerManagerApproveGeneratorScheduleView` / `GeneratorScheduleApproval`) so the generator can see aggregate demand.

### 2. Generator supply

1. Generator selects consumer + date, loads aggregate demand (`GeneratorConsumerDemandSlotsView`).
2. Generator posts **96-slot supply** (`GeneratorSupplyScheduleView`).
3. Validation enforces **per-slot supply ≤ aggregate consumer demand** (and related rules in that view).

### 3. Consumer “generator allocations” view

- **Consumer allocation** page uses APIs such as `ConsumerAllocationSlotsView` to show how generator supply relates to demand for the consumer’s day (slot-level).

### 4. Plantwise allocation (AI-assisted split)

1. Frontend: `/consumer-plantwise-allocation/` loads **`GET /api/consumer-generator-allocations/recommendations/?date=...`** (`ConsumerGeneratorAllocationRecommendationsView`).
2. Backend runs **`compute_allocation_with_ai_overrides`** (`allocation/ai_allocator.py`): reads **gross** demand per plant per slot, **generator supply** per slot, optional **IEX MCP** per slot, and optional **approved overrides** from DB.
3. Response includes plant totals, slot rows, **hourly/slot savings analysis** (allocate vs sell at IEX using contract tariff), **`plant_slot_allocations`** (96 rows per plant: base, AI, final gross), and UI-specific fields (e.g. base supply display, tariffs).
4. Consumer manager **approves** via **`POST /api/consumer-generator-allocations/approve/`** with per-plant AI day totals; server stores overrides and marks run `APPROVED`. Next `recommendations` GET recomputes **final** slot AI using those totals.

### 5. Supporting flows

- **IEX**: MCP fetch/compare endpoints for green day-ahead and predictor UI (`IexGreenDayAheadMcpView`, `IexPredictionCompareView`).
- **Plant management / tariffs / transmission loss**: `core` APIs and plant management UI.
- **Auth**: JWT-style access in `accounts` APIs; dashboard pages use `localStorage` tokens and `fetchWithAuth` patterns.

## Where to read code

| Area | Location |
|------|----------|
| Allocation math | `allocation/ai_allocator.py` |
| Recommendations + savings API | `allocation/api/views.py` (`ConsumerGeneratorAllocationRecommendationsView`) |
| Approve allocation | `allocation/api/views.py` (`ConsumerGeneratorAllocationApproveView`) |
| Generator supply validation | `allocation/api/views.py` (`GeneratorSupplyScheduleView`) |
| Models | `allocation/models.py`, `core/models.py` |
| Plantwise UI | `dashboard/templates/dashboard/consumer_plantwise_allocation.html` |

For a **detailed description of the 55%/45% split, greedy AI ordering, and override redistribution**, see **`docs/ALLOCATION_ALGORITHM.md`**.
