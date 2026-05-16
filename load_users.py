"""
Clear Firestore `users` collection and reload from employee master data.

Manager phone blank -> reports to MD (7538866308).
Document ID: whatsapp:+91<employee_mobile>

Run from project root:
    python load_users.py
"""

import firebase_admin
from firebase_admin import credentials, firestore

# ---------------- FIREBASE ---------------- #

if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-adminsdk.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# MD mobile (10 digits) when manager column is empty
MD_MOBILE = "7538866308"

# (employee_id, name, department, employee_mobile, manager_mobile or "")
EMPLOYEES = [
    ("RAG267", "AGILAN C", "STORE", "7397743346", ""),
    ("SRI080", "AJAY SENTHILKUMAR", "IT", "9498061569", "7397743357"),
    ("SRI067", "AKILA B", "ERP", "6369728200", "6385652906"),
    ("ADC343", "ANBARASAN S", "DESIGN", "9629738177", "9626044814"),
    ("ADC278", "ARUL MURUGAN", "NPD", "8680065838", "7397743344"),
    ("ADC020", "ARUMUGAM", "TOOL ROOM", "9965606030", "7397743351"),
    ("RAG121", "ARUN", "PDC", "9942725748", "7397743354"),
    ("ADC239", "ARUN SELVAM", "LOGISTICS", "9788445561", "7397743355"),
    ("RAG053", "BALAJI", "R/E", "9789309376", "7397743354"),
    ("RAG238", "CHANDRAN", "FETTLING", "6369256670", "7397743362"),
    ("ADC043", "DHANAPACKIAM", "ACCOUNTS", "8220767111", "7397743347"),
    ("RAG052", "DHANUSKODI V", "FINAL", "8925666727", "8072945491"),
    ("SRI081", "DINESH S", "IT", "9994246682", "7397743357"),
    ("ADC149", "DURAI RASU M", "TOOL ROOM", "9708730901", "7397743351"),
    ("RAG054", "ESWARAN", "CNC", "7397743364", ""),
    ("SRI009", "GOKILA R", "ERP", "6385652906", ""),
    ("SRI018", "GOKUL SINGH V", "PPC", "8778682397", "7397743357"),
    ("SRI005", "GOMATHI P", "FINAL-QUALITY", "8925242830", "8072945491"),
    ("ADC280", "GOPINATH", "PPC", "7397743355", ""),
    ("ADC351", "GOWTHAM K P", "TOOL ROOM", "8884168748", "7397743351"),
    ("ADC290", "INDHUMATHI POUNRAJ", "HR", "8072554537", "9626323752"),
    ("RAG215", "INDHUMATHI V", "FINAL", "9597263334", "8072945491"),
    ("RAG264", "JAGATHEESKUMAR P", "ACCOUNTS", "9842029974", "7397743347"),
    ("ADC354", "JEEVANANDAM T", "IT", "7397743357", ""),
    ("ADC128", "JOHNPETER", "FABRICATION", "9566775243", ""),
    ("RAG216", "KALA S", "REWORK", "9092114173", "8072945491"),
    ("SRI082", "KALAIVANNAN S", "MAINTENANCE", "6369799987", "7397743350"),
    ("ADC098", "KALANDAR BASHA A", "NPD", "7397743344", ""),
    ("RAG250", "KALIRAJ", "R/E", "9791832236", "9789309376"),
    ("ADC093", "KANDAN S", "MAINTENANCE", "7094468816", ""),
    ("RAG263", "KARTHIK A", "ACCOUNTS", "9345981043", "7397743347"),
    ("RAG218", "KAVITHA S", "FETTLING", "9626464949", "7397743362"),
    ("RAG230", "KOHILA", "FINAL", "9952656396", "8072945491"),
    ("SRI059", "LAVANYA RAJA", "ERP", "9597114055", "6385652906"),
    ("RAG136", "LOGANATHAN K", "CNC", "8667383727", "7397743354"),
    ("ADC004", "LOGANATHAN M", "PDC", "7397743354", ""),
    ("SRI084", "MADHUBALA J", "ERP", "8072320105", "6385652906"),
    ("ADC340", "MAHADESH KRISHNAPPA", "ACCOUNTS", "7397743347", ""),
    ("ADC036", "MAHENDHIRAN M", "MAINTENANCE", "7397743350", ""),
    ("RAG266", "MANGALAPARAMESWARI", "STORE", "9025152768", "7397743346"),
    ("ADC015", "MANGUNDU M", "DISPATCH", "9787366677", "7397743355"),
    ("SRI079", "MANIKANDAN CHELLAKARUPPIAH", "IT", "7339221730", "7397743357"),
    ("RAG241", "MANIVANNAN M", "CNC", "9994865855", ""),
    ("RAG268", "MANJUNATH M", "FETTLING", "7395887928", "7397743363"),
    ("RAG251", "MARI", "FINAL", "9524327231", "8072945491"),
    ("RAG249", "MEENA", "FINAL", "9626323752", "8072945491"),
    ("ADC048", "MEENA V", "HR", "9751045688", ""),
    ("ADC087", "MUNIRAI K", "SECONDARY", "9791529686", "7094468814"),
    ("RAG209", "MUNIRAJ N", "FETTLING", "7397743362", ""),
    ("ADC014", "MUNUSAMY M", "TOOL ROOM", "7397743351", ""),
    ("ADC006", "MURALI M", "PDC", "9578986083", "8838531245"),
    ("ADC245", "MURUGAN C", "DESIGN", "9626044814", ""),
    ("ADC012", "MURUGESAN", "MAINTENANCE", "7397743349", ""),
    ("ADC291", "MUTHUKUMAR SUBRAMANIYAN", "NPD", "8667295513", "7397743344"),
    ("RAG051", "NAGAMMAL R", "SECONDARY", "8754291228", "7094468814"),
    ("RAG210", "NAGARAJ V", "FETTLING", "7397743363", ""),
    ("ADC336", "NANDHAKISHOR", "CNC", "8838562830", "7397743354"),
    ("SRI063", "NANDHINI M", "ERP", "9597357332", "6385652906"),
    ("RAG059", "PACHIAPPAN", "FINAL", "8072945491", ""),
    ("RAG244", "PADMA", "FINAL", "9047157207", "8072945491"),
    ("ADC324", "PANDIARAJAN S", "LOGISTICS", "8870009466", "7397743355"),
    ("RAG237", "PRABAKARAN M", "PDC", "8838531245", ""),
    ("ADC189", "PRAGASAM P", "PDC", "9789630110", "7397743354"),
    ("RAG259", "RAJA A", "FETTLING-QUALITY", "7010376210", "7397743363"),
    ("RAG145", "RAJESH J P", "R/E", "8098760744", "9789309376"),
    ("RAG129", "RAJIV V", "PDC", "8344444837", "7397743354"),
    ("ADC323", "RAMACHANDRIAH", "TOOL ROOM", "7397743352", "7397743351"),
    ("SRI043", "RAVICHANDIRAN ONGALI", "ASSEMBLY", "8015776426", "7094468814"),
    ("ADC027", "RUTHRESH S", "TOOL ROOM", "9688953662", "7397743354"),
    ("RAG061", "SAFI B", "CNC", "9629276085", "7397743354"),
    ("ADC352", "SAMPATH R", "NPD", "9791743693", "7397743344"),
    ("RAG233", "SEKAR A", "FETTLING", "8248603055", "7397743354"),
    ("ADC021", "SELVARAJ M", "SHOT BLASTING", "7397743365", "9342259466"),
    ("SRI028", "SHAGUL A", "CNC", "8825837715", "7397743354"),
    ("SRI085", "SHEIK MUKSENA N", "NPD", "8072466081", "7397743344"),
    ("RAG132", "SINGARAVELU", "PDC", "7871887761", "8838531245"),
    ("ADC009", "SIVAKUMAR K", "MOULD", "7904337320", "8838531245"),
    ("ADC005", "SIVAKUMAR P", "MAINTENANCE", "7397743348", ""),
    ("RAG049", "SIVAN K", "PDC", "7402294172", "7397743354"),
    ("RAG239", "SIVAPRAKASAM", "PDC", "8825583278", "8838531245"),
    ("RAG243", "SULINA", "FINAL", "9677811718", "8072945491"),
    ("RAG235", "SURESH", "SECONDARY", "9894559476", "7094468814"),
    ("SRI077", "THEJASRI S", "ERP", "8248821177", "6385652906"),
    ("SRI078", "THILAGAVATHI K", "STORE", "8148749960", "7397743354"),
    ("RAG242", "UDAYA KUMAR M", "SECONDARY", "7094468814", ""),
    ("ADC288", "UDAYAKUMAR A R", "PPC", "9342259466", ""),
    ("ADC024", "VADIVELAN L", "TOOL ROOM", "7094468812", "7397743351"),
    ("RAG050", "VALLI", "FETTLING", "7603880476", "7397743362"),
    ("RAG222", "VEDIYAPPAN M", "CNC", "7502223442", "7397743364"),
    ("RAG240", "VELAYUTHAM M", "CNC", "9600852720", ""),
    ("RAG224", "VELU E", "FETTLING", "9751771266", "7397743354"),
    ("SRI044", "VIGNESH", "ASSEMBLY", "8610014031", "7094468814"),
    ("RAG236", "VIJAY P", "PDC", "6374665709", "8838531245"),
    ("RAG217", "VIMALA H", "FINAL", "9650671894", "8072945491"),
    ("RAG256", "VINOTH MURUGAN", "CNC", "9345561034", "9629276085"),
]


def _digits_only(value: str) -> str:
    return "".join(c for c in str(value).strip() if c.isdigit())


def _to_whatsapp(mobile: str) -> str:
    digits = _digits_only(mobile)
    if len(digits) == 10:
        return f"whatsapp:+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"whatsapp:+{digits}"
    raise ValueError(f"Invalid mobile number: {mobile!r} -> {digits!r}")


def _resolve_manager(manager_mobile: str) -> str:
    mgr = _digits_only(manager_mobile)
    if not mgr:
        return _to_whatsapp(MD_MOBILE)
    return _to_whatsapp(mgr)


def delete_all_users() -> int:
    users_ref = db.collection("users")
    deleted = 0
    while True:
        docs = list(users_ref.limit(500).stream())
        if not docs:
            break
        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        deleted += len(docs)
        print(f"Deleted {deleted} user document(s)...")
    return deleted


def load_users() -> int:
    users_ref = db.collection("users")
    loaded = 0
    seen_doc_ids = set()
    batch = db.batch()
    batch_count = 0

    for employee_id, name, department, emp_mobile, mgr_mobile in EMPLOYEES:
        doc_id = _to_whatsapp(emp_mobile)
        if doc_id in seen_doc_ids:
            print(f"WARNING: duplicate employee mobile -> skipping {employee_id} ({name})")
            continue
        seen_doc_ids.add(doc_id)

        batch.set(
            users_ref.document(doc_id),
            {
                "name": name,
                "department": department,
                "role": "employee",
                "manager": _resolve_manager(mgr_mobile),
                "employee_id": employee_id,
            },
        )
        batch_count += 1
        loaded += 1

        if batch_count >= 500:
            batch.commit()
            batch = db.batch()
            batch_count = 0

    if batch_count:
        batch.commit()

    return loaded


if __name__ == "__main__":
    print("Clearing users collection...")
    removed = delete_all_users()
    print(f"Removed {removed} document(s).\n")

    print(f"Loading {len(EMPLOYEES)} employees (MD fallback: +91{MD_MOBILE})...")
    added = load_users()
    print(f"Added {added} user document(s).")
    print("Done.")
