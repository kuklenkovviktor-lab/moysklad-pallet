"""Offline WB FBS sticker barcode vs scanner-payload contract verification.

Does not call WB API or read actual warehouse data. Synthetic mode tests the
comparator itself; actual API/printed QR equivalence requires authorized
paired samples captured outside the employer's warehouse.
"""
import argparse
import json
from pathlib import Path
import sys


def verify_pair(api_barcode, scanned_value):
    """Return exact match: never guess orderId or translate partA/partB."""
    if not isinstance(api_barcode, str) or not isinstance(scanned_value, str):
        return False
    return bool(api_barcode) and api_barcode == scanned_value


def run_synthetic():
    samples = [
        ("SYNTH-WB-ORDER-001", "SYNTH-WB-ORDER-001", True),
        ("SYNTH-WB-ORDER-001", "SYNTH-WB-ORDER-002", False),
        ("SYNTH-WB-ORDER-001", "001", False),
        ("SYNTH-WB-ORDER-001", " SYNTH-WB-ORDER-001", False),
        ("SYNTH-WB-ORDER-001", "", False),
        ("", "SYNTH-WB-ORDER-001", False),
    ]
    for expected, actual, allowed in samples:
        if verify_pair(expected, actual) != allowed:
            raise AssertionError("Synthetic scanner/API equality gate failed")
    print("WB QR SYNTHETIC CONTRACT PASS: 6 tests, no WB API calls.")
    print("REAL QR/API BARCODE EQUALITY NOT VERIFIED: authorized paired example required.")


def run_fixture(path):
    # Fixture format: {"samples":[{"api_barcode":"...","scanner_value":"..."}]}
    # Never print the raw identifiers: they may be order-specific.
    blob = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = blob.get("samples")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty samples[] array")
    for index, item in enumerate(rows, 1):
        if not isinstance(item, dict) or not verify_pair(
            item.get("api_barcode"), item.get("scanner_value")
        ):
            raise AssertionError(
                "QR mismatch/missing data in sample " + str(index) +
                ": do not enable WB integration"
            )
    print(f"WB QR PAIR CONTRACT PASS: {len(rows)} paired authorized samples.")
    print("NOTE: this checks supplied strings, not scanner hardware or image authenticity.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic", action="store_true")
    mode.add_argument("--fixture", help="Authorized local fixture JSON, NOT employer warehouse data")
    args = parser.parse_args()
    try:
        run_synthetic() if args.synthetic else run_fixture(args.fixture)
    except (ValueError, KeyError, AssertionError, OSError) as exc:
        print("FAIL: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
