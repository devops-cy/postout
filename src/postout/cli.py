#!/usr/bin/env python3
import os
import sys
import re
import stat
import json
import argparse
import getpass
import tempfile
import pwd
import grp
import subprocess
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from html.parser import HTMLParser
from pathlib import Path

from . import __version__

try:
    import readline  # noqa: F401
except ImportError:
    # Postout still works without line editing support.
    pass

SMTP_TIMEOUT_SECONDS = 30
MAX_ATTACHMENT_MB = 20
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024

# ----------------------------
# Helpers
# ----------------------------

def die(msg: str, code: int = 2) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(code)

def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)

def info(msg: str) -> None:
    print(f"[INFO] {msg}")

def strip_crlf(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", s or "").strip()

def is_reasonable_email(addr: str) -> bool:
    """Apply conservative validation to one bare email address."""
    if not addr:
        return False

    _, parsed_email = parseaddr(addr)

    if parsed_email != addr:
        return False

    if any(
        character in addr
        for character in ["\r", "\n", " ", "\t"]
    ):
        return False

    if addr.count("@") != 1:
        return False

    local, domain = addr.rsplit("@", 1)

    if not local or not domain:
        return False

    if (
        local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or domain.startswith(".")
        or domain.endswith(".")
        or ".." in domain
    ):
        return False

    return True


def parse_recipients(
    value: str,
    field_name: str,
) -> list[tuple[str, str]]:
    """Return formatted header values and bare SMTP addresses."""
    if not value:
        return []

    if "\r" in value or "\n" in value:
        die(
            f"{field_name} recipients contain a line break.",
            2,
        )

    parsed_addresses = getaddresses([value])

    if not parsed_addresses:
        die(
            f"No valid {field_name} recipients were provided.",
            2,
        )

    recipients = []

    for display_name, email_address in parsed_addresses:
        display_name = display_name.strip()
        email_address = email_address.strip()

        if not is_reasonable_email(email_address):
            invalid_value = (
                email_address
                or display_name
                or value
            )

            die(
                f"Invalid {field_name} recipient: "
                f"{invalid_value}",
                2,
            )

        if "\r" in display_name or "\n" in display_name:
            die(
                f"{field_name} recipient name contains "
                "a line break.",
                2,
            )

        header_value = (
            formataddr((display_name, email_address))
            if display_name
            else email_address
        )

        recipients.append(
            (header_value, email_address)
        )

    return recipients


def deduplicate_envelope_addresses(
    *recipient_groups: list[tuple[str, str]],
) -> list[str]:
    """Deduplicate SMTP recipients while preserving first use."""
    seen = set()
    addresses = []

    for recipient_group in recipient_groups:
        for _, email_address in recipient_group:
            key = email_address.casefold()

            if key in seen:
                continue

            seen.add(key)
            addresses.append(email_address)

    return addresses


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_body(args) -> str:
    """Resolve the message body from exactly one source.

    argparse prevents --body and --body-file from being used together.
    When neither option is supplied, piped or redirected stdin is consumed.
    An interactive terminal with no body option produces an empty body.
    """
    if args.body is not None:
        return args.body

    if args.body_file is not None:
        if args.body_file == "-":
            return sys.stdin.read()
        if not os.path.isfile(args.body_file):
            die(f"Body file not found: {args.body_file}", 2)
        return read_text_file(args.body_file)

    if not sys.stdin.isatty():
        return sys.stdin.read()

    return ""

class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []
        self._in_pre = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "br":
            self.out.append("\n")
        elif t in ["p", "div", "li"]:
            self.out.append("\n")
        elif t in ["pre", "code"]:
            self._in_pre = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ["p", "div", "ul", "ol"]:
            self.out.append("\n")
        elif t in ["pre", "code"]:
            self._in_pre = False

    def handle_data(self, data):
        if not data:
            return
        if self._in_pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))

def html_to_text(html: str) -> str:
    p = _HTMLToText()
    p.feed(html or "")
    txt = "".join(p.out)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()

def preflight_attachments(filepaths: list[str]) -> int:
    """Validate attachment files and total their sizes before reading."""
    total_bytes = 0

    for filepath in filepaths:
        attachment_path = Path(filepath)

        try:
            file_status = attachment_path.stat()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Attachment not found: {filepath}"
            )
        except OSError as exc:
            raise OSError(
                f"Cannot inspect attachment '{filepath}': {exc}"
            ) from exc

        if not stat.S_ISREG(file_status.st_mode):
            raise ValueError(
                f"Attachment is not a regular file: {filepath}"
            )

        total_bytes += file_status.st_size

        if total_bytes > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"Attachments too large: {total_bytes} bytes exceeds "
                f"the {MAX_ATTACHMENT_MB} MB limit."
            )

    return total_bytes


def attach_file(
    msg: EmailMessage,
    filepath: str,
    remaining_bytes: int,
) -> int:
    """Attach one file without reading beyond the remaining limit."""
    ctype, encoding = mimetypes.guess_type(filepath)

    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"

    maintype, subtype = ctype.split("/", 1)

    try:
        with open(filepath, "rb") as handle:
            file_status = os.fstat(handle.fileno())

            if not stat.S_ISREG(file_status.st_mode):
                raise ValueError(
                    f"Attachment is not a regular file: {filepath}"
                )

            if file_status.st_size > remaining_bytes:
                raise ValueError(
                    "Attachment sizes changed after preflight and "
                    f"now exceed the {MAX_ATTACHMENT_MB} MB limit."
                )

            # The extra byte detects a file growing while it is read,
            # without loading an unexpectedly large file into memory.
            data = handle.read(remaining_bytes + 1)

    except FileNotFoundError:
        raise FileNotFoundError(
            f"Attachment not found: {filepath}"
        )
    except OSError as exc:
        raise OSError(
            f"Cannot read attachment '{filepath}': {exc}"
        ) from exc

    if len(data) > remaining_bytes:
        raise ValueError(
            "Attachment sizes changed after preflight and "
            f"now exceed the {MAX_ATTACHMENT_MB} MB limit."
        )

    msg.add_attachment(
        data,
        maintype=maintype,
        subtype=subtype,
        filename=os.path.basename(filepath),
    )

    return len(data)


def build_from_header(display_name: str, email_addr: str) -> str:
    display_name = strip_crlf(display_name)
    if display_name:
        return formataddr((display_name, email_addr))
    return email_addr

def _get_bool(v, default: bool):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ["1", "true", "yes", "y", "on"]:
        return True
    if s in ["0", "false", "no", "n", "off"]:
        return False
    return default

def extract_profiles(document, source) -> dict:
    """Return profiles from the single supported document format."""
    if not isinstance(document, dict):
        die(
            f"Profiles file {source} must contain a JSON object.",
            2,
        )

    profiles = document.get("profiles")

    if not isinstance(profiles, dict):
        die(
            f"Profiles file {source} must contain a "
            "'profiles' object.",
            2,
        )

    return profiles


def load_profiles(path: str) -> dict:
    if not path:
        return {}

    profile_path = Path(path)

    if not profile_path.is_file():
        die(f"Profiles file not found: {path}", 2)

    try:
        st = profile_path.stat()
        mode = st.st_mode & 0o777

        if profile_path == SYSTEM_PROFILES_PATH:
            if mode & 0o007:
                warn(
                    f"System profiles are accessible by users outside "
                    f"the '{SYSTEM_GROUP}' group. Recommended: "
                    f"chmod 640 {path}"
                )

            if mode & 0o020:
                warn(
                    f"System profiles are group-writable. Recommended: "
                    f"chmod 640 {path}"
                )

            try:
                system_group = grp.getgrnam(SYSTEM_GROUP)

                if st.st_uid != 0 or st.st_gid != system_group.gr_gid:
                    warn(
                        f"Recommended ownership: "
                        f"root:{SYSTEM_GROUP} {path}"
                    )
            except KeyError:
                warn(
                    f"System group '{SYSTEM_GROUP}' does not exist."
                )
        else:
            if mode & 0o077:
                warn(
                    f"Profiles file is accessible by other users. "
                    f"Recommended: chmod 600 {path}"
                )

            if (
                hasattr(os, "geteuid")
                and st.st_uid != os.geteuid()
            ):
                warn(
                    f"Profiles file is not owned by the current user: "
                    f"{path}"
                )
    except OSError:
        pass

    try:
        with profile_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"Failed to read profiles JSON: {exc}", 2)

    return extract_profiles(document, profile_path)


def resolve_setting(cli_val, profile_val, env_val, default_val=None):
    if cli_val not in [None, ""]:
        return cli_val
    if profile_val not in [None, ""]:
        return profile_val
    if env_val not in [None, ""]:
        return env_val
    return default_val


# ----------------------------
# Profile configuration
# ----------------------------

ANSI_STYLES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "magenta": "35",
    "cyan": "36",
}


def color_enabled() -> bool:
    """Use color only in a real interactive terminal."""
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def colorize(value, *styles: str) -> str:
    """Apply ANSI styles when interactive color is enabled."""
    text = str(value)

    if not color_enabled() or not styles:
        return text

    codes = [ANSI_STYLES[style] for style in styles]
    return f"\033[{';'.join(codes)}m{text}\033[0m"


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

SYSTEM_GROUP = "postout"
SYSTEM_CONFIG_DIR = Path("/etc/postout")
SYSTEM_PROFILES_PATH = SYSTEM_CONFIG_DIR / "profiles.json"


def default_profiles_path() -> Path:
    """Return the current user's XDG profiles path."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()

    if xdg_config_home:
        config_home = Path(xdg_config_home).expanduser()

        if not config_home.is_absolute():
            die("XDG_CONFIG_HOME must be an absolute path.", 2)
    else:
        config_home = Path.home() / ".config"

    return config_home / "postout" / "profiles.json"


def profiles_override_path(explicit_path: str = ""):
    """Return an explicitly selected profiles file, if any."""
    if explicit_path:
        return Path(explicit_path).expanduser()

    environment_path = os.environ.get(
        "POSTOUT_PROFILES_FILE"
    )

    if environment_path:
        return Path(environment_path).expanduser()

    return None


def system_access_hint(path: Path) -> str:
    """Return group-membership guidance for the system store."""
    if path != SYSTEM_PROFILES_PATH:
        return ""

    return (
        f" Ensure the user belongs to the '{SYSTEM_GROUP}' group "
        f"and that membership is active. Run "
        f"'newgrp {SYSTEM_GROUP}' or start a new login session."
    )


def load_profile_store(
    path: Path,
    *,
    missing_ok: bool,
):
    """Load one profile store with clean missing/access handling."""
    try:
        file_status = path.stat()
    except FileNotFoundError:
        if missing_ok:
            return None

        die(f"Profiles file not found: {path}", 2)

    except PermissionError:
        die(
            f"Permission denied reading profiles file: {path}."
            f"{system_access_hint(path)}",
            2,
        )

    except OSError as exc:
        die(
            f"Cannot inspect profiles file '{path}': {exc}",
            2,
        )

    if not stat.S_ISREG(file_status.st_mode):
        die(
            f"Profiles path is not a regular file: {path}",
            2,
        )

    if not os.access(path, os.R_OK):
        die(
            f"Profiles file is not readable: {path}."
            f"{system_access_hint(path)}",
            2,
        )

    return load_profiles(str(path))


def profile_from_store(
    profile_name: str,
    profile_path: Path,
    *,
    missing_store_ok: bool,
):
    """Return a named profile from one store, or None."""
    profiles = load_profile_store(
        profile_path,
        missing_ok=missing_store_ok,
    )

    if profiles is None:
        return None

    profile = profiles.get(profile_name)

    if profile is None:
        return None

    if not isinstance(profile, dict):
        die(
            f"Profile '{profile_name}' in {profile_path} "
            "must be a JSON object.",
            2,
        )

    return profile, str(profile_path)


def resolve_named_profile(
    profile_name: str,
    explicit_path: str = "",
) -> tuple[dict, str]:
    """Resolve NAME using explicit-file or personal-first rules."""
    override_path = profiles_override_path(explicit_path)

    if override_path is not None:
        result = profile_from_store(
            profile_name,
            override_path,
            missing_store_ok=False,
        )

        if result is None:
            die(
                f"Unknown profile '{profile_name}' in "
                f"{override_path}.",
                2,
            )

        return result

    personal_path = default_profiles_path()

    result = profile_from_store(
        profile_name,
        personal_path,
        missing_store_ok=True,
    )

    # A personal match wins immediately. Do not inspect /etc.
    if result is not None:
        return result

    result = profile_from_store(
        profile_name,
        SYSTEM_PROFILES_PATH,
        missing_store_ok=True,
    )

    if result is not None:
        return result

    die(
        f"Unknown profile '{profile_name}'. Looked first in "
        f"{personal_path}, then in {SYSTEM_PROFILES_PATH}.",
        2,
    )


def inspect_profile_store(path: Path):
    """Inspect a profile store without failing profile-list."""
    try:
        file_status = path.stat()
    except FileNotFoundError:
        return {}, "missing"
    except PermissionError:
        return {}, "permission denied"
    except OSError as exc:
        return {}, str(exc)

    if not stat.S_ISREG(file_status.st_mode):
        return {}, "not a regular file"

    if not os.access(path, os.R_OK):
        return {}, "permission denied"

    return load_profiles(str(path)), ""


def print_profile_list(explicit_path: str = "") -> None:
    """Show profile names and stores available to this process."""
    override_path = profiles_override_path(explicit_path)

    if override_path is not None:
        profiles, status = inspect_profile_store(override_path)

        print("Available Postout profiles")
        print(f"\nProfiles file: {override_path}")

        if status:
            print(f"Unavailable: {status}")
            return

        if not profiles:
            print("No profiles configured.")
            return

        print("\nNAME")
        for name in sorted(profiles):
            print(name)

        return

    personal_path = default_profiles_path()
    personal_profiles, personal_status = inspect_profile_store(
        personal_path
    )
    system_profiles, system_status = inspect_profile_store(
        SYSTEM_PROFILES_PATH
    )

    rows = []

    if personal_profiles:
        automatic_name = (
            next(iter(personal_profiles))
            if len(personal_profiles) == 1
            else None
        )
    elif system_profiles and len(system_profiles) == 1:
        automatic_name = next(iter(system_profiles))
    else:
        automatic_name = None

    for name in sorted(personal_profiles):
        status = (
            "automatic"
            if name == automatic_name
            else "available"
        )
        rows.append(
            (
                name,
                "personal",
                status,
                str(personal_path),
            )
        )

    for name in sorted(system_profiles):
        if name in personal_profiles:
            status = "shadowed"
        elif name == automatic_name:
            status = "automatic"
        else:
            status = "available"

        rows.append(
            (
                name,
                "system",
                status,
                str(SYSTEM_PROFILES_PATH),
            )
        )

    print("Available Postout profiles")

    if rows:
        name_width = max(
            len("NAME"),
            *(len(row[0]) for row in rows),
        )
        scope_width = max(
            len("SCOPE"),
            *(len(row[1]) for row in rows),
        )
        status_width = max(
            len("STATUS"),
            *(len(row[2]) for row in rows),
        )

        print()
        print(
            f"{'NAME':<{name_width}}  "
            f"{'SCOPE':<{scope_width}}  "
            f"{'STATUS':<{status_width}}  FILE"
        )

        for name, scope, status, source in rows:
            print(
                f"{name:<{name_width}}  "
                f"{scope:<{scope_width}}  "
                f"{status:<{status_width}}  "
                f"{source}"
            )
    else:
        print("\nNo profiles are available.")

    if personal_status not in {"", "missing"}:
        print(
            f"\nPersonal profiles unavailable: "
            f"{personal_status}"
        )

    if system_status not in {"", "missing"}:
        print(
            f"\nSystem profiles unavailable: {system_status}"
        )

        if system_status == "permission denied":
            print(
                f"Activate membership with: "
                f"newgrp {SYSTEM_GROUP}"
            )


def direct_smtp_requested(args) -> bool:
    """Return whether the caller explicitly selected direct SMTP."""
    direct_user = (
        args.smtp_user
        or os.environ.get("SMTP_USER")
        or os.environ.get("GMAIL_USERNAME")
    )
    direct_password = (
        args.smtp_pass
        or os.environ.get("SMTP_PASS")
        or os.environ.get("GMAIL_APP_PASSWORD")
    )

    return bool(
        args.smtp_host
        or os.environ.get("SMTP_HOST")
        or (direct_user and direct_password)
    )



def automatic_profile_from_store(
    profiles: dict,
    profile_path: Path,
    scope: str,
):
    """Select the sole profile or require an explicit choice."""
    names = sorted(profiles)

    if not names:
        return None

    if len(names) > 1:
        joined_names = ", ".join(names)

        die(
            f"Multiple {scope} profiles are available: "
            f"{joined_names}. Use --profile NAME or set "
            f"POSTOUT_PROFILE. Run 'postout --profile-list' "
            "to review the choices.",
            2,
        )

    profile_name = names[0]
    profile = profiles[profile_name]

    if not isinstance(profile, dict):
        die(
            f"Profile '{profile_name}' in {profile_path} "
            "must be a JSON object.",
            2,
        )

    return profile_name, profile, str(profile_path)


def resolve_profile_for_send(args):
    """Resolve an explicit or automatic SMTP profile for sending."""
    requested_name = (
        args.profile
        or os.environ.get("POSTOUT_PROFILE", "")
    ).strip()

    if requested_name:
        profile, source = resolve_named_profile(
            requested_name,
            args.profiles_file,
        )
        return requested_name, profile, source

    # A host or complete credential pair explicitly selects direct
    # SMTP. Other SMTP and sender switches remain profile overrides.
    if direct_smtp_requested(args):
        return "", None, ""

    override_path = profiles_override_path(
        args.profiles_file
    )

    if override_path is not None:
        profiles = load_profile_store(
            override_path,
            missing_ok=False,
        )

        result = automatic_profile_from_store(
            profiles,
            override_path,
            "selected-file",
        )

        if result is not None:
            return result

        die(
            f"No profiles are configured in {override_path}.",
            2,
        )

    personal_path = default_profiles_path()
    personal_profiles = load_profile_store(
        personal_path,
        missing_ok=True,
    )

    # If the user has any personal profiles, stay within that
    # scope for automatic selection.
    if personal_profiles:
        return automatic_profile_from_store(
            personal_profiles,
            personal_path,
            "personal",
        )

    system_profiles = load_profile_store(
        SYSTEM_PROFILES_PATH,
        missing_ok=True,
    )

    if system_profiles:
        return automatic_profile_from_store(
            system_profiles,
            SYSTEM_PROFILES_PATH,
            "system",
        )

    die(
        "No SMTP profiles are available. Run 'postout config' "
        "or use direct SMTP options. Run 'postout --help' for usage.",
        2,
    )



def load_profiles_document(path: Path) -> dict:
    """Load a profile document for interactive editing."""
    if not path.exists():
        return {"profiles": {}}

    if not path.is_file():
        die(f"Profiles path is not a regular file: {path}", 2)

    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"Failed to read profiles JSON: {exc}", 2)

    extract_profiles(document, path)
    return document



def require_system_privileges() -> None:
    """Re-run system configuration through sudo when necessary."""
    if not hasattr(os, "geteuid"):
        die("System profile configuration requires a Unix-like OS.", 1)

    if os.geteuid() == 0:
        return

    info("System profile configuration requires administrator access.")
    script_path = str(Path(sys.argv[0]).resolve())

    try:
        os.execvp(
            "sudo",
            [
                "sudo",
                "--",
                sys.executable,
                script_path,
                *sys.argv[1:],
            ],
        )
    except FileNotFoundError:
        die(
            "sudo is not installed. Run this command as root instead.",
            1,
        )


def ensure_system_group():
    """Create or return the shared Postout system group."""
    try:
        return grp.getgrnam(SYSTEM_GROUP)
    except KeyError:
        try:
            subprocess.run(
                ["groupadd", "--system", SYSTEM_GROUP],
                check=True,
            )
        except FileNotFoundError:
            die("groupadd is not available on this system.", 1)
        except subprocess.CalledProcessError as exc:
            die(
                f"Failed to create system group '{SYSTEM_GROUP}': "
                f"{exc}",
                1,
            )

    try:
        return grp.getgrnam(SYSTEM_GROUP)
    except KeyError:
        die(
            f"System group '{SYSTEM_GROUP}' was not created.",
            1,
        )


def ensure_system_storage() -> None:
    """Create and secure /etc/postout for shared profiles."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        die("System profile storage must be prepared as root.", 1)

    system_group = ensure_system_group()

    SYSTEM_CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o750,
    )
    os.chown(
        SYSTEM_CONFIG_DIR,
        0,
        system_group.gr_gid,
    )
    os.chmod(SYSTEM_CONFIG_DIR, 0o750)

    if SYSTEM_PROFILES_PATH.exists():
        os.chown(
            SYSTEM_PROFILES_PATH,
            0,
            system_group.gr_gid,
        )
        os.chmod(SYSTEM_PROFILES_PATH, 0o640)


def save_profiles_document(
    path: Path,
    document: dict,
    system: bool = False,
) -> None:
    """Atomically save a personal or shared system profiles file."""
    directory = path.parent

    if system:
        ensure_system_storage()
        system_group = grp.getgrnam(SYSTEM_GROUP)
        directory_mode = 0o750
        file_mode = 0o640
    else:
        directory.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
        os.chmod(directory, 0o700)
        system_group = None
        directory_mode = 0o700
        file_mode = 0o600

    fd, temporary_name = tempfile.mkstemp(
        prefix=".profiles.",
        suffix=".tmp",
        dir=str(directory),
        text=True,
    )
    temporary_path = Path(temporary_name)

    try:
        os.fchmod(fd, 0o600)

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                document,
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)

        if system:
            os.chown(path, 0, system_group.gr_gid)

        os.chmod(path, file_mode)

    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    info(f"Saved profiles: {path}")
    info(
        f"Permissions: directory {directory_mode:o}, "
        f"file {file_mode:o}"
    )


def prompt_text(
    label: str,
    default=None,
    required: bool = False,
) -> str:
    while True:
        suffix = ""

        if default not in [None, ""]:
            suffix = f" [{default}]"

        value = input(f"{label}{suffix}: ").strip()

        if value:
            return value

        if default is not None:
            return str(default)

        if not required:
            return ""

        print("A value is required.", file=sys.stderr)


def prompt_yes_no(label: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"

    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()

        if not value:
            return default

        if value in {"y", "yes"}:
            return True

        if value in {"n", "no"}:
            return False

        print("Please answer yes or no.", file=sys.stderr)


def profile_security(profile: dict) -> str:
    if _get_bool(profile.get("smtp_ssl"), False):
        return "ssl"

    if _get_bool(profile.get("smtp_starttls"), False):
        return "starttls"

    return "none"


def prompt_security(default: str = "ssl") -> str:
    choices = {
        "1": "ssl",
        "2": "starttls",
        "3": "none",
    }

    default_number = {
        "ssl": "1",
        "starttls": "2",
        "none": "3",
    }.get(default, "1")

    while True:
        print("\nSecurity:")
        print("  1. SSL/TLS   (usually port 465)")
        print("  2. STARTTLS  (usually port 587)")
        print("  3. None      (usually port 25)")

        selected = input(
            f"Choose [{default_number}]: "
        ).strip() or default_number

        if selected in choices:
            return choices[selected]

        print("Choose 1, 2, or 3.", file=sys.stderr)


def prompt_password(existing: str = "") -> str:
    while True:
        if existing:
            password = getpass.getpass(
                "SMTP password [Enter keeps existing]: "
            )

            if password == "":
                return existing
        else:
            password = getpass.getpass(
                "SMTP password or app password: "
            )

            if password == "":
                print("A password is required.", file=sys.stderr)
                continue

        confirmation = getpass.getpass(
            "Confirm SMTP password: "
        )

        if password == confirmation:
            return password

        print("Passwords do not match. Try again.", file=sys.stderr)


def profile_authentication(
    profile: dict,
    profile_name: str = "",
) -> bool:
    """Return a profile's required explicit authentication setting."""
    label = (
        f"Profile '{profile_name}'"
        if profile_name
        else "Profile"
    )

    if "smtp_auth" not in profile:
        die(
            f"{label} is missing the required boolean "
            "'smtp_auth' setting. Recreate or edit the profile.",
            2,
        )

    smtp_auth = profile["smtp_auth"]

    if type(smtp_auth) is not bool:
        die(
            f"{label} has an invalid 'smtp_auth' setting. "
            "It must be true or false.",
            2,
        )

    return smtp_auth


def collect_profile(
    existing=None,
    display_name_default: str = "",
) -> dict:
    existing = dict(existing or {})

    smtp_host = prompt_text(
        "SMTP host",
        existing.get("smtp_host", "smtp.gmail.com"),
        required=True,
    )

    security = prompt_security(
        profile_security(existing) if existing else "ssl"
    )

    suggested_port = {
        "ssl": 465,
        "starttls": 587,
        "none": 25,
    }[security]

    port_default = existing.get(
        "smtp_port",
        suggested_port,
    )

    while True:
        port_text = prompt_text(
            "SMTP port",
            port_default,
            required=True,
        )

        try:
            smtp_port = int(port_text)
        except ValueError:
            print(
                "The SMTP port must be a number.",
                file=sys.stderr,
            )
            continue

        if 1 <= smtp_port <= 65535:
            break

        print(
            "The SMTP port must be between 1 and 65535.",
            file=sys.stderr,
        )

    if existing:
        default_auth = profile_authentication(existing)
    else:
        default_auth = True

    smtp_auth = prompt_yes_no(
        "SMTP authentication required?",
        default_auth,
    )

    if smtp_auth:
        smtp_user = prompt_text(
            "SMTP username",
            existing.get("smtp_user"),
            required=True,
        )

        smtp_pass = prompt_password(
            existing.get("smtp_pass", "")
        )
    else:
        smtp_user = ""
        smtp_pass = ""

    from_default = (
        existing.get("from_email")
        or smtp_user
        or None
    )

    while True:
        from_email = prompt_text(
            "From email",
            from_default,
            required=True,
        )

        if is_reasonable_email(from_email):
            break

        print(
            "Enter a valid From email address.",
            file=sys.stderr,
        )

    if display_name_default:
        display_name = display_name_default
    else:
        display_name = prompt_text(
            "Display name",
            existing.get("display_name", ""),
        )

    updated = dict(existing)
    updated.update(
        {
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_ssl": security == "ssl",
            "smtp_starttls": security == "starttls",
            "smtp_auth": smtp_auth,
            "from_email": from_email,
            "display_name": display_name,
        }
    )

    if smtp_auth:
        updated["smtp_user"] = smtp_user
        updated["smtp_pass"] = smtp_pass
    else:
        updated.pop("smtp_user", None)
        updated.pop("smtp_pass", None)

    return updated



def print_profile_summary(
    name: str,
    profile: dict,
) -> None:
    smtp_auth = profile_authentication(
        profile,
        name,
    )

    print(
        f"\n{colorize('Profile summary', 'bold', 'cyan')}"
    )
    print(
        f"  {colorize('Name:', 'bold')}           "
        f"{colorize(name, 'green')}"
    )
    print(
        f"  {colorize('SMTP server:', 'bold')}    "
        f"{profile.get('smtp_host')}:{profile.get('smtp_port')}"
    )
    print(
        f"  {colorize('Security:', 'bold')}       "
        f"{profile_security(profile)}"
    )
    print(
        f"  {colorize('Authentication:', 'bold')} "
        f"{'required' if smtp_auth else 'not required'}"
    )

    if smtp_auth:
        print(
            f"  {colorize('SMTP user:', 'bold')}      "
            f"{profile.get('smtp_user')}"
        )
        print(
            f"  {colorize('Password:', 'bold')}       "
            f"{colorize('hidden', 'dim')}"
        )

    print(
        f"  {colorize('From email:', 'bold')}     "
        f"{profile.get('from_email')}"
    )
    print(
        f"  {colorize('Display name:', 'bold')}   "
        f"{profile.get('display_name') or '-'}"
    )



def choose_profile(profiles: dict, action: str):
    names = sorted(profiles)

    if not names:
        print(
            f"\n{colorize('No profiles have been configured.', 'yellow')}"
        )
        return None

    print(
        f"\n{colorize(f'Choose a profile to {action}', 'bold', 'cyan')}"
    )

    for number, name in enumerate(names, start=1):
        print(
            f"  {colorize(number, 'yellow')}. "
            f"{colorize(name, 'green')}"
        )

    print(
        f"  {colorize('0', 'red')}. "
        f"{colorize('Cancel', 'dim')}"
    )

    while True:
        selected = input(
            f"{colorize('Choose:', 'bold', 'yellow')} "
        ).strip().lower()

        if selected == "0":
            return None

        try:
            index = int(selected)
        except ValueError:
            print("Enter a profile number or 0.", file=sys.stderr)
            continue

        if 1 <= index <= len(names):
            return names[index - 1]

        print("That profile number does not exist.", file=sys.stderr)



def add_profile(
    path: Path,
    document: dict,
    system: bool = False,
    requested_name: str = "",
    display_name_default: str = "",
) -> bool:
    profiles = document["profiles"]

    print("\nAdd profile")

    if requested_name:
        name = requested_name

        if not PROFILE_NAME_RE.fullmatch(name):
            die(
                "Profile names may contain only letters, numbers, "
                "dot, underscore, or hyphen.",
                2,
            )

        if name in profiles:
            die(
                f"Profile '{name}' already exists.",
                2,
            )

    else:
        while True:
            name = prompt_text("Profile name", required=True)

            if not PROFILE_NAME_RE.fullmatch(name):
                print(
                    "Use letters, numbers, dot, underscore, or hyphen.",
                    file=sys.stderr,
                )
                continue

            if name in profiles:
                print(
                    f"Profile '{name}' already exists. Use Edit profile.",
                    file=sys.stderr,
                )
                continue

            break

    profile = collect_profile(
        display_name_default=display_name_default,
    )
    print_profile_summary(name, profile)

    if not prompt_yes_no(f"Save profile '{name}'?", True):
        print("Profile was not saved.")
        return False

    profiles[name] = profile
    save_profiles_document(
        path,
        document,
        system=system,
    )
    return True


def edit_profile(
    path: Path,
    document: dict,
    system: bool = False,
    requested_name: str = "",
    display_name_default: str = "",
) -> None:
    profiles = document["profiles"]

    if requested_name:
        name = requested_name

        if name not in profiles:
            die(
                f"Profile '{name}' does not exist.",
                2,
            )
    else:
        name = choose_profile(profiles, "edit")

        if name is None:
            return

    print(f"\nEdit profile: {name}")

    profile = collect_profile(
        profiles[name],
        display_name_default=display_name_default,
    )
    print_profile_summary(name, profile)

    if not prompt_yes_no(f"Save changes to '{name}'?", True):
        print("Changes were not saved.")
        return

    profiles[name] = profile
    save_profiles_document(
        path,
        document,
        system=system,
    )


def delete_profile(
    path: Path,
    document: dict,
    system: bool = False,
) -> None:
    profiles = document["profiles"]
    name = choose_profile(profiles, "delete")

    if name is None:
        return

    if not prompt_yes_no(
        f"Delete profile '{name}' permanently?",
        False,
    ):
        print("Profile was not deleted.")
        return

    del profiles[name]
    save_profiles_document(
        path,
        document,
        system=system,
    )
    info(f"Deleted profile: {name}")


def test_profile(path: Path, document: dict) -> None:
    profiles = document["profiles"]
    name = choose_profile(profiles, "test")

    if name is None:
        return

    profile = profiles[name]
    default_recipient = (
        profile.get("from_email")
        or profile.get("smtp_user")
        or ""
    )

    print(
        f"\n{colorize(f'Test profile: {name}', 'bold', 'cyan')}"
    )

    while True:
        recipient = prompt_text(
            "Test recipient",
            default_recipient,
            required=True,
        )

        if is_reasonable_email(recipient):
            break

        print(
            "Enter a valid recipient email address.",
            file=sys.stderr,
        )

    if not prompt_yes_no(
        f"Send a test email to {recipient}?",
        True,
    ):
        print("Test email was not sent.")
        return

    subject = f"Postout profile test: {name}"
    body = (
        f"This is a test email sent by Postout using "
        f"the profile '{name}'.\n\n"
        "If you received this message, the SMTP profile "
        "is working correctly.\n"
    )

    test_args = build_arg_parser().parse_args(
        [
            "--profiles-file",
            str(path),
            "--profile",
            name,
            "--to",
            recipient,
            "--subject",
            subject,
            "--body",
            body,
        ]
    )

    try:
        send_email(test_args)
    except SystemExit as exc:
        if exc.code not in [None, 0]:
            print(
                colorize(
                    f"Profile test failed for '{name}'.",
                    "bold",
                    "red",
                ),
                file=sys.stderr,
            )
        return

    print(
        colorize(
            f"Profile '{name}' tested successfully.",
            "bold",
            "green",
        )
    )


def user_has_system_access(username: str) -> bool:
    """Return whether a Unix user can read system profiles."""
    account = pwd.getpwnam(username)
    system_group = grp.getgrnam(SYSTEM_GROUP)

    return (
        account.pw_uid == 0
        or account.pw_gid == system_group.gr_gid
        or username in system_group.gr_mem
    )


def explain_group_activation(
    username: str,
    newly_added: bool,
) -> None:
    """Explain when Unix group membership becomes active."""
    if newly_added:
        message = (
            f"Access has been recorded for '{username}'."
        )
    else:
        message = (
            f"Account membership for '{username}' is already present."
        )

    print(colorize(message, "green"))
    print(
        colorize(
            "Existing login sessions do not receive new group "
            "membership automatically.",
            "yellow",
        )
    )

    sudo_user = os.environ.get("SUDO_USER", "").strip()

    if username == sudo_user:
        print(
            f"After leaving Postout, run: "
            f"{colorize(f'newgrp {SYSTEM_GROUP}', 'bold')}"
        )
    else:
        print(
            f"User '{username}' must log out and back in, "
            f"or run: "
            f"{colorize(f'newgrp {SYSTEM_GROUP}', 'bold')}"
        )

    print(
        "Services running under that account must be restarted."
    )


def grant_system_profile_access_to_user(
    username: str,
) -> bool:
    """Grant one Unix user access to shared Postout profiles."""
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        print(
            f"Linux user '{username}' does not exist.",
            file=sys.stderr,
        )
        return False

    if account.pw_uid == 0:
        info("root already has access to system profiles.")
        return True

    if user_has_system_access(username):
        info(
            f"User '{username}' already has "
            "system profile access."
        )
        return True

    try:
        subprocess.run(
            [
                "usermod",
                "-aG",
                SYSTEM_GROUP,
                username,
            ],
            check=True,
        )
    except FileNotFoundError:
        die(
            "usermod is not available on this system.",
            1,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"Failed to grant access to "
            f"'{username}': {exc}",
            file=sys.stderr,
        )
        return False

    info(
        f"Added '{username}' to the "
        f"'{SYSTEM_GROUP}' group."
    )
    explain_group_activation(
        username,
        newly_added=True,
    )
    return True


def grant_system_profile_access(
    confirm_start: bool = True,
) -> None:
    """Interactively grant system-profile access to Unix users."""
    print(
        f"\n{colorize('System profile access', 'bold', 'cyan')}"
    )
    print(
        f"Members of the '{SYSTEM_GROUP}' group can read and use "
        "all system SMTP profiles and their credentials."
    )

    if confirm_start:
        current_user = os.environ.get(
            "SUDO_USER",
            "",
        ).strip()

        if current_user == "root":
            current_user = ""

        if current_user:
            try:
                pwd.getpwnam(current_user)
            except KeyError:
                current_user = ""

        if current_user:
            highlighted_user = colorize(
                current_user,
                "bold",
                "magenta",
            )

            if user_has_system_access(current_user):
                print(
                    f"{highlighted_user} already has "
                    "system profile access."
                )
            elif prompt_yes_no(
                f"Grant system-profile access to "
                f"{highlighted_user}?",
                True,
            ):
                grant_system_profile_access_to_user(
                    current_user
                )

            if not prompt_yes_no(
                "Grant access to another Unix user?",
                False,
            ):
                return
        elif not prompt_yes_no(
            "Grant system-profile access to a Unix user now?",
            True,
        ):
            return

    while True:
        username = input(
            "Username (blank to return): "
        ).strip()

        if not username:
            print(
                colorize(
                    "Returning to system configuration.",
                    "dim",
                )
            )
            break

        granted = grant_system_profile_access_to_user(
            username
        )

        if not granted:
            if prompt_yes_no(
                "Try another username?",
                True,
            ):
                continue

            break

        if not prompt_yes_no(
            "Add another user?",
            False,
        ):
            break

def build_config_arg_parser():
    parser = argparse.ArgumentParser(
        prog="postout config",
        description=(
            "Interactively add, edit, delete, and test SMTP profiles."
        ),
        epilog=(
            "Personal profiles are stored under ~/.config/postout. "
            "System profiles are stored in /etc/postout and are "
            "available to root and members of the 'postout' group."
        ),
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help=(
            "Manage shared profiles in /etc/postout; "
            "administrator access is requested with sudo"
        ),
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--display-name",
        metavar="TEXT",
        default="",
        help=argparse.SUPPRESS,
    )
    return parser



def run_profile_config(
    profile_name: str,
    system: bool = False,
    display_name: str = "",
) -> None:
    """Create or edit one explicitly requested profile."""

    if not PROFILE_NAME_RE.fullmatch(profile_name):
        die(
            "Profile names may contain only letters, numbers, "
            "dot, underscore, or hyphen.",
            2,
        )

    if system:
        ensure_system_storage()
        path = SYSTEM_PROFILES_PATH
    else:
        path = default_profiles_path()

    document = load_profiles_document(path)
    profiles = document["profiles"]

    if profile_name in profiles:
        edit_profile(
            path,
            document,
            system=system,
            requested_name=profile_name,
            display_name_default=display_name,
        )
        return

    created = add_profile(
        path,
        document,
        system=system,
        requested_name=profile_name,
        display_name_default=display_name,
    )

    if system and created:
        grant_system_profile_access()


def run_config_menu(system: bool = False) -> None:
    if system:
        ensure_system_storage()
        path = SYSTEM_PROFILES_PATH
        heading = "Postout system configuration"
    else:
        path = default_profiles_path()
        heading = "Postout configuration"

    show_system_access_note = system

    while True:
        document = load_profiles_document(path)
        profiles = document["profiles"]
        names = sorted(profiles)

        print(
            f"\n{colorize(heading, 'bold', 'cyan')}"
        )
        print(
            f"{colorize('Profiles file:', 'dim')} "
            f"{colorize(path, 'cyan')}"
        )

        if system:
            print(
                f"{colorize('Access:', 'dim')} "
                f"root and members of group '{SYSTEM_GROUP}'"
            )

            if show_system_access_note:
                print()
                print(colorize("System access", "bold"))
                print(
                    "  Root can use system profiles immediately."
                )
                print(
                    "  Other users must be granted access."
                )
                show_system_access_note = False

        if not path.exists():
            print(
                f"{colorize('Status:', 'dim')} "
                f"{colorize('not created yet', 'yellow')}"
            )

        print(
            f"\n{colorize('Profiles', 'bold')}"
        )

        if names:
            for number, name in enumerate(names, start=1):
                profile = profiles[name]
                identity = (
                    profile.get("from_email")
                    or profile.get("smtp_user")
                    or "-"
                )

                print(
                    f"  {colorize(number, 'yellow')}. "
                    f"{colorize(name, 'green')} "
                    f"{colorize(f'({identity})', 'dim')}"
                )
        else:
            print(
                f"  {colorize('No profiles configured', 'dim')}"
            )

        print(
            f"\n{colorize('Actions', 'bold')}"
        )
        print(
            f"  {colorize('1', 'yellow')}. Add profile"
        )
        print(
            f"  {colorize('2', 'yellow')}. Edit profile"
        )
        print(
            f"  {colorize('3', 'yellow')}. Delete profile"
        )
        print(
            f"  {colorize('4', 'yellow')}. Test profile"
        )

        if system:
            print(
                f"  {colorize('5', 'yellow')}. "
                "Manage user access"
            )

        print(
            f"  {colorize('q', 'red')}. Quit"
        )

        action = input(
            f"\n{colorize('Choose:', 'bold', 'yellow')} "
        ).strip().lower()

        if action == "1":
            created = add_profile(
                path,
                document,
                system=system,
            )

            if system and created:
                grant_system_profile_access()
        elif action == "2":
            edit_profile(
                path,
                document,
                system=system,
            )
        elif action == "3":
            delete_profile(
                path,
                document,
                system=system,
            )
        elif action == "4":
            test_profile(path, document)
        elif action == "5" and system:
            grant_system_profile_access(
                confirm_start=False,
            )
        elif action in {"q", "quit", "exit"}:
            print(
                colorize("Configuration closed.", "dim")
            )
            return
        else:
            choices = (
                "1, 2, 3, 4, 5, or q"
                if system
                else "1, 2, 3, 4, or q"
            )
            print(
                f"Choose {choices}.",
                file=sys.stderr,
            )


# ----------------------------
# Main send logic
# ----------------------------

def send_email(args) -> None:
    profile_name, profile, _profile_source = (
        resolve_profile_for_send(args)
    )

    # SMTP configuration
    host = resolve_setting(
        args.smtp_host,
        (profile or {}).get("smtp_host"),
        os.environ.get("SMTP_HOST"),
        "smtp.gmail.com",
    )

    port = resolve_setting(
        args.smtp_port,
        (profile or {}).get("smtp_port"),
        os.environ.get("SMTP_PORT"),
        "465",
    )

    try:
        port = int(port)
    except (TypeError, ValueError):
        die(f"Invalid SMTP port: {port}", 2)

    if not 1 <= port <= 65535:
        die(
            f"Invalid SMTP port: {port}. "
            "Use a value from 1 to 65535.",
            2,
        )

    if profile is not None:
        use_authentication = profile_authentication(
            profile,
            profile_name,
        )

        if use_authentication:
            user = resolve_setting(
                args.smtp_user,
                profile.get("smtp_user"),
                None,
                "",
            )
            password = resolve_setting(
                args.smtp_pass,
                profile.get("smtp_pass"),
                None,
                "",
            )

            if not user or not password:
                die(
                    f"Authenticated profile '{profile_name}' "
                    "requires both smtp_user and smtp_pass.",
                    2,
                )
        else:
            if (
                profile.get("smtp_user") not in [None, ""]
                or profile.get("smtp_pass") not in [None, ""]
            ):
                die(
                    f"Unauthenticated profile '{profile_name}' "
                    "must not contain smtp_user or smtp_pass.",
                    2,
                )

            if args.smtp_user or args.smtp_pass:
                die(
                    f"Profile '{profile_name}' has "
                    "smtp_auth=false. Remove the direct credential "
                    "overrides or use direct SMTP without --profile.",
                    2,
                )

            user = ""
            password = ""

    else:
        user = resolve_setting(
            args.smtp_user,
            None,
            (
                os.environ.get("SMTP_USER")
                or os.environ.get("GMAIL_USERNAME")
            ),
            "",
        )
        password = resolve_setting(
            args.smtp_pass,
            None,
            (
                os.environ.get("SMTP_PASS")
                or os.environ.get("GMAIL_APP_PASSWORD")
            ),
            "",
        )

        if bool(user) != bool(password):
            die(
                "Direct SMTP requires both username and password "
                "for authentication, or neither for an "
                "unauthenticated relay.",
                2,
            )

        use_authentication = bool(user and password)

    # SSL / STARTTLS selection
    default_ssl = _get_bool(os.environ.get("SMTP_SSL", "1"), True)
    default_starttls = _get_bool(os.environ.get("SMTP_STARTTLS", "0"), False)

    prof_ssl = _get_bool((profile or {}).get("smtp_ssl"), default_ssl)
    prof_starttls = _get_bool((profile or {}).get("smtp_starttls"), default_starttls)

    use_ssl = args.smtp_ssl if args.smtp_ssl is not None else prof_ssl
    use_starttls = args.smtp_starttls if args.smtp_starttls is not None else prof_starttls

    if use_ssl and use_starttls:
        die("Invalid SMTP config. Cannot use both SSL and STARTTLS in the same run.", 2)

    # From handling
    from_email = resolve_setting(
        args.from_email,
        (profile or {}).get("from_email"),
        os.environ.get("SMTP_FROM"),
        user,
    )
    if not is_reasonable_email(from_email):
        die(f"Invalid From email: {from_email}", 2)

    # Display name: CLI name/surname wins. Otherwise profile display_name.
    cli_display_name = " ".join([x for x in [args.name, args.surname] if x]) if (args.name or args.surname) else ""
    prof_display_name = strip_crlf((profile or {}).get("display_name", ""))
    display_name = cli_display_name if cli_display_name else prof_display_name

    # Recipients
    to_recipients = parse_recipients(
        args.to,
        "To",
    )
    cc_recipients = parse_recipients(
        args.cc or "",
        "Cc",
    )
    bcc_recipients = parse_recipients(
        args.bcc or "",
        "Bcc",
    )

    all_rcpt = deduplicate_envelope_addresses(
        to_recipients,
        cc_recipients,
        bcc_recipients,
    )

    if not all_rcpt:
        die("No recipients provided.", 2)

    to_headers = [
        header_value
        for header_value, _ in to_recipients
    ]
    cc_headers = [
        header_value
        for header_value, _ in cc_recipients
    ]

    subject = strip_crlf(args.subject or "")
    if args.require_subject and subject == "":
        die("Subject is required.", 2)

    msg = EmailMessage()
    msg["From"] = build_from_header(
        display_name,
        from_email,
    )

    if to_headers:
        msg["To"] = ", ".join(to_headers)

    if cc_headers:
        msg["Cc"] = ", ".join(cc_headers)

    msg["Subject"] = subject

    # Body
    body = read_body(args)

    if args.html:
        html_body = body if body else "<p>(empty)</p>"
        text_fallback = (
            args.text_fallback
            or html_to_text(html_body)
            or "(no text body)"
        )

        msg.set_content(text_fallback)
        msg.add_alternative(html_body, subtype="html")
    else:
        msg.set_content(body if body else "(empty)")

    # Attachments
    attachment_paths = args.attachments or []

    try:
        preflight_bytes = preflight_attachments(
            attachment_paths
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        die(str(exc), 2)

    total_bytes = 0

    try:
        for filepath in attachment_paths:
            remaining_bytes = (
                MAX_ATTACHMENT_BYTES - total_bytes
            )

            total_bytes += attach_file(
                msg,
                filepath,
                remaining_bytes,
            )
    except (FileNotFoundError, OSError, ValueError) as exc:
        die(str(exc), 2)

    # A shrinking file is harmless, but an unexplained increase is not.
    if total_bytes > preflight_bytes:
        warn(
            "One or more attachment sizes changed during processing."
        )

    # Send
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP

    try:
        with smtp_class(
            host,
            port,
            timeout=SMTP_TIMEOUT_SECONDS,
        ) as smtp:
            if not use_ssl:
                smtp.ehlo()

                if use_starttls:
                    smtp.starttls()
                    smtp.ehlo()

            if use_authentication:
                smtp.login(user, password)

            smtp.send_message(msg, to_addrs=all_rcpt)

    except Exception as exc:
        die(f"Failed to send email: {exc}", 1)

    info(
        f"Email sent. Profile={profile_name or '-'} "
        f"To={len(to_recipients)} "
        f"Cc={len(cc_recipients)} "
        f"Bcc={len(bcc_recipients)} "
        f"AttachBytes={total_bytes}"
    )

def build_arg_parser():
    examples = r"""Examples:
  Manage personal SMTP profiles interactively:
    postout config

  Manage shared system SMTP profiles:
    postout config --system

  List the profiles available to the current user:
    postout --profile-list

  Profiles explicitly record whether SMTP authentication is required.
  Direct SMTP authenticates when both username and password are given;
  supplying neither uses an unauthenticated relay.

  Use the interactive configuration menus to add, edit, delete, and
  test profiles. Manual editing of profile files is not normally
  required.

  The sending examples assume a configured profile named "gmail".

  Send a short message:
    postout --profile gmail -t user@example.com \
      -u "Hello" -m "Test message"

  Read the message body from a file:
    postout --profile gmail -t user@example.com \
      -u "Report" --body-file report.txt

  Send command output as the message body:
    df -h | postout --profile gmail \
      -t admin@example.com -u "Disk usage"

  Send one or more attachments:
    postout --profile gmail -t user@example.com \
      -u "Documents" -m "Please see the attached files." \
      -a invoice.pdf report.csv

  Send HTML from a file:
    postout --profile gmail -t user@example.com \
      -u "Newsletter" --html --body-file newsletter.html

  Postout sends HTML unchanged. Applications inserting untrusted values
  into HTML must escape or sanitize those values before invoking Postout.

  Send to multiple recipients with CC and BCC:
    postout --profile gmail \
      -t "alice@example.com,bob@example.com" \
      --cc manager@example.com \
      --bcc archive@example.com \
      -u "Status update" -m "The work is complete."

Body input:
  Use one of:
    -m TEXT
    --body-file PATH
    --body-file -
    piped or redirected stdin

  If no body source is supplied, Postout sends an empty body.
"""

    p = argparse.ArgumentParser(
        prog="postout",
        usage=(
            "%(prog)s [OPTIONS] -t ADDRESS\n"
            "       %(prog)s --profile-list"
        ),
        description=(
            "Send email through a configured SMTP profile "
            "or direct SMTP settings."
        ),
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show version and exit",
    )

    profile_group = p.add_argument_group("Profile and configuration")
    profile_group.add_argument(
        "--profile-list",
        action="store_true",
        help=(
            "List accessible personal and system profiles, "
            "their source files, and automatic selection"
        ),
    )
    profile_group.add_argument(
        "--profile",
        metavar="NAME",
        default="",
        help=(
            "Use profile NAME; personal profiles take priority and "
            "the sole available profile is selected automatically"
        ),
    )
    profile_group.add_argument(
        "--profiles-file",
        metavar="PATH",
        default="",
        help=(
            "Advanced: search only PATH instead of the personal "
            "and system profile stores"
        ),
    )

    recipient_group = p.add_argument_group("Recipients")
    recipient_group.add_argument(
        "-t",
        "--to",
        metavar="ADDRESS",
        default="",
        help="To recipient(s), comma-separated",
    )
    recipient_group.add_argument(
        "--cc",
        metavar="ADDRESS",
        default="",
        help="CC recipient(s), comma-separated",
    )
    recipient_group.add_argument(
        "--bcc",
        metavar="ADDRESS",
        default="",
        help="BCC recipient(s), comma-separated",
    )

    message_group = p.add_argument_group("Message")
    message_group.add_argument(
        "-u",
        "--subject",
        metavar="TEXT",
        default="",
        help="Subject line",
    )
    message_group.add_argument(
        "--require-subject",
        action="store_true",
        help="Fail instead of sending an empty subject",
    )

    body_group = message_group.add_mutually_exclusive_group()
    body_group.add_argument(
        "-m",
        "--body",
        metavar="TEXT",
        default=None,
        help="Use TEXT as the message body",
    )
    body_group.add_argument(
        "--body-file",
        metavar="PATH",
        default=None,
        help="Read the UTF-8 message body from PATH; use '-' for stdin",
    )

    message_group.add_argument(
        "--html",
        action="store_true",
        help="Send the body as HTML with a plain-text alternative",
    )
    message_group.add_argument(
        "--text-fallback",
        metavar="TEXT",
        default="",
        help="Use TEXT as the plain-text alternative for HTML mail",
    )
    attachment_group = p.add_argument_group("Attachments")
    attachment_group.add_argument(
        "-a",
        "--attachments",
        metavar="FILE",
        nargs="*",
        default=[],
        help="Attach one or more files",
    )

    sender_group = p.add_argument_group("Sender identity")
    sender_group.add_argument(
        "--from-email",
        metavar="ADDRESS",
        default="",
        help="Override the profile or SMTP sender address",
    )
    sender_group.add_argument(
        "--name",
        metavar="TEXT",
        default="",
        help="Sender first name",
    )
    sender_group.add_argument(
        "--surname",
        metavar="TEXT",
        default="",
        help="Sender surname",
    )

    smtp_group = p.add_argument_group("Direct SMTP overrides")
    smtp_group.add_argument(
        "--smtp-host",
        metavar="HOST",
        default="",
        help="SMTP server hostname",
    )
    smtp_group.add_argument(
        "--smtp-port",
        metavar="PORT",
        default="",
        help="SMTP server port",
    )
    smtp_group.add_argument(
        "--smtp-user",
        metavar="USER",
        default="",
        help=(
            "SMTP authentication username; supply together with "
            "--smtp-pass"
        ),
    )
    smtp_group.add_argument(
        "--smtp-pass",
        metavar="PASSWORD",
        default="",
        help=(
            "SMTP password; supply together with --smtp-user. "
            "Command-line use may expose it"
        ),
    )
    smtp_group.add_argument(
        "--smtp-ssl",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use implicit SMTP over SSL",
    )
    smtp_group.add_argument(
        "--smtp-starttls",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Upgrade the SMTP connection using STARTTLS",
    )

    return p

def print_welcome() -> None:
    """Show the landing screen for a bare Postout invocation."""
    print()
    print(colorize("POSTOUT", "bold", "cyan"))
    print(
        colorize(
            "SMTP mail for scripts, servers, and the command line.",
            "dim",
        )
    )
    print()
    print("Postout uses reusable SMTP profiles.")
    print(
        "Profiles keep connection settings and passwords "
        "out of shell history."
    )
    print()
    print(colorize("Recommended start", "bold"))
    print(
        f"  {colorize('Current user:', 'bold')} "
        f"{colorize('postout config', 'green')}"
    )
    print(
        colorize(
            "  Best for first setup, testing, and personal use.",
            "dim",
        )
    )
    print()
    print(colorize("Server notifications", "bold"))
    print(
        f"  {colorize('System-wide:', 'bold')} "
        f"{colorize('postout config --system', 'green')}"
    )
    print(
        colorize(
            "  Best for monitoring jobs, services, and shared server use.",
            "dim",
        )
    )
    print()
    print(
        f"{colorize('View profiles:', 'bold')} "
        f"{colorize('postout --profile-list', 'green')}"
    )
    print()
    print("Direct SMTP options are available for one-off use.")
    print(
        colorize(
            "Avoid passing passwords directly on the command line.",
            "yellow",
        )
    )
    print()
    print(
        f"{colorize('Need help?', 'bold')} "
        f"{colorize('postout --help', 'green')}"
    )


def main():
    if len(sys.argv) == 1:
        print_welcome()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "config":
        config_parser = build_config_arg_parser()
        config_args = config_parser.parse_args(
            sys.argv[2:]
        )

        if config_args.display_name and not config_args.profile:
            config_parser.error(
                "--display-name requires --profile"
            )

        if config_args.system:
            require_system_privileges()

        if config_args.profile:
            run_profile_config(
                config_args.profile,
                system=config_args.system,
                display_name=config_args.display_name,
            )
            return

        run_config_menu(system=config_args.system)
        return

    args = build_arg_parser().parse_args()

    if args.profile_list:
        print_profile_list(args.profiles_file)
        return

    send_email(args)

def entrypoint() -> int:
    """Run the Postout command-line interface."""
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\n[INFO] Cancelled by user.",
            file=sys.stderr,
        )
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(entrypoint())
