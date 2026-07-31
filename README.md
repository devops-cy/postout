# Postout

**Simple SMTP mail for scripts, servers, and the command line.**

Postout is a lightweight command-line mailer for sending email through SMTP. It is designed for server notifications, monitoring jobs, scheduled tasks, shell scripts, and normal interactive use.

Postout uses reusable SMTP profiles so that connection settings and passwords do not need to be repeated in scripts or shell history.

## Features

- Personal SMTP profiles for individual users
- Shared system profiles for services and server notifications
- Authenticated and unauthenticated SMTP
- SSL/TLS, STARTTLS, and plain SMTP
- Plain-text and HTML messages
- Automatic plain-text fallback for HTML email
- To, Cc, and Bcc recipients
- Multiple recipients and formatted email addresses
- File attachments
- Message bodies from arguments, files, or standard input
- Interactive profile creation, editing, deletion, and testing
- Direct SMTP mode for one-off use
- No third-party Python runtime dependencies

## Requirements

Postout supports:

- Debian 12 or later
- Ubuntu 22.04 or later
- Compatible Linux Mint releases
- Python 3.10 or later

The packaged Debian installation also uses standard Unix account and group-management tools.

## Installation

### Install the Debian package

Clone the repository and install the packaged release:

```bash
git clone https://github.com/devops-cy/postout.git
cd postout

sudo dpkg -i packages/postout_1.0.0-1_all.deb
sudo apt-get install -f
```

The second command completes dependency installation if anything required is missing.

Verify the installation:

```bash
postout
postout --help
man postout
```

### Install from Python source

For development or source-based installation:

```bash
git clone https://github.com/devops-cy/postout.git
cd postout

python3 -m venv .venv
. .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install .
```

The `postout` command will then be available inside the virtual environment.

## Quick start

Create a profile for the current user:

```bash
postout config
```

This is the recommended starting point for personal use, initial setup, and testing.

List the profiles available to the current user:

```bash
postout --profile-list
```

Send a message:

```bash
postout --profile gmail \
    --to user@example.com \
    --subject "Hello" \
    --body "Test message"
```

When only one usable profile exists, Postout selects it automatically:

```bash
postout \
    --to user@example.com \
    --subject "Hello" \
    --body "Test message"
```

## Personal profiles

Personal profiles are stored at:

```text
~/.config/postout/profiles.json
```

When `XDG_CONFIG_HOME` is set, Postout uses:

```text
$XDG_CONFIG_HOME/postout/profiles.json
```

Create or manage personal profiles with:

```bash
postout config
```

The interactive menu can:

- Add profiles
- Edit profiles
- Delete profiles
- Test profiles

Personal profile files are created with permissions intended to restrict access to their owner.

## System profiles

Shared system profiles are useful for:

- Monitoring jobs
- Backup scripts
- Scheduled tasks
- System services
- Shared server notification accounts

Create or manage them with:

```bash
postout config --system
```

Postout requests administrator access through `sudo` when required.

System profiles are stored at:

```text
/etc/postout/profiles.json
```

Root can use system profiles immediately. Other users must belong to the `postout` Unix group.

Postout can grant system-profile access from the interactive configurator. New group membership may require a new login session or:

```bash
newgrp postout
```

Services running under an account that has just received access must be restarted.

## Profile selection

Postout resolves profiles using these rules:

1. An explicitly named personal profile takes precedence over a system profile with the same name.
2. When no profile is named, a single personal profile is selected automatically.
3. When no personal profiles exist, a single available system profile is selected automatically.
4. When multiple profiles exist in the applicable scope, select one explicitly with `--profile`.

Example:

```bash
postout --profile notifications \
    --to admin@example.com \
    --subject "Server report" \
    --body "All checks passed."
```

Review profile availability and selection status with:

```bash
postout --profile-list
```

## Sending messages

### Plain-text message

```bash
postout --profile gmail \
    --to user@example.com \
    --subject "Hello" \
    --body "This is a test message."
```

Short option equivalents are available:

```bash
postout --profile gmail \
    -t user@example.com \
    -u "Hello" \
    -m "This is a test message."
```

### Read the body from a file

```bash
postout --profile gmail \
    --to user@example.com \
    --subject "Daily report" \
    --body-file report.txt
```

Postout reads body files as UTF-8 text.

### Read from standard input

```bash
df -h | postout --profile gmail \
    --to admin@example.com \
    --subject "Disk usage"
```

You can also use `-` explicitly:

```bash
postout --profile gmail \
    --to admin@example.com \
    --subject "Command output" \
    --body-file -
```

### Send HTML

```bash
postout --profile gmail \
    --to user@example.com \
    --subject "Service report" \
    --html \
    --body-file report.html
```

Postout sends supplied HTML unchanged and generates a plain-text alternative automatically.

A custom plain-text alternative can be supplied with:

```bash
--text-fallback "Plain-text version of the message"
```

Applications that insert untrusted values into HTML must escape or sanitize those values before invoking Postout.

### Send attachments

```bash
postout --profile gmail \
    --to user@example.com \
    --subject "Documents" \
    --body "Please see the attached files." \
    --attachments invoice.pdf report.csv
```

The short form is:

```bash
-a invoice.pdf report.csv
```

Attachments must be regular files. Postout limits the combined attachment size to 20 MB.

### Cc and Bcc

```bash
postout --profile gmail \
    --to "Alice Example <alice@example.com>,bob@example.com" \
    --cc manager@example.com \
    --bcc archive@example.com \
    --subject "Status update" \
    --body "The work is complete."
```

Bcc recipients are used during SMTP delivery but are not written into the message headers.

### Require a subject

By default, an empty subject is allowed. To reject messages without a subject:

```bash
postout --require-subject \
    --to user@example.com \
    --body "Message body"
```

## Direct SMTP mode

Profiles are recommended, but Postout also supports direct SMTP options for one-off use.

Example using STARTTLS:

```bash
postout \
    --smtp-host smtp.example.com \
    --smtp-port 587 \
    --no-smtp-ssl \
    --smtp-starttls \
    --smtp-user sender@example.com \
    --smtp-pass 'APPLICATION_PASSWORD' \
    --from-email sender@example.com \
    --to recipient@example.com \
    --subject "Direct SMTP test" \
    --body "This message was sent without a stored profile."
```

Direct SMTP authentication requires both a username and password. Supplying neither uses an unauthenticated SMTP relay.

Avoid placing passwords directly on the command line. Command arguments may be visible in shell history and process listings.

## Environment variables

Postout supports environment variables for compatibility and automated use.

Common variables include:

```text
POSTOUT_PROFILE
POSTOUT_PROFILES_FILE

SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASS
SMTP_FROM
SMTP_SSL
SMTP_STARTTLS
```

The following compatibility variables are also recognized:

```text
GMAIL_USERNAME
GMAIL_APP_PASSWORD
```

Stored profiles are usually preferable because they keep SMTP settings out of scripts.

## Security

Postout protects profile files using Unix filesystem ownership and permissions.

Typical permissions are:

```text
Personal configuration directory: 700
Personal profile file:             600

System configuration directory:   750
System profile file:               640
System ownership:                  root:postout
```

SMTP credentials stored in profiles are **not encrypted**. Their protection depends on filesystem permissions and control of the relevant user or group accounts.

Keep these points in mind:

- Do not commit profile files or credentials to Git.
- Prefer application-specific SMTP passwords where supported.
- Avoid supplying passwords directly as command arguments.
- Limit membership of the `postout` group.
- Treat system-profile access as access to all credentials stored in the shared profile file.
- Use SSL/TLS or STARTTLS whenever the SMTP server supports it.
- Only send trusted HTML.

## Useful commands

Show the welcome screen:

```bash
postout
```

Show complete command-line help:

```bash
postout --help
```

Open personal configuration:

```bash
postout config
```

Open system-wide configuration:

```bash
postout config --system
```

List profiles:

```bash
postout --profile-list
```

Read the manual:

```bash
man postout
```

## Files

```text
~/.config/postout/profiles.json
    Personal SMTP profiles

$XDG_CONFIG_HOME/postout/profiles.json
    Personal profiles when XDG_CONFIG_HOME is set

/etc/postout/profiles.json
    Shared system SMTP profiles

/usr/share/postout/
    Installed Postout Python files on Debian-based systems

/usr/share/man/man1/postout.1.gz
    Installed manual page
```

## Exit status

Postout returns:

```text
0     Successful operation
1     SMTP, authentication, permission, or operational failure
2     Invalid arguments, configuration, profile, recipient, or file input
130   Cancelled with Ctrl+C
```

## Building the Debian package

Install the standard Debian packaging tools, then run:

```bash
dpkg-buildpackage -us -uc -b
```

The generated `.deb` is written to the parent directory.

The public, ready-to-install package is retained in:

```text
packages/
```

## License

Postout is released under the **MIT No Attribution License (MIT-0)**.

You may use, copy, modify, redistribute, or include Postout in commercial or
private software without attribution. The software is provided **as is**,
without warranty or liability.

See [LICENSE](LICENSE) for the complete terms.

## Maintainer

**DEVOPS CY**

- Email: info@devops.com.cy
- Website: https://devops.com.cy
