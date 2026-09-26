# Appointment Service MVP

שירות דטרמיניסטי לשליפת התור הרלוונטי של מטופל עבור Hospital Patient Agent (ה-API לקריאה בלבד), עם ממשק ניהול שבו מנהל מחובר קובע ומבטל תורים. המימוש כולל API ב-FastAPI, מסד SQLite פנימי, נתוני דמו, Audit טכני ואריזה כ-container יחיד.

## הפעלה

דרישה מקדימה: Docker Desktop פעיל.

```bash
docker compose up --build -d
docker compose ps
```

השירות זמין בכתובות:

- API: `http://localhost:8080`
- ממשק תורים: `http://localhost:8080/`
- Swagger: `http://localhost:8080/docs`
- Health: `http://localhost:8080/health`

בכניסה הראשונה הממשק יעבור ל-`/setup`. קוד ההקמה המקומי הוא `local-setup-token`. יש לבחור סיסמה באורך 12 תווים לפחות, לסרוק את ה-QR באמצעות Microsoft Authenticator ולאמת קוד בן 6 ספרות. בפריסה אמיתית חובה להחליף את `UI_SETUP_TOKEN` ואת `SESSION_SECRET` בערכים אקראיים ולהפעיל `SECURE_COOKIES=true` רק לאחר הגדרת HTTPS.

קריאות Hospital Agent ל-`CheckAppointment` מוגנות בכותרת `X-API-Key`. בסביבת Docker המקומית מפתח הפיתוח הוא `local-development-api-key`. בפריסה אמיתית חובה להגדיר ערך אקראי וחזק באמצעות `APPOINTMENT_API_KEY`, לשמור אותו גם בסודות של Hospital Agent ולא להכניס אותו לקוד, ל-image או ללוגים.

להגדרה מקומית, יש להעתיק את `.env.example` אל `.env`, להחליף את ערכי הדוגמה בסודות אקראיים, ולהריץ `docker compose up -d`. קובץ `.env` מוחרג מ-Git.

## בדיקת תור קיים

```bash
curl -i \
  -H "X-API-Key: local-development-api-key" \
  -H "X-Case-ID: CASE-001" \
  -H "X-Execution-ID: EXEC-001" \
  http://localhost:8080/api/v1/patients/P-10041/appointment
```

תוצאה צפויה: `200`, ‏`found=true` והתור `APT-8391`. `appointment_at` מוחזר תמיד עם אזור זמן (שעון ישראל, `+03:00` בקיץ ו-`+02:00` בחורף), כי Hospital Agent דוחה זמן בלי אזור זמן.

מטופל נוסף עם תור:

```bash
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-20000/appointment
```

תוצאה צפויה: `200`, ‏`found=true` והתור `APT-8392`. התשובה כוללת גם `required_documents` (ראו "קביעת וביטול תורים בממשק").

התשובה כוללת גם `exam_type` (`{code, label}`) ו-`instruction` (`{source_id, version, title}`, בלי הטקסט עצמו) - אף פעם לא ריקים: אלה של סוג הבדיקה המקושר לתור, ובהיעדר קישור (תור שנקבע לפני שהיה קטלוג סוגי בדיקה) - ברירת המחדל של המחלקה. אפשר גם לבקש תור מסוים של אותו מטופל, עם `?appointment_id=`: מוחזר רק אם הוא של המטופל הזה ומצבו `Scheduled` - לעולם לא תור של מטופל אחר, ואם לא - `found=false`, בדיוק כמו מטופל בלי תורים. התשובה כוללת גם `upcoming_count` - מספר התורים המתוכננים והעתידיים של המטופל, בלי קשר לפרמטר:

```bash
curl -i -H "X-API-Key: local-development-api-key" \
  "http://localhost:8080/api/v1/patients/P-10041/appointment?appointment_id=APT-8391"
```

## שליפת הוראת הכנה

```bash
curl -i -H "X-API-Key: local-development-api-key" \
  "http://localhost:8080/api/v1/instructions/INSTR-CARD-ECHO?version=1"
```

תוצאה צפויה: `200` עם `{source_id, version, title, text}`. מזהה או גרסה שאינם קיימים בקטלוג (`app/catalog.py`): `404` עם `{"error": "instruction_not_found"}` בלבד (בלי `message`). הקריאה אינה מקבלת ואינה בודקת שום פרט על המטופל (עיצוב §11) - אותו מפתח API כמו שאר ה-API, ונרשמת ב-`appointment_audit_logs` עם `operation=GetInstruction` ו-`patient_id` ריק (העמודה עצמה אינה מאפשרת `NULL`).

## רשימת התורים של מטופל

```bash
curl -i \
  -H "X-API-Key: local-development-api-key" \
  "http://localhost:8080/api/v1/patients/P-10041/appointments?from=2026-10-01T00:00:00%2B03:00&to=2027-01-01T00:00:00%2B02:00"
```

תוצאה צפויה: `200` עם `appointments` (מסודר לפי `appointment_at`, מהמוקדם למאוחר, ובאותו זמן בדיוק - לפי `appointment_id`) ו-`truncated`. `from` ו-`to` הם חובה, זמן ISO עם אזור זמן (בלעדיו: `400`), כאשר `from` נכלל בטווח ו-`to` לא (`from <= appointment_at < to`), והפרש שביניהם עד 366 יום (כולל). הרשימה כוללת גם תורים שבוטלו (`Cancelled`) - עובדה שהמטופל צריך לראות - ולא רק תורים מתוכננים. הרשימה מוגבלת ל-100 שורות; כשיש יותר, `truncated=true` והשורה ה-101 ואילך אינן מוחזרות.

ההשוואה לטווח נעשית אחרי המרה לשעון ישראל (השורות שמורות כשעון ישראל מקומי בלי אזור זמן, בדיוק כמו ב-`CheckAppointment`), כך ש-`from`/`to` אפשר לשלוח בכל אזור זמן (למשל UTC) והשירות ממיר אותם לפני ההשוואה. השעה שבה שעון הקיץ חוזר לשעון חורף הופכת שעה מקומית אחת בשנה לדו-משמעית; טווח שנופל בתוכה עלול לפספס תור שנקבע בה - מגבלה ידועה ומקובלת.

קודי שגיאה: `400 validation_error` - `from`/`to` חסרים, לא ניתנים לפענוח כתאריך, בלי אזור זמן, `from >= to`, הפרש מעל 366 יום, או `patient_id` שאינו תואם את התבנית הנדרשת. `401 unauthorized` - מפתח API חסר או שגוי. `404 patient_not_found` ו-`503 patient_registry_unavailable` - כמו ב-`CheckAppointment` (ראו "בדיקת המטופל מול מרשם המטופלים"). `503 service_unavailable` - תקלת מסד נתונים. `504 timeout` - הדמיית timeout (ראו "הדמיית timeout"). ל-`appointment_audit_logs` (`operation=ListAppointments`) נרשמות רק התוצאות שקיבלו החלטה על המטופל או על התור: `200` (`found`/`not_found`), `404`, `503 patient_registry_unavailable` ו-`504` (שתיהן `technical_failure`); `400`, `401` ו-`503 service_unavailable` אינם נרשמים.

## בדיקת המטופל מול מרשם המטופלים

כאשר `PATIENT_REGISTRY_URL` מוגדר (ברירת המחדל ב-`compose.yaml`), כל מזהה מטופל נבדק תחילה מול טבלת `patients` של Hospital Agent, בקריאה בלבד דרך התפקיד `hospital_reader`:

- מטופל שאינו רשום במרשם: `404` עם `error=patient_not_found`.
- מרשם שאינו זמין: `503` עם `error=patient_registry_unavailable`. השירות נכשל באופן סגור ואינו מחזיר תור למטופל שלא אומת.
- מטופל רשום ללא תור: `200` עם `found=false`. זהו מצב עסקי תקין ולא כשל טכני.

```bash
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-30000/appointment
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-99999/appointment
```

תוצאה צפויה: `P-30000` מחזיר `200` עם `found=false`, ו-`P-99999` מחזיר `404` עם `patient_not_found`.

המרשם נמצא ב-PostgreSQL של Hospital Agent (`127.0.0.1:54322`, מסד `hospital`), ולכן Hospital Agent צריך לרוץ. מתוך ה-container הכתובת היא `host.docker.internal`, וזה עובד ב-Docker Desktop. ב-Linux הכתובת מתורגמת לכתובת ה-bridge, ש-PostgreSQL אינו מאזין לה כל עוד הפורט מפורסם על `127.0.0.1` בלבד, ולכן כל בקשה תקבל `503`; נדרש לפרסם את הפורט גם על כתובת שה-bridge מגיע אליה (למשל `172.17.0.1:54322`) או לחבר את שני הפרויקטים לרשת Docker משותפת.

כאשר `PATIENT_REGISTRY_URL` אינו מוגדר או ריק (הרצה ללא compose), הבדיקה כבויה והשירות מתנהג כמו קודם: `P-99999` מחזיר `200` עם `found=false`. ב-compose אין לכבות אותה דרך `.env`, כי ערך ריק מוחלף בברירת המחדל. ביומן ההפעלה מופיעה השורה `patient registry check: enabled` או `disabled`.

## קביעת וביטול תורים בממשק

מנהל מחובר קובע תור מטופס "קביעת תור חדש" בדף הראשי: מטופל, מחלקה, תאריך ושעה (חובה), רופא ומיקום (לא חובה). המחלקות, הרופאים של כל מחלקה והמיקומים שלה מוגדרים בקטלוג קבוע, `app/catalog.py`, ונבחרים מרשימות. בבחירת מחלקה, רשימות הרופא והמיקום מציגות רק את שלה. התאריך נבחר בשלוש רשימות: יום, חודש (מספר ושם עברי) ושנה (השנה הנוכחית ושתיים אחריה). תאריך שאינו קיים, למשל 31/02, נדחה. השעה נבחרת מרשימה בשעון 24 שעות, במשבצות של 15 דקות בין 08:00 ל-17:45. השרת מקבל רק ערכים מהקטלוג: רופא או מיקום של מחלקה אחרת, או שעה מחוץ למשבצות, נדחים. כדי להוסיף מחלקה, רופא או מיקום, עורכים את `app/catalog.py` ובונים מחדש את ה-image. המטופל נבחר מרשימה נפתחת שנטענת ממרשם המטופלים (מזהה ושם, ללא טלפון), ושמו מוצג גם ליד המזהה בטבלת התורים. כשהמרשם אינו זמין או אינו מוגדר, הדף עדיין נפתח ומציג את התורים, אבל הרשימה והכפתור כבויים. הרשימה היא נוחות בלבד: השרת בודק את המטופל מול המרשם בכל קביעה מחדש. המועד הוא שעון ישראל וחייב להיות בעתיד. לפני השמירה המטופל נבדק מול מרשם המטופלים, והקביעה נכשלת באופן סגור:

- מטופל שאינו רשום במרשם: התור לא נקבע (`400`).
- מרשם שאינו זמין, או `PATIENT_REGISTRY_URL` שאינו מוגדר כלל: התור לא נקבע (`503`), כי אי אפשר לאמת את המטופל.

תור חדש מקבל מזהה `APT-XXXXXX` ומצב `Scheduled`, ולכן `CheckAppointment` מחזיר אותו מיד. בכל שורה מתוכננת יש קישור "עריכה", שפותח את אותו טופס כשהוא מלא בפרטי התור. אפשר לשנות מחלקה, רופא, מיקום, תאריך ושעה, באותם כללים של קביעה, והתור נשמר במקום, עם אותו מזהה. המטופל אינו משתנה בעריכה: העברת תור למטופל אחר היא קביעה חדשה. תור מבוטל אינו ניתן לעריכה. יש גם כפתור "ביטול תור", שמשנה את המצב ל-`Cancelled`. דבר אינו נמחק, ותור מבוטל אינו מוחזר עוד מה-API. כל קביעה, ניסיון קביעה שנדחה, עריכה וביטול נרשמים ב-`appointment_audit_logs` (`operation` = `BookAppointment` / `UpdateAppointment` / `CancelAppointment`, `case_id` = `ui:<שם המשתמש>`).

הקביעה והביטול זמינים בממשק בלבד, ואין להם נקודת קצה ב-API (הם אינם מופיעים ב-Swagger). הטפסים מוגנים באסימון CSRF, וכאשר `UI_AUTH_ENABLED=true` הם דורשים התחברות.

לכל תור אפשר לסמן את המסמכים הנדרשים לו מתוך קטלוג קבוע של חמישה סוגים (`CBC`, `COAGULATION_TESTS`, `ECG`, `URINALYSIS`, `PREOP_SUMMARY`). הבחירה נשמרת עם התור בטבלה `appointment_required_documents`, והיא לא נגזרת מהרופא או מהמחלקה. בעריכה מוצגת הבחירה השמורה, ושמירה מחליפה אותה. `CheckAppointment` מחזיר אותה בשדה `required_documents`, רשימה ממוינת, וריקה כשאין דרישות. תורים שנקבעו לפני השינוי מקבלים רשימה ריקה. כדי לראות דרישות, עורכים את התור ומסמנים אותן (בדמו: APT-8391 של P-10041, עם CBC, COAGULATION_TESTS ו-ECG).

## סוגי בדיקה והוראות הכנה

לכל תור יש סוג בדיקה (`app/catalog.py`, קטלוג קבוע של 13 סוגים בחמש המחלקות): רשימה נפתחת בטופס הקביעה, חובה, המסוננת לפי המחלקה הנבחרת (כמו הרופא והמיקום), ובחירתה מסמנת מראש - בצד הלקוח בלבד, ניתן לשנות - את המסמכים הנדרשים שהיא מציעה. השרת בודק שסוג הבדיקה שנשלח שייך למחלקה שנבחרה, ודוחה את הטופס (`400`) אם לא. הבחירה נשמרת בטבלה `appointment_exam_types` (שורה אחת לכל תור) ומוצגת בעמודת "סוג בדיקה" בטבלת התורים ובעריכה. כל סוג בדיקה נושא גם הוראת הכנה משלו (`instruction_id`, `instruction_version`, כותרת וטקסט בעברית, טיוטת דמו הטעונה אישור רפואי) - הטבלה `exam_types` מתעדכנת מהקטלוג בכל הפעלה, בדיוק כמו `document_types`. תור שנקבע לפני השינוי (ואין לו שורה ב-`appointment_exam_types`) מוצג ונקרא כברירת המחדל של המחלקה שלו (סוג ה-"…_VISIT" שלה); שני תורי הדמו קיבלו סוג בדיקה פעם אחת בהפעלה הראשונה לאחר השינוי: APT-8391 ← ביקור במרפאה נוירולוגית, APT-8392 ← מבחן מאמץ.

## הדמיית timeout

כאשר `ENABLE_FAILURE_SIMULATION=true`, הקריאה למזהה `P-TIMEOUT` מחזירה `504` ואינה מומרת ל-`found=false`:

```bash
curl -i -H "X-API-Key: local-development-api-key" http://localhost:8080/api/v1/patients/P-TIMEOUT/appointment
```

בסביבת Production יש להגדיר `ENABLE_FAILURE_SIMULATION=false`.

## Trace ו-Audit

הכותרות `X-Case-ID` ו-`X-Execution-ID` מתקבלות מה-Tool Executor. אם הן חסרות, השירות מייצר UUID ומחזיר אותו בכותרות התגובה. כל חיפוש נרשם בטבלה `appointment_audit_logs` וגם כ-JSON ב-stdout, עם התוצאה וזמן התגובה בלבד.

## אחסון הנתונים

ה-API ומסד SQLite רצים באותו container. קובץ הנתונים נשמר ב-`/data/appointments.db`, שמחובר ל-Docker volume בשם `appointment_sqlite_data`. לכן עדכון או יצירה מחדש של ה-container אינם מוחקים את הנתונים. אין container נוסף, והתורים עצמם אינם דורשים PostgreSQL או RDS. בדיקת המטופלים (ראו לעיל) קוראת את מרשם המטופלים מה-PostgreSQL של Hospital Agent, בקריאה בלבד, ואינה כותבת אליו דבר.

## בדיקות

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

## עצירה וניקוי

```bash
docker compose down
```

למחיקת נתוני SQLite המקומיים בלבד:

```bash
docker compose down -v
```
