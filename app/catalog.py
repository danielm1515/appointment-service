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


_DISCLAIMER = "טיוטת דמו – טעונה אישור רפואי. בכל שאלה רפואית יש לפנות לצוות המטפל."


@dataclass(frozen=True)
class ExamType:
    code: str
    department: str  # a Department.value above - a rule, not a suggestion (design D1)
    label: str
    documents: tuple[str, ...]  # suggested required documents; pre-ticked, not enforced
    instruction_id: str
    instruction_version: str
    instruction_title: str
    instruction_text: str


# The demo catalog (design §4): every exam belongs to one department and carries one
# preparation instruction. Every department has exactly one "…_VISIT" - its default exam for
# an appointment with no exam type of its own (an appointment booked before this change).
# The texts are demo drafts (design D4): common public practice, no medication doses, each
# ending with the fixed disclaimer sentence.
EXAM_TYPES: tuple[ExamType, ...] = (
    ExamType(
        "CARD_VISIT", "Cardiology", "ביקור במרפאה קרדיולוגית", ("ECG",),
        "INSTR-CARD-VISIT", "1", "הכנה לביקור במרפאה קרדיולוגית",
        "מומלץ להביא רשימה מעודכנת של התרופות שאתם נוטלים. כדאי לצרף סיכומים רפואיים ובדיקות "
        "קודמות רלוונטיות, כגון אק\"ג או אקו לב, אם יש ברשותכם. אין צורך בצום או בהכנה מיוחדת "
        "לביקור במרפאה. יש להגיע כ-15 דקות לפני מועד התור לצורך קליטה. " + _DISCLAIMER,
    ),
    ExamType(
        "CARD_ECHO", "Cardiology", "אקו לב", (),
        "INSTR-CARD-ECHO", "1", "הכנה לאקו לב",
        "בדיקת אקו לב אינה דורשת הכנה מיוחדת. אפשר לאכול, לשתות וליטול תרופות כרגיל לפני "
        "הבדיקה. מומלץ להגיע עם לבוש נוח המאפשר חשיפת בית החזה. הבדיקה אינה כואבת ואורכת כמה "
        "עשרות דקות. " + _DISCLAIMER,
    ),
    ExamType(
        "CARD_STRESS", "Cardiology", "מבחן מאמץ", ("ECG",),
        "INSTR-CARD-STRESS", "1", "הכנה למבחן מאמץ",
        "יש לצום כ-3 שעות לפני הבדיקה; שתיית מים מותרת. יש להימנע מקפאין (קפה, תה, שוקולד, "
        "משקאות אנרגיה) במשך 12 שעות לפני הבדיקה. נטילת תרופות חוסמות בטא לפני הבדיקה - רק לפי "
        "הנחיית הרופא המפנה. יש להגיע עם בגדים ונעלי ספורט נוחים המתאימים למאמץ גופני. " + _DISCLAIMER,
    ),
    ExamType(
        "CARD_HOLTER", "Cardiology", "הולטר לב", (),
        "INSTR-CARD-HOLTER", "1", "הכנה להתקנת הולטר לב",
        "מומלץ להתקלח לפני ההגעה למרפאה, מכיוון שאסור להרטיב את המכשיר במשך כ-24 שעות מרגע "
        "ההתקנה. יש להגיע עם חולצה מכופתרת או פתוחה מלפנים, כדי להקל על הצמדת החיישנים. "
        "במהלך הבדיקה מומלץ לנהל יומן תסמינים ופעילויות. יש לחזור למרפאה למחרת להחזרת "
        "המכשיר. " + _DISCLAIMER,
    ),
    ExamType(
        "DERM_VISIT", "Dermatology", "ביקור במרפאת עור", (),
        "INSTR-DERM-VISIT", "1", "הכנה לביקור במרפאת עור",
        "מומלץ להביא רשימה מעודכנת של תרופות ומשחות הנמצאות בשימוש. אם יש נגע עורי שהשתנה, "
        "כדאי לצלם אותו מראש ולהביא את התמונה לביקור. אין צורך בהכנה מיוחדת נוספת לביקור "
        "במרפאה. אפשר להגיע בלבוש רגיל המאפשר חשיפה נוחה של האזור הנבדק. " + _DISCLAIMER,
    ),
    ExamType(
        "DERM_MOLES", "Dermatology", "מיפוי שומות", (),
        "INSTR-DERM-MOLES", "1", "הכנה למיפוי שומות",
        "יש להגיע עם עור נקי, ללא קרמים, איפור או לק על הציפורניים. יש להימנע משיזוף או חשיפה "
        "ממושכת לשמש במשך כשבועיים לפני הבדיקה. אם בוצע מיפוי שומות קודם, מומלץ להביא אותו "
        "להשוואה. הבדיקה כוללת צילום כלל הגוף במכשיר ייעודי. " + _DISCLAIMER,
    ),
    ExamType(
        "NEURO_VISIT", "Neurology", "ביקור במרפאה נוירולוגית", (),
        "INSTR-NEURO-VISIT", "1", "הכנה לביקור במרפאה נוירולוגית",
        "מומלץ להביא רשימה מעודכנת של התרופות הנלקחות. כדאי לצרף הדמיות קודמות (כגון MRI או "
        "CT) אם קיימות. אם רלוונטי, מומלץ לנהל יומן התקפים או תסמינים ולהביאו לביקור. אין "
        "צורך בהכנה מיוחדת נוספת. " + _DISCLAIMER,
    ),
    ExamType(
        "NEURO_EEG", "Neurology", "EEG", (),
        "INSTR-NEURO-EEG", "2", "הכנה לבדיקת EEG",
        "יש לחפוף את השיער בערב שלפני הבדיקה, ללא שימוש בג'ל, שמן או תרסיסי עיצוב. אפשר לאכול "
        "כרגיל ואין צורך בצום. מומלץ לישון כרגיל בלילה שלפני הבדיקה, אלא אם נמסרה הנחיה אחרת. "
        "בכל שאלה לגבי תרופות יש לפנות לרופא המפנה. " + _DISCLAIMER,
    ),
    ExamType(
        "NEURO_EMG", "Neurology", "EMG", (),
        "INSTR-NEURO-EMG", "1", "הכנה לבדיקת EMG",
        "יש להימנע ממריחת קרמים או תחליבי גוף על העור באזור הנבדק ביום הבדיקה. מומלץ להגיע "
        "עם בגדים רחבים המאפשרים חשיפה נוחה של הגפיים. חשוב לדווח מראש לצוות על נטילת תרופות "
        "מדללות דם או על קוצב לב. אין צורך בצום לפני הבדיקה. " + _DISCLAIMER,
    ),
    ExamType(
        "OPHTH_VISIT", "Ophthalmology", "בדיקת עיניים", (),
        "INSTR-OPHTH-VISIT", "1", "הכנה לבדיקת עיניים",
        "יש להביא משקפיים ועדשות מגע קיימות לבדיקה. שימוש בעדשות מגע ביום הבדיקה - לפי "
        "הנחיית המרפאה בלבד. מומלץ להביא רשימת תרופות עיניים בשימוש, אם יש. אין צורך בהכנה "
        "מיוחדת נוספת. " + _DISCLAIMER,
    ),
    ExamType(
        "OPHTH_DILATED", "Ophthalmology", "בדיקה עם הרחבת אישונים", (),
        "INSTR-OPHTH-DILATED", "1", "הכנה לבדיקה עם הרחבת אישונים",
        "לאחר הטיפות המרחיבות הראייה עלולה להיות מטושטשת למשך כ-4 עד 6 שעות. אין לנהוג ברכב "
        "לאחר הבדיקה, ולכן מומלץ להגיע עם מלווה. כדאי להביא משקפי שמש להקלה על הרגישות לאור. "
        "אפשר לאכול ולשתות כרגיל לפני הבדיקה. " + _DISCLAIMER,
    ),
    ExamType(
        "ORTHO_VISIT", "Orthopedics", "ביקור במרפאה אורתופדית", (),
        "INSTR-ORTHO-VISIT", "1", "הכנה לביקור במרפאה אורתופדית",
        "מומלץ להביא צילומים והדמיות קודמים של האזור הנבדק, אם קיימים. יש להגיע בלבוש "
        "המאפשר חשיפה נוחה של המפרק או האזור הנבדק. אין צורך בצום או בהכנה מיוחדת נוספת. "
        "אפשר להמשיך ליטול תרופות כרגיל. " + _DISCLAIMER,
    ),
    ExamType(
        "ORTHO_INJECTION", "Orthopedics", "זריקה למפרק", (),
        "INSTR-ORTHO-INJECTION", "2", "הכנה לזריקה למפרק",
        "אפשר לאכול כרגיל לפני הזריקה ואין צורך בצום. יש להביא רשימה מעודכנת של התרופות "
        "הקבועות ולהציג אותה לצוות בקבלה. מומלץ להגיע בבגדים נוחים שמאפשרים חשיפה של המפרק. "
        "אם הצוות ביקש זאת, מומלץ להגיע עם מלווה לחזרה הביתה. " + _DISCLAIMER,
    ),
)
EXAMS_BY_CODE = {e.code: e for e in EXAM_TYPES}


def exams_of(department: str) -> tuple[ExamType, ...]:
    return tuple(e for e in EXAM_TYPES if e.department == department)


def default_exam(department: str) -> ExamType:
    """The department's own "…_VISIT" exam - what an appointment with no exam type of its own
    resolves to (design D1/D2): a link-less appointment, or one from before this change."""
    for exam in exams_of(department):
        if exam.code.endswith("_VISIT"):
            return exam
    raise KeyError(f"no default (…_VISIT) exam type for department {department!r}")


def exam_type_label(code: str) -> str:
    exam = EXAMS_BY_CODE.get(code)
    return exam.label if exam else code
