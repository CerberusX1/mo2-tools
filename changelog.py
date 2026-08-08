#!/usr/bin/env python3
import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from datetime import datetime
import xml.etree.ElementTree as ET
import requests
import re
import hashlib
import os

# ------------------------------------------------------------
# Config handling
# ------------------------------------------------------------

def load_config(config_path):
    if not Path(config_path).exists():
        print(f"ERROR: Config file not found: {config_path}")
        print("Run this script with --setup to create it.")
        sys.exit(2)

    tree = ET.parse(config_path)
    root = tree.getroot()

    def get(path):
        el = root.find(path)
        return el.text if el is not None else None

    return {
        "mo2Root": get("mo2Root"),
        "outputDir": get("changelog/outputDir"),
        "collectionSlug": get("changelog/collectionSlug"),
    }


def write_config(config_path, mo2_root, output_dir, collection_slug):
    root = ET.Element("config")

    mo2_el = ET.SubElement(root, "mo2Root")
    mo2_el.text = mo2_root

    changelog_el = ET.SubElement(root, "changelog")

    out_el = ET.SubElement(changelog_el, "outputDir")
    out_el.text = output_dir

    slug_el = ET.SubElement(changelog_el, "collectionSlug")
    slug_el.text = collection_slug

    tree = ET.ElementTree(root)
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    tree.write(config_path, encoding="utf-8", xml_declaration=True)

    print(f"\nConfig file created at: {config_path}")
    print(f"  MO2 Root: {mo2_root}")
    print(f"  Output Dir: {output_dir}")
    print(f"  Collection Slug: {collection_slug}")


def write_apikey(path, key):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key.strip(), encoding="utf-8")
    print(f"\nNexus API key saved to: {p}")


def read_apikey(path):
    try:
        return Path(path).read_text().strip()
    except Exception:
        print("ERROR: Missing or unreadable apikey.txt")
        print("Run this script with --setup to configure it.")
        sys.exit(2)


# ------------------------------------------------------------
# Database
# ------------------------------------------------------------

def db_connect():
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect("data/changelog.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            profile TEXT,
            version TEXT,
            timestamp TEXT,
            PRIMARY KEY (profile, version)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_files (
            profile TEXT,
            version TEXT,
            mod_id INTEGER,
            filename TEXT,
            file_hash TEXT,
            size INTEGER,
            PRIMARY KEY (profile, version, filename)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mod_names (
            mod_id INTEGER PRIMARY KEY,
            name TEXT,
            fetched_at TEXT
        )
    """)
    return conn


def get_last_snapshot(conn, profile):
    cur = conn.execute(
        "SELECT version FROM snapshots WHERE profile = ? ORDER BY timestamp DESC LIMIT 1",
        (profile,)
    )
    row = cur.fetchone()
    return row[0] if row else None


def save_snapshot(conn, profile, version, files):
    ts = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO snapshots (profile, version, timestamp) VALUES (?, ?, ?)",
        (profile, version, ts)
    )

    for f in files:
        conn.execute(
            "INSERT INTO snapshot_files (profile, version, mod_id, filename, file_hash, size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (profile, version, f["mod_id"], f["filename"], f["hash"], f["size"])
        )

    conn.commit()


def load_snapshot_files(conn, profile, version):
    cur = conn.execute(
        "SELECT mod_id, filename, file_hash, size FROM snapshot_files "
        "WHERE profile = ? AND version = ?",
        (profile, version)
    )
    files = {}
    for mod_id, filename, file_hash, size in cur.fetchall():
        files[filename] = {
            "mod_id": mod_id,
            "hash": file_hash,
            "size": size,
        }
    return files


# ------------------------------------------------------------
# Auto-detect compiler settings
# ------------------------------------------------------------

def detect_compiler_settings(mo2_root):
    root = Path(mo2_root)
    candidates = list(root.glob("*compiler*settings*"))

    if not candidates:
        print(f"ERROR: No compiler settings file found in {mo2_root}")
        print("This file is usually created by the modlist compiler and lives in the MO2 root.")
        sys.exit(2)

    settings_path = candidates[0]

    try:
        data = json.loads(settings_path.read_text())
    except Exception:
        print(f"ERROR: Compiler settings file is not valid JSON: {settings_path}")
        sys.exit(2)

    modlist_name = data.get("ModListName", "Modlist")
    version = data.get("Version", "Unknown")
    downloads = data.get("Downloads")

    if not downloads:
        print("ERROR: Downloads path not found in compiler settings.")
        print("The compiler settings JSON should contain a 'Downloads' entry pointing to your downloads folder.")
        sys.exit(2)

    return modlist_name, version, downloads


# ------------------------------------------------------------
# MO2 Profile Support
# ------------------------------------------------------------

def list_profiles(mo2_root):
    profiles_dir = Path(mo2_root) / "profiles"
    if not profiles_dir.exists():
        return []
    return [p.name for p in profiles_dir.iterdir() if p.is_dir()]


def load_enabled_mods(profile_path):
    modlist = profile_path / "modlist.txt"
    if not modlist.exists():
        print(f"ERROR: modlist.txt missing in profile {profile_path.name}")
        sys.exit(2)

    enabled = set()
    for line in modlist.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("+"):
            enabled.add(line[1:].strip())
    return enabled


def build_profile_mod_ids(mo2_root):
    mods_dir = Path(mo2_root) / "mods"
    profiles_dir = Path(mo2_root) / "profiles"

    folder_to_modid = {}
    for folder in mods_dir.iterdir():
        if not folder.is_dir():
            continue
        if folder.name.endswith("_separator"):
            continue
        meta = folder / "meta.ini"
        if not meta.exists():
            continue
        text = meta.read_text(errors="ignore")
        modid_match = re.search(r"modid\s*=\s*(\d+)", text)
        if modid_match:
            folder_to_modid[folder.name] = int(modid_match.group(1))

    profile_mod_ids = {}
    for profile in profiles_dir.iterdir():
        if not profile.is_dir():
            continue
        enabled = load_enabled_mods(profile)
        mod_ids = set()
        for folder_name in enabled:
            mod_id = folder_to_modid.get(folder_name)
            if mod_id is not None:
                mod_ids.add(mod_id)
        profile_mod_ids[profile.name] = mod_ids

    return profile_mod_ids


# ------------------------------------------------------------
# Downloads Scanner (patched)
# ------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_mod_id_from_filename(filename):
    base = os.path.basename(filename)

    m = re.match(r"^(\d+)\s*-\s*", base)
    if m:
        return int(m.group(1))

    m = re.match(r"^(\d+)-", base)
    if m:
        return int(m.group(1))

    parts = base.split("-")
    if parts and parts[0].isdigit():
        return int(parts[0])

    return None


def scan_downloads(downloads_dir):
    downloads_dir = Path(downloads_dir)
    if not downloads_dir.exists():
        print(f"ERROR: Downloads folder does not exist: {downloads_dir}")
        sys.exit(2)

    files = []

    for full in downloads_dir.iterdir():
        if not full.is_file():
            continue

        if not full.name.lower().endswith((".zip", ".7z", ".rar", ".fomod")):
            continue

        try:
            size = full.stat().st_size
        except OSError:
            continue

        file_hash = sha256_file(full)
        mod_id = extract_mod_id_from_filename(full.name)

        files.append({
            "filename": full.name,
            "path": full,
            "size": size,
            "hash": file_hash,
            "mod_id": mod_id,
        })

    return files


# ------------------------------------------------------------
# Name Resolution
# ------------------------------------------------------------

def resolve_mod_name(conn, api_key, domain, mod_id, filename):
    if mod_id is None:
        return filename

    cur = conn.execute(
        "SELECT name FROM mod_names WHERE mod_id = ?",
        (mod_id,)
    )
    row = cur.fetchone()
    if row:
        return row[0]

    url = f"https://api.nexusmods.com/v1/games/{domain}/mods/{mod_id}.json"
    headers = {"apikey": api_key}

    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 429 or r.status_code == 404:
            name = filename
        else:
            name = r.json().get("name", filename)
    except Exception:
        name = filename

    conn.execute(
        "INSERT INTO mod_names (mod_id, name, fetched_at) VALUES (?, ?, ?)",
        (mod_id, name, datetime.utcnow().isoformat())
    )
    conn.commit()
    return name


# ------------------------------------------------------------
# Diff Logic
# ------------------------------------------------------------

def diff_files(snapshot, current):
    added = []
    removed = []
    updated = []

    snap_names = set(snapshot.keys())
    curr_names = set(current.keys())

    for fname in curr_names - snap_names:
        added.append(fname)

    for fname in snap_names - curr_names:
        removed.append(fname)

    for fname in snap_names & curr_names:
        s = snapshot[fname]
        c = current[fname]
        if s["hash"] != c["hash"] or s["size"] != c["size"]:
            updated.append(fname)

    return added, removed, updated


# ------------------------------------------------------------
# Write changelog
# ------------------------------------------------------------

def write_changelog(path, modlist_name, profile_name, version, domain, slug,
                    added, removed, updated, current_files, snapshot_files,
                    conn, api_key):
    base = modlist_name.lower().replace(" ", "")
    profile_lower = profile_name.lower().replace(" ", "")

    if profile_lower == base:
        filename = f"{base}_changelog.txt"
    else:
        filename = f"{base}_{profile_name}_changelog.txt"

    folder = Path(path) / profile_name
    folder.mkdir(parents=True, exist_ok=True)

    outpath = folder / filename

    lines = []
    lines.append(f"# {modlist_name} {version} — Profile: {profile_name}")
    lines.append("")
    lines.append("### Changelog:")

    for fname in sorted(added):
        info = current_files[fname]
        name = resolve_mod_name(conn, api_key, domain, info["mod_id"], fname)
        lines.append(f"- Added {name}")

    for fname in sorted(removed):
        info = snapshot_files[fname]
        name = resolve_mod_name(conn, api_key, domain, info["mod_id"], fname)
        lines.append(f"- Removed {name}")

    for fname in sorted(updated):
        info = current_files[fname]
        name = resolve_mod_name(conn, api_key, domain, info["mod_id"], fname)
        lines.append(f"- Updated {name}")

    outpath.write_text("\n".join(lines), encoding="utf-8")
    print(f"Changelog written for profile {profile_name}: {outpath}")


# ------------------------------------------------------------
# Setup Wizard
# ------------------------------------------------------------

def run_setup_wizard(config_path, apikey_path):
    print("=== Changelog Setup Wizard ===\n")

    print("Step 1: MO2 Root Directory")
    print("This is the folder where Mod Organizer 2 for your modlist lives.")
    print("Example: E:/Modlists/Arcadia Sands/MO2")
    mo2_root = input("Enter MO2 root path: ").strip()
    if not Path(mo2_root).exists():
        print("ERROR: That directory does not exist.")
        sys.exit(2)

    print("\nStep 2: Output Directory for Changelogs")
    print("This is where the generated changelog .txt files will be written.")
    print("Example: E:/Modlists/Arcadia Sands/Changelogs")
    output_dir = input("Enter output directory path: ").strip()
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    print(f"Output directory verified/created: {out_path}")

    print("\nStep 3: Collection Slug")
    print("This is the Nexus collection slug for your modlist, used for naming and linking.")
    print("Example: arcadia-sands")
    collection_slug = input("Enter collection slug: ").strip()
    if not collection_slug:
        print("ERROR: Collection slug cannot be empty.")
        sys.exit(2)

    print("\nStep 4: Nexus API Key")
    print("This is your personal Nexus Mods API key, used to resolve mod names.")
    print("You can find it at: Nexus Mods -> User Settings -> API Key.")
    api_key = input("Enter Nexus API key: ").strip()
    if not api_key:
        print("ERROR: API key cannot be empty.")
        sys.exit(2)

    write_config(config_path, mo2_root, output_dir, collection_slug)
    write_apikey(apikey_path, api_key)

    print("\nSetup wizard complete. Now creating initial snapshots...\n")

    return mo2_root


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config_path = "config.xml"
    apikey_path = "collection/common/apikey.txt"

    if args.setup:
        mo2_root_str = run_setup_wizard(config_path, apikey_path)
        config = load_config(config_path)
        api_key = read_apikey(apikey_path)
    else:
        config = load_config(config_path)
        api_key = read_apikey(apikey_path)
        mo2_root_str = config["mo2Root"]

    conn = db_connect()

    mo2_root = Path(mo2_root_str)
    modlist_name, version, downloads_dir = detect_compiler_settings(mo2_root)

    profiles = list_profiles(mo2_root)
    if not profiles:
        print("ERROR: No MO2 profiles found.")
        sys.exit(2)

    profile_mod_ids = build_profile_mod_ids(mo2_root)

    all_files = scan_downloads(downloads_dir)

    if args.setup:
        print(f"Detected Modlist: {modlist_name}")
        print(f"Detected Version: {version}")
        confirm = input("Create initial snapshots for ALL profiles (downloads-based)? (y/n): ").lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(2)

        for profile in profiles:
            mod_ids = profile_mod_ids.get(profile, set())
            profile_files = []
            for f in all_files:
                if f["mod_id"] is not None and mod_ids and f["mod_id"] not in mod_ids:
                    continue
                profile_files.append(f)

            save_snapshot(conn, profile, version, profile_files)
            print(f"Snapshot saved for profile: {profile}")

        print("All profile snapshots created.")
        return

    domain = "fallout4"

    current_files_map = {}
    for f in all_files:
        current_files_map[f["filename"]] = {
            "mod_id": f["mod_id"],
            "hash": f["hash"],
            "size": f["size"],
        }

    for profile in profiles:
        last_version = get_last_snapshot(conn, profile)
        if not last_version:
            print(f"ERROR: No snapshot found for profile '{profile}'. Run --setup first.")
            continue

        snapshot_files = load_snapshot_files(conn, profile, last_version)

        mod_ids = profile_mod_ids.get(profile, set())
        current_filtered = {}
        for fname, info in current_files_map.items():
            if info["mod_id"] is not None and mod_ids and info["mod_id"] not in mod_ids:
                continue
            current_filtered[fname] = info

        added, removed, updated = diff_files(snapshot_files, current_filtered)

        write_changelog(
            config["outputDir"],
            modlist_name,
            profile,
            version,
            domain,
            config["collectionSlug"],
            added,
            removed,
            updated,
            current_filtered,
            snapshot_files,
            conn,
            api_key
        )


if __name__ == "__main__":
    try:
        print("Starting changelog script...")
        main()
        print("Script finished successfully.")
    except Exception as e:
        import traceback
        print("\n\n=== FATAL ERROR ===")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {e}")
        print("\n--- TRACEBACK ---")
        traceback.print_exc()
        print("\n===================\n")
        input("Press Enter to exit...")
