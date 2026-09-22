"""What the booking form offers: the departments, each one's doctors and locations, and the
appointment times. The stored values are the English ones the service already uses (the
lookup API returns them to the Hospital Agent); the form shows the Hebrew label beside them.

The server books only what is listed here - a doctor or location must belong to the chosen
department, and a time must be one of the slots - so the lists are a rule, not a suggestion.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Department:
    value: str
    label: str
    doctors: tuple[str, ...]
    locations: tuple[str, ...]


DEPARTMENTS: tuple[Department, ...] = (
    Department("Cardiology", "קרדיולוגיה", ("Dr. Mizrahi", "Dr. Friedman"), ("Building A, Floor 3",)),
    Department("Dermatology", "עור", ("Dr. Katz",), ("Building A, Floor 1",)),
    Department("Neurology", "נוירולוגיה", ("Dr. Cohen", "Dr. Shapiro"), ("Building B, Floor 2",)),
    Department("Ophthalmology", "עיניים", ("Dr. Azulay",), ("Building D, Floor 2",)),
    Department("Orthopedics", "אורתופדיה", ("Dr. Levi", "Dr. Peretz"),
               ("Building C, Floor 1", "Building C, Floor 2")),
)
BY_VALUE = {d.value: d for d in DEPARTMENTS}

# 24-hour clock, every 15 minutes from 08:00 to 17:45.
TIME_SLOTS: tuple[str, ...] = tuple(f"{h:02d}:{m:02d}" for h in range(8, 18) for m in (0, 15, 30, 45))


def department_label(value: str) -> str:
    department = BY_VALUE.get(value)
    return department.label if department else value


MONTHS: tuple[str, ...] = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט",
                           "ספטמבר", "אוקטובר", "נובמבר", "דצמבר")
# How many years ahead, counting the current one, the form offers.
BOOKING_YEARS = 3


def booking_years(this_year: int) -> tuple[int, ...]:
    return tuple(range(this_year, this_year + BOOKING_YEARS))


@dataclass(frozen=True)
class DocumentType:
    code: str
    label: str
    max_age_days: int  # used by the document service's validity check (design §2)


# The focused spec's document types (design §2). The code is what every system stores and sends.
DOCUMENT_TYPES: tuple[DocumentType, ...] = (
    DocumentType("CBC", "ספירת דם מלאה", 90),
    DocumentType("COAGULATION_TESTS", "בדיקות קרישה", 90),
    DocumentType("ECG", "תרשים פעילות חשמלית של הלב", 180),
    DocumentType("URINALYSIS", "בדיקת שתן", 90),
    DocumentType("PREOP_SUMMARY", "סיכום טרום ניתוח", 30),
)
DOCUMENT_TYPE_CODES = frozenset(t.code for t in DOCUMENT_TYPES)
_DOCUMENT_LABELS = {t.code: t.label for t in DOCUMENT_TYPES}


def document_type_label(code: str) -> str:
    return _DOCUMENT_LABELS.get(code, code)
