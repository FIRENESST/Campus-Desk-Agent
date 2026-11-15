"""Synthetic venue inputs for tests; never loaded by the application."""
from store import GymStore

VENUES = tuple(
    {"id": ident, "name": f"测试场馆 {i}", "capacity": courts * places,
     "courts": courts, "court_type": court_type, "places": places}
    for i, (ident, courts, places, court_type) in enumerate((
        ("strength", 2, 1, "训练区"), ("cardio", 3, 1, "训练区"), ("yoga", 2, 4, "教室"),
        ("student-center", 3, 1, "活动室"), ("indoor-basketball", 3, 1, "场"),
        ("indoor-badminton", 6, 1, "场"), ("table-tennis", 4, 1, "桌"),
    ), 1)
)

def gym_store(*args, **kwargs):
    return GymStore(*args, venues=VENUES, **kwargs)
