"""Choose and pin a real signing identity; never relax code requirements or TCC."""
import json
from pathlib import Path
import re
import subprocess
import time


def sign(*arguments):
    command = ["codesign", *map(str, arguments)]
    for attempt in range(3):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return
        timestamp_error = any(message in result.stderr.lower() for message in (
            "timestamp service is not available",
            "a timestamp was expected but was not found",
        ))
        if timestamp_error and attempt < 2:
            print("Apple timestamp service unavailable; retrying signing.", flush=True)
            time.sleep(attempt + 1)
            continue
        raise RuntimeError(result.stderr.strip() or "Code signing failed.")


def available_identities():
    result = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                            check=True, capture_output=True, text=True)
    return dict(re.findall(r'\b([0-9A-Fa-f]{40})\s+"([^"\n]+)"', result.stdout))


def select_identity(requested, identities, saved=None, require_stable=False):
    if requested == "-":
        if require_stable:
            raise ValueError("This build requires a certificate-backed signing identity.")
        return "-"
    choice = requested or saved
    if choice:
        matches = [fingerprint for fingerprint, name in identities.items()
                   if choice.upper() == fingerprint.upper() or choice == name]
        if len(matches) != 1:
            raise ValueError("The configured signing identity is missing or ambiguous. "
                             "Install/unlock its certificate and private key, or select --identity explicitly.")
        return matches[0]
    # Only auto-select Apple application certificates; other identities must be explicit.
    eligible = [fingerprint for fingerprint, name in identities.items()
                if name.startswith(("Apple Development:", "Mac Developer:", "Developer ID Application:"))]
    if len(eligible) > 1:
        raise ValueError("Multiple signing identities found. Choose one with --identity; it will be pinned for later builds.")
    if eligible:
        return eligible[0]
    if require_stable:
        raise ValueError("No stable signing identity found. Install an Apple Development or Developer ID Application certificate with its private key.")
    return "-"


def resolve_identity(requested, build: Path, require_stable=False):
    pin = build / "signing-identity.json"
    saved = json.loads(pin.read_text())["identity"] if pin.exists() else None
    identities = available_identities()
    identity = select_identity(requested, identities, saved, require_stable)
    if identity == "-":
        print("WARNING: ad-hoc signing changes the app's code identity on rebuild. "
              "macOS may require Keychain/microphone/control permissions again. "
              "Install a stable signing certificate to retain identity across builds.", flush=True)
    else:
        print("Signing with: " + identities[identity], flush=True)
    return identity


def pin_identity(identity, build: Path):
    # Only pin after a successful build. An explicit ad-hoc CI build never erases a pin.
    if identity != "-":
        (build / "signing-identity.json").write_text(json.dumps({"identity": identity}) + "\n")
