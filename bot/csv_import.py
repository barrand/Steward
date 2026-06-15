"""CSV bootstrap importer for Stew member data.

Usage:
  python csv_import.py import-csv /path/to/Elders.csv
  python csv_import.py import-csv /path/to/Elders.csv --dry-run
"""

import argparse
import csv
import sys
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import firestore_client as fsc

MT = ZoneInfo("America/Denver")


def parse_name(full_name: str) -> Tuple[str, bool]:
    """Parse 'Last, First [Middle]' into 'First [Middle] Last'.

    Returns (parsed_name, is_inactive).
    If name ends with 'Out-of-Unit', strips it and marks as inactive.
    """
    is_inactive = False

    if full_name.endswith("Out-of-Unit"):
        is_inactive = True
        full_name = full_name.replace("Out-of-Unit", "").strip()

    # Split by comma: "Last, First Middle"
    if "," in full_name:
        parts = full_name.split(",", 1)
        last = parts[0].strip()
        first_middle = parts[1].strip()
        return f"{first_middle} {last}", is_inactive

    # No comma — return as-is (shouldn't happen in this CSV)
    return full_name, is_inactive


def parse_birth_date(date_str: str) -> Optional[str]:
    """Parse 'DD Mon YYYY' to 'YYYY-MM-DD'.

    Returns None if parsing fails.
    """
    if not date_str or not date_str.strip():
        return None

    try:
        dt = datetime.strptime(date_str.strip(), "%d %b %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        print(f"  ⚠️  Could not parse birth date '{date_str}'", file=sys.stderr)
        return None


def parse_phone(phone_str: str) -> Optional[str]:
    """Parse phone number, remove parens and dashes as-is.

    Returns None if empty.
    """
    if not phone_str or not phone_str.strip():
        return None

    # Strip whitespace, return as-is (keep formatting from CSV)
    return phone_str.strip()


def import_csv(csv_path: str, dry_run: bool = False) -> None:
    """Import members from CSV file.

    Idempotent: if a member with the same name exists, updates fields.
    """
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "Name" not in reader.fieldnames:
                print("❌ CSV must have 'Name' column", file=sys.stderr)
                return

            rows = list(reader)
    except FileNotFoundError:
        print(f"❌ File not found: {csv_path}", file=sys.stderr)
        return
    except Exception as e:
        print(f"❌ Error reading CSV: {e}", file=sys.stderr)
        return

    db = fsc.get_db()

    # Fetch existing members for idempotency check
    existing_members = {}
    try:
        docs = db.collection("members").stream()
        for doc in docs:
            data = doc.to_dict() or {}
            if data.get("name"):
                existing_members[data["name"]] = doc.id
    except Exception as e:
        print(f"⚠️  Could not fetch existing members: {e}", file=sys.stderr)

    imported_count = 0
    updated_count = 0
    inactive_count = 0
    failed_count = 0

    print(f"\n📥 Importing {len(rows)} members from {csv_path}")
    if dry_run:
        print("   (DRY RUN — no changes will be written)")
    print()

    for i, row in enumerate(rows, 1):
        name_raw = row.get("Name", "").strip()
        if not name_raw:
            print(f"  Row {i}: ⚠️  Empty name, skipping")
            continue

        # Parse fields
        name, is_inactive = parse_name(name_raw)
        birthday = parse_birth_date(row.get("Birth Date", ""))
        phone = parse_phone(row.get("Phone Number", ""))

        status = "inactive" if is_inactive else "active"

        # Build member data
        member_data = {
            "name": name,
            "status": status,
            "created_at": datetime.now(MT).isoformat(),
        }
        if birthday:
            member_data["birthday"] = birthday
        if phone:
            member_data["phone"] = phone

        # Check if member already exists (idempotency)
        if name in existing_members:
            member_id = existing_members[name]
            if not dry_run:
                try:
                    db.collection("members").document(member_id).update(member_data)
                    updated_count += 1
                    print(f"  Row {i}: ✏️  Updated {name}")
                except Exception as e:
                    print(f"  Row {i}: ❌ Error updating {name}: {e}")
                    failed_count += 1
            else:
                print(f"  Row {i}: ✏️  [DRY RUN] Would update {name}")
                updated_count += 1
        else:
            # New member
            if not dry_run:
                try:
                    doc_ref = db.collection("members").document()
                    doc_ref.set(member_data)
                    imported_count += 1
                    print(f"  Row {i}: ✅ Created {name}")
                except Exception as e:
                    print(f"  Row {i}: ❌ Error creating {name}: {e}")
                    failed_count += 1
            else:
                print(f"  Row {i}: ✅ [DRY RUN] Would create {name}")
                imported_count += 1

        if is_inactive:
            inactive_count += 1

    print()
    print("📊 Import Summary:")
    print(f"  • New members: {imported_count}")
    print(f"  • Updated: {updated_count}")
    print(f"  • Inactive: {inactive_count}")
    print(f"  • Failed: {failed_count}")
    print(f"  • Total active: {imported_count + updated_count - inactive_count}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Import Stew member data from CSV")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # import-csv subcommand
    import_parser = subparsers.add_parser("import-csv", help="Import members from CSV")
    import_parser.add_argument("csv_path", help="Path to CSV file")
    import_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")

    args = parser.parse_args()

    if args.command == "import-csv":
        import_csv(args.csv_path, dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
