"""
Clear Firestore `vehicles` collection and reload company vehicle master data.

Fields per document:
  vehicle_id, vehicle, make, description  (description = last 4 of reg no + '-' + make)

Document ID: vehicle_id (e.g. V001).

Run from project root:
    python load_vehicles.py
"""

import re

import firebase_admin
from firebase_admin import credentials, firestore

if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-adminsdk.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# (vehicle_id, vehicle registration, make, category for reference only — not stored)
VEHICLES = [
    ("V001", "TN 70 AK 4635", "OLA", "TWO WHEELER"),
    ("V002", "TN 70 AK 7346", "TVS XL", "TWO WHEELER"),
    ("V003", "TN 70 AH 1473", "TVS XL", "TWO WHEELER"),
    ("V004", "TN 70 J 9376", "WEGO", "TWO WHEELER"),
    ("V005", "TN 70 V 0993", "TVS XL", "TWO WHEELER"),
    ("V006", "TN 70 S 1248", "ACTIVA", "TWO WHEELER"),
    ("V007", "TN 70 Q 3103", "ACCESS", "TWO WHEELER"),
    ("V008", "TN 70 W 4907", "ACTIVA", "TWO WHEELER"),
    ("V009", "TN 70 T 1477", "TVS XL", "TWO WHEELER"),
    ("V010", "KA 51 AG 3271", "DOST", "FOUR WHEELER"),
    ("V011", "KA 51 AJ 2568", "DOST", "FOUR WHEELER"),
    ("V012", "TN 20 AQ 2004", "SANTRO", "CAR"),
    ("V013", "TN 70 E 1666", "SANTA FE", "CAR"),
]


def _registration_last4(vehicle_no: str) -> str:
    """Last 4 digits from registration (ignores spaces/letters)."""
    digits = re.sub(r"\D", "", (vehicle_no or "").upper())
    if len(digits) >= 4:
        return digits[-4:]
    compact = re.sub(r"\s+", "", (vehicle_no or "").upper())
    return compact[-4:] if len(compact) >= 4 else compact or "0000"


def vehicle_description(vehicle_no: str, make: str) -> str:
    return f"{_registration_last4(vehicle_no)}-{make.strip()}"


def delete_all_vehicles():
    coll = db.collection("vehicles")
    deleted = 0
    while True:
        docs = list(coll.limit(500).stream())
        if not docs:
            break
        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
            deleted += 1
        batch.commit()
    return deleted


def load_vehicles():
    coll = db.collection("vehicles")
    batch = db.batch()
    batch_count = 0
    loaded = 0
    seen_ids = set()

    for vehicle_id, vehicle_no, make, _category in VEHICLES:
        vehicle_id = vehicle_id.strip().upper()
        vehicle_no = " ".join(vehicle_no.split()).upper()
        make = make.strip().upper()

        if vehicle_id in seen_ids:
            print(f"WARNING: duplicate vehicle_id -> skipping {vehicle_id}")
            continue
        seen_ids.add(vehicle_id)

        batch.set(
            coll.document(vehicle_id),
            {
                "vehicle_id": vehicle_id,
                "vehicle": vehicle_no,
                "make": make,
                "description": vehicle_description(vehicle_no, make),
                "active": True,
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
    print("Clearing vehicles collection...")
    removed = delete_all_vehicles()
    print(f"Removed {removed} document(s).\n")

    print(f"Loading {len(VEHICLES)} vehicles...")
    added = load_vehicles()
    print(f"Added {added} vehicle document(s).")
    print("\nSample descriptions:")
    for vehicle_id, vehicle_no, make, _ in VEHICLES[:5]:
        print(f"  {vehicle_id}: {vehicle_description(vehicle_no, make)}")
    print("Done.")
