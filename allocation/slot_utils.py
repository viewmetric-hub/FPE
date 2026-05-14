from datetime import datetime, time, timedelta


SLOT_INTERVAL_MINUTES = 15
# 15-minute intervals for a full day = 96 slots (00:00 .. 23:45)
SLOTS_PER_DAY = 96


def slot_index_to_time_block(slot_index: int) -> str:
    """
    IEX-style time block string for a slot.
    Slot 1 -> "00:00 - 00:15", Slot 2 -> "00:15 - 00:30", ... Slot 96 -> "23:45 - 24:00"
    """
    start_mins = (slot_index - 1) * SLOT_INTERVAL_MINUTES
    end_mins = slot_index * SLOT_INTERVAL_MINUTES
    base = datetime(2000, 1, 1, 0, 0, 0)

    def fmt(mins: int) -> str:
        if mins >= 1440:  # 24:00
            return "24:00"
        dt = base + timedelta(minutes=mins)
        return dt.strftime("%H:%M")

    return f"{fmt(start_mins)} - {fmt(end_mins)}"


def generate_day_slots() -> list[dict]:
    """
    Generates 15-minute slot start times for a full day.
    Includes IEX-style time_block (e.g. "00:00 - 00:15").

    Slot 1: 00:00
    Slot 96: 23:45  (96 * 15 minutes = 1440 minutes = full-day coverage)
    """
    base = datetime(2000, 1, 1, 0, 0, 0)
    out: list[dict] = []
    for slot_index in range(1, SLOTS_PER_DAY + 1):
        dt = base + timedelta(minutes=(slot_index - 1) * SLOT_INTERVAL_MINUTES)
        out.append(
            {
                "slot_index": slot_index,
                "slot_time": dt.time().replace(second=0, microsecond=0),
                "time_block": slot_index_to_time_block(slot_index),
            }
        )
    return out

