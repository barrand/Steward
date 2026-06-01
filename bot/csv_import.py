"""Stew CSV Bootstrap Importer

One-time member import from CSV (Phase 1, CLI-only).
Source: /Users/bbarrand/Documents/Projects/EldersQuorum/Elders.csv

CSV Format:
  Name,Age,Birth Date,Phone Number
  "Barrand, Samuel Allen",21,24 Aug 2004,(801) 755-2681
  "Bona, Samuel",19,8 May 2007,

Parsing:
  - Name: "Last, First [Middle]" → store as "First [Middle] Last"
  - Age: ignored (can infer from birth date)
  - Birth Date: "DD Mon YYYY" → "YYYY-MM-DD" (use YYYY=0000 if age but no year known)
  - Phone: "(XXX) XXX-XXXX" → "XXX-XXX-XXXX" (optional)
  - Detect "Out-of-Unit" suffix → set status="inactive", strip from name

Idempotent: if member with same name exists, update fields instead of duplicating.

Usage:
  python csv_import.py import /path/to/Elders.csv [--dry-run]
"""

# TODO: Implement CSV parsing + Firestore insert/update
# - Parse dates using dateutil or datetime
# - Detect Out-of-Unit suffix
# - Fuzzy match existing members by name
# - Print summary: "Imported 87 members (3 inactive, 84 active)"

if __name__ == "__main__":
    print("Stew CSV importer — TODO: implement")
